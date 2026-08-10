# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Side-effect-free environment-variable helpers shared across modules.

This module deliberately performs no I/O and imports nothing from the rest of
the package, so it is safe to import from the lightweight ``auth_login`` CLI
without triggering the server configuration in :mod:`sas_mcp_server.config`.
"""

import logging
import os

_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def env_bool(name: str, default: bool) -> bool:
    """Parse a boolean-ish environment variable.

    Recognises ``true/1/yes/on`` and ``false/0/no/off`` (case-insensitive).
    Returns *default* when the variable is unset or holds an unrecognised value.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return default


def parse_log_results(raw: str | None) -> str:
    """Map a ``COLLECTION_LOG_RESULTS`` value to ``never | failures | always``.

    Unset defaults to ``failures``. With shape-only results a success and a
    tool-declared failure are indistinguishable — both log as
    ``{"_type": "object", "_keys": [...]}`` — so a ``never`` default made the
    highest-value records (why a call failed) unreadable. Failure bodies are
    also the least likely to carry table rows, so this keeps the privacy
    posture that matters while making the log diagnostic.

    The boolean spellings are back-compat aliases and reuse the same sets
    :func:`env_bool` accepts, so the two never drift apart.
    """
    val = (raw or "").strip().lower()
    if not val:
        return "failures"  # unset -> the diagnostic default
    if val in ("failures", "failure"):
        return "failures"
    if val == "always" or val in _TRUE:
        return "always"
    if val == "never" or val in _FALSE:
        return "never"
    logging.getLogger(__name__).warning(
        "Unrecognized COLLECTION_LOG_RESULTS=%r; falling back to 'never'.", raw
    )
    return "never"
