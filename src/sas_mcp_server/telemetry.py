# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Opt-in collection-mode telemetry middleware for the SAS MCP server.

Injects a required ``goal`` parameter into every published tool schema and logs
each tool call's input/output/session-id/goal/status/latency to a JSONL file.
Default OFF; requires ZERO changes to existing tools; works identically in HTTP
and stdio (it relies solely on ``context.message`` + a guarded
``context.fastmcp_context.session_id`` and NEVER calls get_http_request).

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
# and is_error dropped (constants/derivable — see the session_start record).
SCHEMA_VERSION = 2

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
        session_tag: str | None = None,
    ) -> None:
        self.logger = logger
        self.require_goal = require_goal
        self.transport = transport
        # bool accepted for back-compat: True -> always, False -> never.
        if isinstance(log_results, bool):
            log_results = "always" if log_results else "never"
        self.result_mode = log_results if log_results in ("always", "failures", "never") else "never"
        self.session_tag = session_tag
        # Per-process fallback session id (used only if the transport session
        # id is unavailable).
        self._proc_session = str(uuid4())
        # session_id -> next per-call sequence number; sessions already
        # announced via a session_start header record.
        self._seq: dict[str, int] = {}
        self._announced: set[str] = set()

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
        session_id = self._resolve_session(context)
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
                header = None
                # No await between the check and the add, so concurrent calls
                # on one loop cannot double-announce; on a failed write the id
                # is discarded so a later call retries the header.
                if session_id not in self._announced:
                    self._announced.add(session_id)
                    header = self._build_session_start(session_id, context)
                record = self._build_record(
                    context.message.name,
                    goal,
                    raw,
                    result_obj,
                    status,
                    is_error,
                    error,
                    session_id,
                    dur_ms,
                )
                if rescued:
                    record["input_rescued"] = True

                def _write() -> None:
                    # One sync call writes header-then-record so the header
                    # always precedes its session's first record on disk.
                    if header is not None:
                        self.logger.write(header)
                    self.logger.write(record)

                # Offload the blocking write (and any rollover) off the event
                # loop; the handler's own lock keeps each append atomic across
                # worker threads.
                try:
                    await anyio.to_thread.run_sync(_write)
                except Exception:
                    if header is not None:
                        self._announced.discard(session_id)
                    raise
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

    def _build_session_start(
        self, session_id: str, context: MiddlewareContext[Any]
    ) -> dict[str, Any]:
        """One header record per session: the constants and the join keys.

        Carries what per-call records dropped in v2 (transport, result mode)
        plus client/server identity and the optional experiment tag — so A/B
        runs (skill on/off, server build) are self-describing.
        """
        client_name, client_version = self._client_info(context)
        header: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "record": "session_start",
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "transport": self.transport,
            "client_name": client_name,
            "client_version": client_version,
            "server_version": self._server_version(),
            "result_mode": self.result_mode,
            "require_goal": self.require_goal,
            "max_field_bytes": self.logger.max_field_bytes,
            "max_result_bytes": getattr(self.logger, "max_result_bytes", None),
        }
        if self.session_tag:
            header["tag"] = self.session_tag
        return header

    @staticmethod
    def _client_info(context: MiddlewareContext[Any]) -> tuple[str | None, str | None]:
        """Best-effort MCP initialize clientInfo (name, version); never raises."""
        try:
            fc = getattr(context, "fastmcp_context", None)
            session = getattr(getattr(fc, "request_context", None), "session", None)
            info = getattr(getattr(session, "client_params", None), "clientInfo", None)
            if info is not None:
                return getattr(info, "name", None), getattr(info, "version", None)
        except Exception:  # noqa: BLE001 - identity is best-effort
            pass
        return None, None

    @staticmethod
    def _server_version() -> str | None:
        try:
            from importlib.metadata import version

            return version("sas-mcp-server")
        except Exception:  # noqa: BLE001
            return None

    def _resolve_session(self, context: MiddlewareContext[Any]) -> str:
        fc = getattr(context, "fastmcp_context", None)
        if fc is not None:
            try:
                return fc.session_id
            except RuntimeError:
                pass
            except Exception:  # noqa: BLE001 - never let session read break us
                pass
        return self._proc_session

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

    def _next_seq(self, session_id: str) -> int:
        self._seq[session_id] = self._seq.get(session_id, 0) + 1
        return self._seq[session_id]

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
        session_id: str,
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
        record = {
            "schema_version": SCHEMA_VERSION,
            "ts": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "seq": self._next_seq(session_id),
            "tool": tool,
            "goal": goal_val,
            "arguments": args_val,
            "arguments_truncated": args_trunc,
            "args_hash": self._args_hash(arguments),
            "result": res_val,
            "result_truncated": res_trunc,
            "result_logged": log_full,
            "status": status,
            "error": err_val,
            "duration_ms": dur_ms,
        }
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
        session_tag=config.COLLECTION_SESSION_TAG,
    )
    try:
        from urllib.parse import urlparse

        mw._viya_host = urlparse(config.VIYA_ENDPOINT).netloc or None
    except Exception:  # noqa: BLE001 - scrubbing is best-effort
        mw._viya_host = None
    mcp.add_middleware(mw)
    return mw
