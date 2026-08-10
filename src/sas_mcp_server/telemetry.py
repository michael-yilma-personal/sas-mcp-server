# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in collection-mode telemetry middleware for the SAS MCP server.

Injects a required ``goal`` parameter into every published tool schema and logs
each tool call's input/output/run-id/goal/status/latency to a JSONL file.
Default OFF; requires ZERO changes to existing tools; works identically in HTTP
and stdio (it relies solely on ``context.message`` and NEVER calls
get_http_request).

NO MCP SESSION DEPENDENCY. Records are grouped by ``run_id`` — a UUID minted
once per process, at import — not by the transport's MCP session id. The
protocol is moving to a SESSIONLESS era (FastMCP 4 makes it the default), where
``fastmcp_context.session_id`` is absent or synthesized per request; grouping on
it would silently shatter every trace into one-call fragments. A process-scoped
id degrades honestly instead: under stdio one process IS one client, so it is
exactly as informative as the session id was. Under HTTP a run spans every
client the process served, and ``client_name``/``client_version`` are the only
thing that tells them apart — an ACCEPTED limitation, not an oversight: there
is no protocol-level per-client key left to record.

Every record is SELF-CONTAINED, and the ``run_start`` header is re-emitted
every HEADER_INTERVAL_RECORDS calls. Log rotation discards whole files, so a
header written once per process is simply gone from a long-lived server's
retained log, taking the run's constants (and the A/B tag) with it. Because the
header's fields — including its ``ts`` — are true for the whole run, every
re-emission is byte-identical, so a consumer may take any one of them.

The disk write is offloaded to a worker thread via ``anyio.to_thread`` so the
blocking file I/O and any RotatingFileHandler rollover never run on the asyncio
event loop; the handler's own lock keeps each append atomic across threads.

REJECTED ALTERNATIVES (do not "simplify" into these):
  * ToolTransform / ArgTransform CANNOT add a brand-new ``goal`` arg
    (ArgTransform only forwards EXISTING parent properties), would force
    re-registering all ~45 tools, inherits ``additionalProperties: false`` +
    the parent output_schema, and runs INNERMOST so it could not observe auth
    failures. Middleware is the right layer.
  * A Context wrapper is impossible: Context is built internally per request.
  * A QueueHandler/QueueListener would move I/O off-loop too, but needs
    lifespan shutdown wiring in both entry points to avoid a lost final flush;
    anyio.to_thread keeps per-call flush semantics with no extra wiring.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import anyio.to_thread
import mcp.types
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# Tool/ToolResult live in fastmcp.tools.base — the path FastMCP's own middleware
# imports. A sys.modules shim in fastmcp.tools defeats Pyright's static resolver
# for this submodule, so the import is runtime-correct but flagged; ignore it.
from fastmcp.tools.base import Tool, ToolResult  # pyright: ignore[reportMissingImports]

from .usage_logger import GOAL_KEY, UsageLogger, bounded_redact

module_logger = logging.getLogger(__name__)

# v2: session_start header records; per-call seq/args_hash; tool-DECLARED
# outcome fields (tool_status/tool_message/...); tri-state result logging
# (never|failures|always) with per-record result_logged; per-record transport
# dropped (a constant — see the header record).
# v3: `session_id` -> process-scoped `run_id`, `session_start` -> `run_start`
# (+ pid, re-emitted periodically), and the header's client_name/client_version
# moved onto every record. The shape is NOT v2-compatible and a v2 log can sit
# in the same file after an upgrade, so this MUST stay bumped — a reader keying
# on `run_id` silently drops every v2 line, and a v2 `session_start` header
# passes a naive `record != "run_start"` filter as if it were a tool call.
SCHEMA_VERSION = 3

# ONE run identity per PROCESS, not per middleware instance: two middlewares in
# one process (a composed/mounted server, a re-import) must not look like two
# server runs sharing a pid. Stamped at import, which is server startup — so the
# header's ts answers "when did this server come up", not "when was it first
# used"; the two can be hours apart on an idle deployment.
_RUN_ID = str(uuid4())
_RUN_STARTED_AT = datetime.now(UTC).isoformat()

