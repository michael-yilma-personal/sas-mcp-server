# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tier 9 - SAS Code Assistance & Documentation tools.

The Code Assistant copilot is reached over Viya's REST API (the GenAI Gateway
copilot endpoint) using the authenticated user's Viya bearer token; no separate
GenAI/LLM API key is required. No service credential or source code is persisted
here.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

import httpx
from fastmcp import Context, FastMCP

from ..config import VIYA_ENDPOINT
from ..viya_client import logger, make_client, raise_for_viya_status

_DEFAULT_PRODUCTS = ["SAS Studio", "SAS Studio with SAS Viya Platform Programming Documentation"]
_CODE_ASSISTANT_PATH = "/genAiGateway/v1/copilotRequest"
_CODE_ASSISTANT_APPLICATION_NAME = "SAS MCP Server"
_CODE_ASSISTANT_COPILOT_ID = "codeAssistant"
_CODE_ASSISTANT_COPILOT_VERSION = "v1"

# The GenAI Gateway validates UserRequest.content as a required, non-empty
# string. The copilot itself drives the action from context.commandId and
# treats content as an optional user message, so a short natural-language
# instruction per command satisfies the gateway without changing behaviour.
_COMMAND_PROMPTS = {
    "generate": "Generate code from the provided requirements.",
    "explain": "Explain the selected code.",
    "comment": "Add explanatory comments to the selected code.",
    "findProblems": "Find potential problems in the selected code.",
    "examples": "Show examples based on the provided code or requirements.",
    "format": "Format the selected code.",
    "refine": "Refine the selected code.",
    "analyzeLog": "Analyze the log and identify the cause of errors or warnings.",
}


def _envelope(message: dict[str, Any]) -> dict[str, Any]:
    """Wrap a copilot ``message`` in the GenAI Gateway request envelope.

    The Viya GenAI Gateway (``/genAiGateway/v1/copilotRequest``) routes to a
    registered copilot identified by ``copilot.id`` / ``copilot.version`` on
    behalf of ``applicationName``.
    """
    return {
        "applicationName": _CODE_ASSISTANT_APPLICATION_NAME,
        "copilot": {"id": _CODE_ASSISTANT_COPILOT_ID, "version": _CODE_ASSISTANT_COPILOT_VERSION},
        "message": message,
    }


