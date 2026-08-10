# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Frozen vocabularies and constants for collection-mode telemetry.

Configuration only — no behaviour. Split from ``telemetry_helpers`` (the
functions) and ``telemetry.py`` (the middleware) so the values a maintainer
tunes sit in one place, matching the ``*_registry`` / ``*_helpers`` split used
by the report-authoring and FedSQL modules.
"""

import re
from typing import Any

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

# Re-emit the run_start header every N call records. Sized so a header survives
# in any retained window: at the ~630 bytes/record measured on real logs, the
# 10 MiB default rotation holds ~16k records, so 1000 gives ~16 anchors per file
# at ~0.1% overhead. Rotation is not the only truncation — operators tail and
# split these files too.
HEADER_INTERVAL_RECORDS = 1000

# A call this long is almost never a real query: it is a hung call, or a host
# that slept mid-call (perf_counter keeps counting across suspend). Real logs
# carried entries up to 18.8 HOURS, silently poisoning every latency
# percentile. Flagged rather than dropped — the outlier itself is a signal.
SUSPECT_DURATION_MS = 600_000.0

# Caps for fields that ride on EVERY record, kept tighter than the configurable
# per-field budget so a chatty client cannot eat the log rotation window.
CLIENT_FIELD_MAX_BYTES = 128
TOOL_MESSAGE_MAX_BYTES = 512

# Statuses this repo's tools use to declare a failure AS DATA (the MCP layer
# sees success). Logging them per record turns 'success' rows into an honest
# funnel — real tool-level failure rates, apply_failed clustering. The pattern
# groups cover the repo's full status inventory (grep '"status": "' to re-audit
# when adding tools): *_failed, invalid_*, unknown_*, missing_*, not_*,
# *_not_found/_not_supported/_not_global, plus the explicit stragglers.
TOOL_FAILURE_STATUSES = frozenset(
    {"error", "export_too_large", "file_unreadable", "file_upload_disabled", "no_active_session"}
)
TOOL_FAILURE_PREFIXES = ("invalid_", "unknown_", "missing_", "not_")
TOOL_FAILURE_SUFFIXES = ("_failed", "_not_found", "_not_supported", "_not_global")

# Free-text exception/HTTP noise -> a low-cardinality class analysis can group
# by. Ordered: the first match wins, so specific classes precede generic ones.
ERROR_TYPE_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bValidationError\b", re.I), "validation"),
    (re.compile(r"\bAuthenticationError\b|\b401\b|\bUnauthorized\b", re.I), "auth"),
    (re.compile(r"\b403\b|\bForbidden\b", re.I), "permission"),
    (re.compile(r"getaddrinfo|ConnectError|ConnectTimeout|Name or service not known", re.I), "network"),
    (re.compile(r"\bTimeout\b|\bTimedOut\b", re.I), "timeout"),
    (re.compile(r"Server error '5\d\d|\b5\d\d Internal Server Error\b", re.I), "server_error"),
    (re.compile(r"Client error '4\d\d|\b4\d\d\b", re.I), "client_error"),
    (re.compile(r"\bToolError\b", re.I), "tool"),
)
HTTP_STATUS_RE = re.compile(r"\b(?:error ')?([45]\d{2})\b")

# Salvages the goal from a malformed argument envelope (see
# telemetry_helpers.rescue_unparsed_input).
GOAL_IN_RAW_RE = re.compile(r'"goal"\s*:\s*"((?:[^"\\]|\\.)*)"')

# Injected into every published tool schema.
GOAL_SCHEMA: dict[str, Any] = {
    "type": "string",
    "description": (
        "Before the other arguments, state in ONE sentence WHY you are calling "
        "THIS specific tool for the user's current request — the "
        "underlying goal it serves."
    ),
}