# Re-emit the run_start header every N call records. Sized so a header survives
# in any retained window: at the ~630 bytes/record measured on real logs, the
# 10 MiB default rotation holds ~16k records, so 1000 gives ~16 anchors per file
# at ~0.1% overhead. Rotation is not the only truncation — operators tail and
# split these files too.
HEADER_INTERVAL_RECORDS = 1000

# Statuses this repo's tools use to declare a failure AS DATA (the MCP layer
# sees success). Logging them per record turns 'success' rows into an honest
# funnel — real tool-level failure rates, apply_failed clustering. The pattern
# groups cover the repo's full status inventory (grep '"status": "' to re-audit
# when adding tools): *_failed, invalid_*, unknown_*, missing_*, not_*,
# *_not_found/_not_supported/_not_global, plus the explicit stragglers.
_TOOL_FAILURE_STATUSES = frozenset(
    {"error", "export_too_large", "file_unreadable", "file_upload_disabled", "no_active_session"}
)
_TOOL_FAILURE_PREFIXES = ("invalid_", "unknown_", "missing_", "not_")
_TOOL_FAILURE_SUFFIXES = ("_failed", "_not_found", "_not_supported", "_not_global")


# A call this long is almost never a real query: it is a hung call, or a host
# that slept mid-call (perf_counter keeps counting across suspend). Real logs
# carried entries up to 18.8 HOURS, silently poisoning every latency
# percentile. Flagged rather than dropped — the outlier itself is a signal.
SUSPECT_DURATION_MS = 600_000.0

# Free-text exception/HTTP noise -> a low-cardinality class analysis can group
# by. Ordered: the first match wins, so specific classes precede generic ones.
_ERROR_TYPE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bValidationError\b", re.I), "validation"),
    (re.compile(r"\bAuthenticationError\b|\b401\b|\bUnauthorized\b", re.I), "auth"),
    (re.compile(r"\b403\b|\bForbidden\b", re.I), "permission"),
    (re.compile(r"getaddrinfo|ConnectError|ConnectTimeout|Name or service not known", re.I), "network"),
    (re.compile(r"\bTimeout\b|\bTimedOut\b", re.I), "timeout"),
    (re.compile(r"Server error '5\d\d|\b5\d\d Internal Server Error\b", re.I), "server_error"),
    (re.compile(r"Client error '4\d\d|\b4\d\d\b", re.I), "client_error"),
    (re.compile(r"\bToolError\b", re.I), "tool"),
)
_HTTP_STATUS_RE = re.compile(r"\b(?:error ')?([45]\d{2})\b")


def classify_error(text: Any) -> tuple[str | None, int | None]:
    """Map an error string to ``(error_type, http_status)``.

    Errors are logged as free text, so counting "how many 404s" or "how many
    client-side validation failures" otherwise means re-deriving a regex per
    analysis. Returns ``(None, None)`` when there is no error text.
    """
    if not isinstance(text, str) or not text:
        return None, None
    status: int | None = None
    match = _HTTP_STATUS_RE.search(text)
    if match:
        try:
            status = int(match.group(1))
        except ValueError:
            status = None
    for pattern, label in _ERROR_TYPE_RULES:
        if pattern.search(text):
            return label, status
    return "unknown", status


def _is_tool_failure_status(status: Any) -> bool:
    return isinstance(status, str) and (
        status.startswith(_TOOL_FAILURE_PREFIXES)
        or status.endswith(_TOOL_FAILURE_SUFFIXES)
        or status in _TOOL_FAILURE_STATUSES
    )


_GOAL_IN_RAW_RE = re.compile(r'"goal"\s*:\s*"((?:[^"\\]|\\.)*)"')

GOAL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Before the other arguments, state in ONE sentence WHY you are calling "
        "THIS specific tool for the user's current request — the "
        "underlying goal it serves."
    ),
}