def _content(response: httpx.Response, *, operation: str) -> str:
    """Validate and extract the textual ``content`` contract from the service."""
    raise_for_viya_status(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(f"The Code Assistant {operation} service returned invalid JSON.") from exc
    content: Any = None
    if isinstance(payload, dict):
        if isinstance(payload.get("content"), str):
            content = payload["content"]
        elif isinstance(payload.get("message"), dict) and isinstance(payload["message"].get("content"), str):
            content = payload["message"]["content"]
    if not isinstance(content, str) or not content.strip():
        raise ValueError(f"The Code Assistant {operation} service returned an empty response.")
    return content


async def _post(client: httpx.AsyncClient, payload: dict[str, Any], *, operation: str) -> str:
    response = await client.post(
        f"{VIYA_ENDPOINT}{_CODE_ASSISTANT_PATH}",
        json=payload,
    )
    return _content(response, operation=operation)


def _code_request(
    code: str,
    language: Literal["sas", "python", "r"],
    command_id: str,
    *,
    use_rag_for_sas: bool = True,
    product: list[str] | None = None,
) -> dict[str, Any]:
    """Build the documented request shape used by the Code Assistant REST API."""
    context: dict[str, Any] = {
        "type": "code",
        "commandId": command_id,
        "useRAGForSAS": use_rag_for_sas,
        "languageId": language,
        "selectedText": code,
        "currentFile": {"name": f"mcp-request.{language}", "language": language, "content": code},
    }
    if product:
        context["product"] = product
    content = _COMMAND_PROMPTS.get(command_id, f"Apply the {command_id} action to the selected code.")
    return _envelope({"type": "userRequest", "content": content, "context": context})


def register(mcp: FastMCP, get_token: Callable[[Context], Awaitable[str]]) -> None:
    """Register Tier 9 (SAS Code Assistance & Documentation) tools on *mcp*."""

    @mcp.tool(name="code_assistant_get_doc_answer")
    async def get_doc_answer(question: str, ctx: Context, product: list[str] | None = None) -> dict[str, str]:
        """Answer a SAS documentation question using the Code Assistant knowledge base."""
        if not question.strip():
            raise ValueError("question must not be empty.")
        logger.info("--- TOOL USED: code_assistant_get_doc_answer ---")
        token = await get_token(ctx)
        async with make_client(token) as client:
            answer = await _post(
                client,
                _envelope(
                    {
                        "type": "userRequest",
                        "content": question,
                        "context": {"type": "doc", "product": product or _DEFAULT_PRODUCTS},
                    }
                ),
                operation="documentation",
            )
        return {"answer": answer}

    async def _code_tool(
        code: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        command_id: str,
        *,
        tool_name: str,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> str:
        if not code.strip():
            raise ValueError("The input must not be empty.")
        # Log the registered tool name, not the copilot's camelCase commandId
        # (findProblems, analyzeLog), so the usage log matches list_tools.
        logger.info("--- TOOL USED: %s ---", tool_name)
        token = await get_token(ctx)
        async with make_client(token) as client:
            return await _post(
                client,
                _code_request(code, language, command_id, use_rag_for_sas=use_rag_for_sas, product=product),
                operation=command_id,
            )

    @mcp.tool(name="code_assistant_explain_code")
    async def explain_code(code: str, language: Literal["sas", "python", "r"], ctx: Context) -> dict[str, str]:
        """Explain SAS, Python, or R code and return a human-readable explanation."""
        explanation = await _code_tool(code, language, ctx, "explain", tool_name="code_assistant_explain_code")
        return {"explanation": explanation}

    @mcp.tool(name="code_assistant_generate_code")
    async def generate_code(
        prompt: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Generate SAS, Python, or R code from natural-language requirements."""
        generated_code = await _code_tool(
            prompt,
            language,
            ctx,
            "generate",
            tool_name="code_assistant_generate_code",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"generated_code": generated_code}

    @mcp.tool(name="code_assistant_format_code")
    async def format_code(
        code: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Format SAS, Python, or R code without intentionally changing its logic."""
        formatted_code = await _code_tool(
            code,
            language,
            ctx,
            "format",
            tool_name="code_assistant_format_code",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"formatted_code": formatted_code}

    @mcp.tool(name="code_assistant_add_comment")
    async def add_comment(
        code: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Add clear, production-quality comments while preserving source-code behavior."""
        commented_code = await _code_tool(
            code,
            language,
            ctx,
            "comment",
            tool_name="code_assistant_add_comment",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"commented_code": commented_code}

    @mcp.tool(name="code_assistant_find_problems")
    async def find_problems(
        code: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Find correctness, performance, maintainability, and style problems in code."""
        problems = await _code_tool(
            code,
            language,
            ctx,
            "findProblems",
            tool_name="code_assistant_find_problems",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"problems": problems}

    @mcp.tool(name="code_assistant_show_examples")
    async def show_examples(
        code: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Show three SAS, Python, or R examples based on code or requirements."""
        examples = await _code_tool(
            code,
            language,
            ctx,
            "examples",
            tool_name="code_assistant_show_examples",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"examples": examples}

    @mcp.tool(name="code_assistant_refine_code")
    async def refine_code(
        code: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Refine code for clarity and maintainability while preserving its behavior."""
        refined_code = await _code_tool(
            code,
            language,
            ctx,
            "refine",
            tool_name="code_assistant_refine_code",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"refined_code": refined_code}

    @mcp.tool(name="code_assistant_analyze_log")
    async def analyze_log(
        log: str,
        language: Literal["sas", "python", "r"],
        ctx: Context,
        use_rag_for_sas: bool = True,
        product: list[str] | None = None,
    ) -> dict[str, str]:
        """Analyze a SAS, Python, or R log and identify causes of errors and warnings."""
        analysis = await _code_tool(
            log,
            language,
            ctx,
            "analyzeLog",
            tool_name="code_assistant_analyze_log",
            use_rag_for_sas=use_rag_for_sas,
            product=product,
        )
        return {"analysis": analysis}
