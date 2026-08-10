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

import json
import logging
import os
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

from .helpers.telemetry_helpers import (
    args_hash,
    classify_error,
    raw_client_info,
    rescue_unparsed_input,
    result_shape,
    sanitize_client,
    scrub_host,
    scrub_host_deep,
    server_version,
    tool_outcome,
)
from .helpers.telemetry_registry import (
    GOAL_SCHEMA,
    HEADER_INTERVAL_RECORDS,
    SCHEMA_VERSION,
    SUSPECT_DURATION_MS,
)
from .usage_logger import GOAL_KEY, UsageLogger, bounded_redact

# Re-exported so `from sas_mcp_server.telemetry import ...` keeps working for
# callers and tests that predate the helpers split.
__all__ = [
    "GOAL_SCHEMA",
    "HEADER_INTERVAL_RECORDS",
    "SCHEMA_VERSION",
    "SUSPECT_DURATION_MS",
    "TelemetryMiddleware",
    "classify_error",
    "install_telemetry",
]

module_logger = logging.getLogger(__name__)

# ONE run identity per PROCESS, not per middleware instance: two middlewares in
# one process (a composed/mounted server, a re-import) must not look like two
# server runs sharing a pid. Stamped at import, which is server startup — so the
# header's ts answers "when did this server come up", not "when was it first
# used"; the two can be hours apart on an idle deployment.
#
# Everything else that was here — the schema version, failure-status
# vocabularies, error-classification rules and GOAL_SCHEMA — now lives in
# helpers/telemetry_registry.py, and the record-shaping functions in
# helpers/telemetry_helpers.py.
_RUN_ID = str(uuid4())
_RUN_STARTED_AT = datetime.now(UTC).isoformat()


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
        raw, rescued, salvage_goal = rescue_unparsed_input(raw)
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
            "server_version": server_version(),
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
        """Sanitized ``(name, version)`` for the calling client, memoized.

        Extraction and redaction live in ``telemetry_helpers``; what stays here
        is the cache, because it is per-middleware state. Read per call — the
        identity belongs to the caller, not the process — but sanitized once
        per distinct client, so the redact-and-bound work does not repeat for
        the single client a stdio run ever sees.
        """
        try:
            key = raw_client_info(context)
            if key[0] is None:
                return None, None
            cached = self._client_cache.get(key)
            if cached is None:
                cached = (sanitize_client(key[0]), sanitize_client(key[1]))
                # An unbounded cache would be a memory leak on a hostile or
                # buggy client that varies its name per connection.
                if len(self._client_cache) < 64:
                    self._client_cache[key] = cached
            return cached
        except Exception:  # noqa: BLE001 - identity is best-effort
            return None, None

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

    def _next_seq(self) -> int:
        """Monotonic call counter for the run — the trace's ordering key.

        Timestamps alone cannot order concurrent calls that share a millisecond,
        and a sequence per run stays meaningful with no session to count within.
        """
        self._seq += 1
        return self._seq

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
        outcome = tool_outcome(result_obj, self._viya_host)
        log_full = self.result_mode == "always" or (
            self.result_mode == "failures" and (is_error or outcome.get("is_tool_error"))
        )
        if log_full:
            res_val, res_trunc = bounded_redact(result_obj, result_bytes)
            # Deep-scrub AFTER bounding: full results quote raw VA bodies and
            # carry open_url; the value is already capped so recursion is cheap.
            res_val = scrub_host_deep(res_val, self._viya_host)
        else:
            res_val, res_trunc = result_shape(result_obj), False
        err_val, _ = (
            # Scrub BEFORE bounding — see _tool_outcome.
            bounded_redact(scrub_host(error, self._viya_host), field_bytes) if error is not None else (None, False)
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
            "args_hash": args_hash(arguments),
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
