# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 0 — Compute Contexts & Code Execution tools."""

from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context, FastMCP

from ..config import COMPUTE_SESSION_ID, CONTEXT_NAME
from ..viya_client import contains_filter, get_paged_items, logger, make_client, return_items
from ..viya_utils import reset_cached_session, run_one_snippet
from ._common import make_session_helpers


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 0 (Compute Contexts & Code Execution) tools on *mcp*."""

    viya_session, _ = make_session_helpers(get_token)

    @mcp.tool()
    async def execute_sas_code(
        sas_code: str, ctx: Context, fresh_session: bool = False
    ) -> dict[str, str]:
        """
        Executes the provided SAS code in the Viya environment and returns information about the completed Job.
        This will create a job definition for the SAS code, execute it, and then retrieve the results.

        IMPORTANT — state persists between calls: the code runs in a reusable
        compute session that is kept warm and shared across calls (per user),
        so SAS state — WORK tables, macro variables, and assigned librefs —
        survives between successive ``execute_sas_code`` calls. A re-run can
        therefore see leftovers from earlier calls (e.g. a check that counts
        results twice). Pass ``fresh_session=True`` (or call
        ``reset_compute_session``) when the code must start from a clean slate.

        Tip: to reach CAS data, prefer ``libname casuser cas;`` (or a targeted
        ``caslib`` statement) over ``caslib _all_ assign;`` — on tenants with
        many caslibs the latter floods the log with assignment NOTEs.

        Args:
            sas_code (str): the SAS code snippet to be executed using the Viya Job Execution API Service
            fresh_session: When True, discard any cached compute session first
                so the code runs with no inherited SAS state (equivalent to
                calling ``reset_compute_session`` immediately before).

        Returns:
            A dictionary with four string fields describing the executed job:
            ``snippet_id`` (the job's snippet identifier), ``state`` (the final
            job state, e.g. ``completed``/``error``/``warning``), ``log`` (the
            full SAS log — execution details, notes, and any errors/warnings),
            and ``listing`` (the SAS listing output, i.e. the intended results
            when the code ran successfully).
        """
        logger.info("--- TOOL USED: execute_sas_code ---")
        token = await get_token(ctx)
        if fresh_session and not COMPUTE_SESSION_ID:
            # Fixed externally managed sessions can't be reset; run_one_snippet
            # creates a new cached session transparently after the reset.
            async with make_client(token) as client:
                await reset_cached_session(client, CONTEXT_NAME, token)
        return await run_one_snippet(sas_code, "1", token)

    @mcp.tool()
    async def list_compute_contexts(
        ctx: Context, limit: int = 50, start: int = 0, filter_name: str | None = None
    ) -> list[dict[str, Any]]:
        """List available compute contexts on the Viya environment."""
        if COMPUTE_SESSION_ID:
            return [{
                "name": f"fixed-session:{COMPUTE_SESSION_ID}",
                "description": (
                    "Fixed compute session mode enabled via COMPUTE_SESSION_ID; "
                    "context discovery is bypassed."
                ),
            }]
        async with viya_session("list_compute_contexts", ctx) as client:
            filters = contains_filter(filter_name)
            items, _ = await get_paged_items("/compute/contexts", client, limit=limit, start=start, filters=filters)
            return return_items(items, ["name", "description"])

    @mcp.tool()
    async def reset_compute_session(ctx: Context, compute_context_name: str | None = None) -> dict[str, str]:
        """Reset (delete) the cached compute session for a compute context.

        The server keeps one reusable SAS compute session per user and compute
        context so repeat calls skip the slow session spin-up; SAS state (WORK
        tables, macro variables, assigned librefs) therefore persists across
        ``execute_sas_code`` and ``list_compute_*`` calls. Call this to discard
        that state — the next compute tool call transparently creates a fresh
        session.

        Args:
            compute_context_name: Compute context whose session to reset.
                Defaults to the server's configured execution context (the one
                ``execute_sas_code`` uses).
        """
        context_name = compute_context_name or CONTEXT_NAME
        if COMPUTE_SESSION_ID:
            return {
                "status": "fixed_session_mode",
                "compute_context": context_name,
                "message": (
                    "COMPUTE_SESSION_ID is configured; the session is externally "
                    "managed and cannot be reset by this server."
                ),
            }
        logger.info("--- TOOL USED: reset_compute_session ---")
        token = await get_token(ctx)
        async with make_client(token) as client:
            sid = await reset_cached_session(client, context_name, token)
        if sid is None:
            return {
                "status": "no_active_session",
                "compute_context": context_name,
                "message": "No cached compute session to reset for this context.",
            }
        return {
            "status": "reset",
            "compute_context": context_name,
            "deleted_session": sid,
        }