class TelemetryMiddleware(Middleware):
    """Injects ``goal`` into every listed tool schema and logs each call."""

    def __init__(
        self,
        logger: UsageLogger,
        *,
        require_goal: bool,
        transport: str,
        log_results: bool | str = True,
        run_tag: str | None = None,
    ) -> None:
        self.logger = logger
        self.require_goal = require_goal
        self.transport = transport
        # bool accepted for back-compat: True -> always, False -> never.
        if isinstance(log_results, bool):
            log_results = "always" if log_results else "never"
        self.result_mode = log_results if log_results in ("always", "failures", "never") else "never"
        self.run_tag = run_tag
        # The ONLY grouping key — process-scoped, independent of the MCP
        # session model (see the module docstring).
        self.run_id = _RUN_ID
        self._seq = 0
        # Records written since the last run_start. Starts AT the interval so
        # the header precedes the very first record.
        self._since_header = HEADER_INTERVAL_RECORDS
        # Raw (name, version) -> sanitized (name, version), so the redact+bound
        # runs once per distinct client rather than once per call.
        self._client_cache: dict[tuple[str | None, str | None], tuple[str | None, str | None]] = {}
        # Lazily resolved tool -> tier map (see _tool_tier).
        self._tool_tiers: dict[str, int] | None = None

    async def on_list_tools(
        self,
        context: MiddlewareContext[mcp.types.ListToolsRequest],
        call_next: CallNext[mcp.types.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        out: list[Tool] = []
        for t in tools:
            try:
                params = dict(t.parameters or {})
                props = dict(params.get("properties", {}))
                if GOAL_KEY not in props:  # idempotent across repeated lists
                    props = {GOAL_KEY: GOAL_SCHEMA, **props}  # goal FIRST
                    params["properties"] = props
                    if self.require_goal:
                        req = [
                            r for r in params.get("required", []) if r != GOAL_KEY
                        ]
                        params["required"] = [GOAL_KEY, *req]
                    # NEW Tool; never mutate the shared registry singleton.
                    out.append(t.model_copy(update={"parameters": params}))
                else:
                    out.append(t)
            except Exception:  # noqa: BLE001 - per-tool isolation
                out.append(t)
        return out

    async def on_call_tool(
        self,
        context: MiddlewareContext[mcp.types.CallToolRequestParams],
        call_next: CallNext[mcp.types.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        raw = dict(context.message.arguments or {})
        # Rescue the observed client bug where the ENTIRE argument object
        # arrives wrapped as {"__unparsedToolInput": {"raw": "<json>"}} — the
        # tool's own validation would reject it before the body runs.
        rescued = False
        raw, rescued, salvage_goal = self._rescue_unparsed_input(raw)
        # LOAD-BEARING: a real tool's TypeAdapter raises
        # unexpected_keyword_argument if 'goal' leaks through, so strip it
        # from a COPY of the arguments before forwarding.
        goal = raw.pop(GOAL_KEY, None)
        if goal is None:
            goal = salvage_goal
        cleaned_ctx = context.copy(
            message=context.message.model_copy(update={"arguments": raw})
        )
        client_name, client_version = self._client_fields(context)
        status, is_error, error, result_obj = "success", False, None, None
        t0 = time.perf_counter()
        try:
            result = await call_next(cleaned_ctx)
            is_error = bool(getattr(result, "is_error", False))
            if is_error:
                status = "error"
                error = self._extract_error_text(result)
            result_obj = self._extract_output(result)
            return result
        except Exception as exc:  # noqa: BLE001 - record then re-raise UNCHANGED
            status, is_error = "error", True
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            try:
                dur_ms = (time.perf_counter() - t0) * 1000.0
                record = self._build_record(
                    context.message.name,
                    goal,
                    raw,
                    result_obj,
                    status,
                    is_error,
                    error,
                    client_name=client_name,
                    client_version=client_version,
                    dur_ms=dur_ms,
                )
                if rescued:
                    record["input_rescued"] = True
                header = (
                    self._build_run_start()
                    if self._since_header >= HEADER_INTERVAL_RECORDS
                    else None
                )

                def _write() -> None:
                    # One sync call writes header-then-record so the header
                    # always precedes the record it anchors on disk.
                    if header is not None:
                        self.logger.write(header)
                    self.logger.write(record)

                # Offload the blocking write (and any rollover) off the event
                # loop; the handler's own lock keeps each append atomic across
                # worker threads.
                await anyio.to_thread.run_sync(_write)
                # Advance ONLY after the write landed. Anything that raises
                # above — including a CancelledError, which is a BaseException
                # and escapes both excepts — leaves the counter untouched, so
                # the next call re-emits rather than losing the header for the
                # whole run. Two concurrent calls can both see the header as
                # due and write it twice; harmless, because the header is a
                # constant and every emission is byte-identical.
                self._since_header = 1 if header is not None else self._since_header + 1
            except Exception:  # noqa: BLE001 - logging must never break the call
                pass

    # -- helpers ---------------------------------------------------------- #

    @staticmethod
    def _rescue_unparsed_input(raw: dict[str, Any]) -> tuple[dict[str, Any], bool, Any]:
        """Unwrap a ``{"__unparsedToolInput": {"raw": "<json>"}}`` envelope.

        Returns (arguments, rescued?, salvaged_goal). When the blob parses to a
        dict, the call proceeds with the real arguments; when it does not, the
        goal is still salvaged from the raw text so trace coverage stays whole.
        """
        envelope = raw.get("__unparsedToolInput")
        if set(raw) != {"__unparsedToolInput"} or not isinstance(envelope, dict):
            return raw, False, None
        blob = envelope.get("raw")
        if not isinstance(blob, str):
            return raw, False, None
        try:
            parsed = json.loads(blob)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return parsed, True, None
        match = _GOAL_IN_RAW_RE.search(blob)
        salvaged = None
        if match:
            try:
                salvaged = json.loads(f'"{match.group(1)}"')
            except (json.JSONDecodeError, ValueError):
                salvaged = match.group(1)
        return raw, False, salvaged

    def _build_run_start(self) -> dict[str, Any]:
        """One header record per server process: the constants and the join key.

        Carries what per-call records dropped in v2 (transport, result mode)
        plus server identity and the optional experiment tag — so A/B runs
        (skill on/off, server build) are self-describing. Takes no context and
        reads no clock: EVERY field must be true for the whole run, which is
        what makes the periodic re-emission idempotent. Client identity is not
        such a field (one process can serve several clients), so it is stamped
        per record instead.

        ``pid`` distinguishes concurrent server processes appending to one
        COLLECTION_LOG_PATH — the multi-worker case .env.sample warns about —
        and lets an operator match a run against OS process tooling. It is NOT
        a join key for the server's own stderr: nothing here configures a log
        format that emits a pid.
        """
        header: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "record": "run_start",
            # The RUN's start (process import time), not this write's time —
            # otherwise the run's only anchor would be "when it was first
            # used", which on an idle server is hours late and duplicates the
            # first call record's own ts.
            "ts": _RUN_STARTED_AT,
            "run_id": self.run_id,
            "pid": os.getpid(),
            "transport": self.transport,
            "server_version": self._server_version(),
            "result_mode": self.result_mode,
            "require_goal": self.require_goal,
            "max_field_bytes": self.logger.max_field_bytes,
            "max_result_bytes": getattr(self.logger, "max_result_bytes", None),
        }
        if self.run_tag:
            header["tag"] = self.run_tag
        return header

    def _client_fields(
        self, context: MiddlewareContext[Any]
    ) -> tuple[str | None, str | None]:
        """Best-effort ``(name, version)`` from MCP initialize; never raises.

        Kept as TWO fields rather than one ``"name/version"`` string: a client
        whose own name contains a slash (``acme/agent-sdk``) makes the joined
        form unsplittable, and a missing version becomes indistinguishable from
        a name that simply has no separator.

        Read per call, because the identity belongs to the caller and not to
        the process, but sanitized through a cache — the values are caller-
        supplied, so they get the same redact-and-bound treatment as every
        other logged field, and that work should not repeat once per call for
        the one client a stdio run ever sees. Returns ``(None, None)`` whenever
        the handshake is unreachable, which a sessionless transport makes
        routine: absent is expected here, not a defect.
        """
        try:
            fc = getattr(context, "fastmcp_context", None)
            session = getattr(getattr(fc, "request_context", None), "session", None)
            info = getattr(getattr(session, "client_params", None), "clientInfo", None)
            if info is None:
                return None, None
            raw_name = getattr(info, "name", None)
            raw_version = getattr(info, "version", None)
            key = (
                raw_name if isinstance(raw_name, str) else None,
                raw_version if isinstance(raw_version, str) else None,
            )
            if key[0] is None:
                return None, None
            cached = self._client_cache.get(key)
            if cached is None:
                cached = (
                    self._sanitize_client(key[0]),
                    self._sanitize_client(key[1]),
                )
                # An unbounded cache would be a memory leak on a hostile or
                # buggy client that varies its name per connection.
                if len(self._client_cache) < 64:
                    self._client_cache[key] = cached
            return cached
        except Exception:  # noqa: BLE001 - identity is best-effort
            return None, None

    @staticmethod
    def _sanitize_client(value: str | None) -> str | None:
        """Redact + bound one client identity string.

        clientInfo comes straight off the handshake, i.e. from the caller, and
        .env.sample promises every field is capped and Bearer/JWT-scrubbed.
        Capped at 128 rather than max_field_bytes: this rides on EVERY record,
        so a client that sends kilobytes must not eat the rotation budget.
        """
        if not value:
            return None
        out, _ = bounded_redact(value, 128)
        return out if isinstance(out, str) and out else None

    @staticmethod
    def _server_version() -> str | None:
        """The running server's version.

        ``importlib.metadata`` reports the *installed distribution* metadata,
        which goes stale under an editable install — real logs carried
        ``1.2.0`` while the checkout was several releases newer, making the
        field worse than useless for correlating behaviour to a version. A
        source checkout's ``pyproject.toml`` is authoritative when present, so
        it wins; installed deployments (no pyproject alongside the package)
        fall through to the metadata.
        """
        try:
            import tomllib
            from pathlib import Path

            # src/sas_mcp_server/telemetry.py -> repo root
            pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
            if pyproject.is_file():
                with pyproject.open("rb") as fh:
                    found = tomllib.load(fh).get("project", {}).get("version")
                if isinstance(found, str) and found:
                    return found
        except Exception:  # noqa: BLE001 - fall through to installed metadata
            pass
        try:
            from importlib.metadata import version

            return version("sas-mcp-server")
        except Exception:  # noqa: BLE001
            return None

    def _extract_output(self, result: Any) -> Any:
        # Cap joined text to a bounded prefix so a huge result is not fully
        # materialized here before bounded_redact enforces the exact cap.
        cap = getattr(self.logger, "max_result_bytes", 8192)
        try:
            sc = getattr(result, "structured_content", None)
            if sc is not None:
                return sc
            content = getattr(result, "content", None)
            if content:
                texts: list[str] = []
                total = 0
                for block in content:
                    text = getattr(block, "text", None)
                    if text is None:
                        continue
                    texts.append(text)
                    total += len(text)
                    if total > cap * 2:  # generous prefix; capped exactly later
                        break
                if texts:
                    return "\n".join(texts)
                # Non-text content (images, embedded files): describe the
                # blocks instead of logging result:null — a successful png
                # export must be distinguishable from a tool that returned
                # nothing (the verify loop is unmeasurable otherwise).
                blocks: list[dict[str, Any]] = []
                for block in content[:8]:
                    desc: dict[str, Any] = {"type": getattr(block, "type", type(block).__name__)}
                    data = getattr(block, "data", None)
                    if isinstance(data, (str, bytes)):
                        desc["bytes"] = len(data)
                    blocks.append(desc)
                if blocks:
                    return {"_content_blocks": blocks}
            return None
        except Exception:  # noqa: BLE001
            return None

    def _extract_error_text(self, result: Any) -> str | None:
        try:
            out = self._extract_output(result)
            if isinstance(out, str):
                return out
            if out is not None:
                return json.dumps(out, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return None

    @staticmethod
    def _result_shape(result_obj: Any) -> Any:
        """Content-free description of a result (used when results aren't logged).

        Key NAMES are schema, not data — and unlike a bare key count they
        distinguish an ``applied`` result from an ``apply_failed`` one.
        """
        if result_obj is None:
            return None
        try:
            if isinstance(result_obj, dict):
                keys = [k if isinstance(k, str) else str(k) for k in list(result_obj)[:24]]
                return {"_type": "object", "_keys": keys}
            if isinstance(result_obj, (list, tuple)):
                return {"_type": "array", "_items": len(result_obj)}
            if isinstance(result_obj, str):
                return {"_type": "string", "_bytes": len(result_obj.encode("utf-8"))}
            return {"_type": type(result_obj).__name__}
        except Exception:  # noqa: BLE001
            return {"_type": "unknown"}

    def _tool_outcome(self, result_obj: Any) -> dict[str, Any]:
        """Tool-DECLARED outcome fields from a structured result.

        This repo's tools return failures as data ({"status": "apply_failed",
        ...}), which the MCP layer records as success — without these fields
        every VA rejection in a trace looks like a win.
        """
        if not isinstance(result_obj, dict):
            return {}
        out: dict[str, Any] = {}
        tool_status = result_obj.get("status")
        if isinstance(tool_status, str):
            out["tool_status"] = tool_status
            out["is_tool_error"] = _is_tool_failure_status(tool_status)
            message = result_obj.get("message")
            if isinstance(message, str) and out["is_tool_error"]:
                # Scrub BEFORE bounding, or a host cut at the cap boundary
                # would partially leak.
                out["tool_message"], _ = bounded_redact(self._scrub_host(message), 512)
            for key in ("failed_operation_index", "error_count"):
                value = result_obj.get(key)
                if isinstance(value, int) and not isinstance(value, bool):
                    out[key] = value
        return out

    def _tool_tier(self, tool: str) -> int | None:
        """Tier of *tool*, resolved through the registry the tiers populate.

        Imported lazily and cached: ``tools`` pulls in ``config``, which raises
        when VIYA_ENDPOINT is unset — this module must stay importable without
        it (see the module docstring).
        """
        if self._tool_tiers is None:
            try:
                from .tools import TOOL_TIERS

                self._tool_tiers = TOOL_TIERS
            except Exception:  # noqa: BLE001 - tier stamping is best-effort
                self._tool_tiers = {}
        return self._tool_tiers.get(tool)

    @staticmethod
    def _args_hash(arguments: dict[str, Any]) -> str:
        """Stable 12-hex fingerprint of the (goal-stripped) arguments.

        Exact-retry runs — the loops a trace analysis needs to count — become
        a one-line groupby instead of ad-hoc rehashing.
        """
        try:
            canonical = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            canonical = str(arguments)
        return hashlib.sha256(canonical.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def _next_seq(self) -> int:
        """Monotonic call counter for the run — the trace's ordering key.

        Timestamps alone cannot order concurrent calls that share a millisecond,
        and a sequence per run stays meaningful with no session to count within.
        """
        self._seq += 1
        return self._seq

    def _scrub_host(self, text: Any) -> Any:
        """Mask the Viya host in free-text fields (raw HTTP errors embed it)."""
        if not isinstance(text, str) or not self._viya_host:
            return text
        return text.replace(self._viya_host, "[viya-host]")

    def _scrub_host_deep(self, value: Any) -> Any:
        """Recursively mask the Viya host in a (bounded) logged result.

        Full results in failures/always mode quote raw VA bodies and carry
        open_url — scrubbing only error/tool_message would leak the host in
        the very field the other two scrubbed. Runs on ALREADY-BOUNDED values
        (<= max_result_bytes), so the recursion is cheap.
        """
        if not self._viya_host:
            return value
        if isinstance(value, str):
            return value.replace(self._viya_host, "[viya-host]")
        if isinstance(value, dict):
            return {k: self._scrub_host_deep(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_host_deep(v) for v in value]
        return value

    _viya_host: str | None = None

    def _build_record(
        self,
        tool: str,
        goal: Any,
        arguments: dict[str, Any],
        result_obj: Any,
        status: str,
        is_error: bool,
        error: Any,
        # Keyword-only: these took over the positional slot that used to hold
        # session_id, and a stale str argument would otherwise be accepted
        # silently and logged as a client name.
        *,
        client_name: str | None = None,
        client_version: str | None = None,
        dur_ms: float,
    ) -> dict[str, Any]:
        field_bytes = self.logger.max_field_bytes
        result_bytes = getattr(self.logger, "max_result_bytes", field_bytes)

        args_val, args_trunc = bounded_redact(arguments, field_bytes)
        # goal is model-authored free text that can restate the user's request
        # (incl. anything they pasted) -> redact + bound it like other fields.
        goal_val, _ = (
            bounded_redact(goal, field_bytes) if goal is not None else (None, False)
        )
        outcome = self._tool_outcome(result_obj)
        log_full = self.result_mode == "always" or (
            self.result_mode == "failures" and (is_error or outcome.get("is_tool_error"))
        )
        if log_full:
            res_val, res_trunc = bounded_redact(result_obj, result_bytes)
            # Deep-scrub AFTER bounding: full results quote raw VA bodies and
            # carry open_url; the value is already capped so recursion is cheap.
            res_val = self._scrub_host_deep(res_val)
        else:
            res_val, res_trunc = self._result_shape(result_obj), False
        err_val, _ = (
            # Scrub BEFORE bounding — see _tool_outcome.
            bounded_redact(self._scrub_host(error), field_bytes) if error is not None else (None, False)
        )
        error_type, http_status = classify_error(err_val)
        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "seq": self._next_seq(),
            "tool": tool,
            # Which tier the tool belongs to, so usage rolls up per tier
            # (None only if a tool was registered outside register_tools).
            "tool_tier": self._tool_tier(tool),
            "goal": goal_val,
            "arguments": args_val,
            "arguments_truncated": args_trunc,
            "args_hash": self._args_hash(arguments),
            "result": res_val,
            "result_truncated": res_trunc,
            "result_logged": log_full,
            "status": status,
            # Present on EVERY record in every mode: v1 had `is_error`, v2 had
            # only `status` plus an optional `is_tool_error`, so "did this call
            # fail" needed different logic per version. One field, always here.
            "is_error": bool(is_error),
            "error": err_val,
            "error_type": error_type,
            "http_status": http_status,
            "duration_ms": dur_ms,
        }
        if dur_ms > SUSPECT_DURATION_MS:
            record["duration_suspect"] = True
        # Omitted rather than null when the handshake is unreachable: absent is
        # the common case on a sessionless transport, and a null on every line
        # would cost more than it says.
        if client_name:
            record["client_name"] = client_name
        if client_version:
            record["client_version"] = client_version
        record.update(outcome)
        return record


def install_telemetry(mcp: Any, transport: str) -> TelemetryMiddleware | None:
    """Add the telemetry middleware to ``mcp`` iff collection mode is enabled.

    config is imported LAZILY (config.py raises ConfigError when VIYA_ENDPOINT
    is unset, which would otherwise make this module unimportable in tests)."""
    from . import config

    if not config.COLLECTION_MODE:
        return None
    try:
        logger = UsageLogger(
            path=os.path.expanduser(config.COLLECTION_LOG_PATH),
            max_log_bytes=config.COLLECTION_MAX_LOG_BYTES,
            backup_count=config.COLLECTION_LOG_BACKUPS,
            max_field_bytes=config.COLLECTION_MAX_FIELD_BYTES,
            max_result_bytes=config.COLLECTION_MAX_RESULT_BYTES,
        )
    except OSError as exc:
        module_logger.warning(
            "Collection mode requested but log path unusable (%s); "
            "telemetry disabled",
            exc,
        )
        return None  # server runs exactly as before
    mw = TelemetryMiddleware(
        logger,
        require_goal=config.COLLECTION_REQUIRE_GOAL,
        transport=transport,
        log_results=config.COLLECTION_LOG_RESULTS,
        run_tag=config.COLLECTION_RUN_TAG,
    )
    try:
        from urllib.parse import urlparse

        mw._viya_host = urlparse(config.VIYA_ENDPOINT).netloc or None
    except Exception:  # noqa: BLE001 - scrubbing is best-effort
        mw._viya_host = None
    mcp.add_middleware(mw)
    return mw
