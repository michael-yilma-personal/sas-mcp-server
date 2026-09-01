# Copyright © 2026, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the optional Tier 9 Code Assistant integration."""

from fastmcp import Client, FastMCP

from conftest import _make_mock_response
from sas_mcp_server import tools
from sas_mcp_server.tools import code_assistant


async def _tool_names(*, read_only: bool = False) -> set[str]:
    mcp = FastMCP("code-assistant-test")

    async def get_token(ctx):
        return "test-token"

    tools.register_tools(mcp, get_token, tiers="9", read_only=read_only)
    async with Client(mcp) as client:
        return {tool.name for tool in await client.list_tools()}


_TIER_NINE_TOOLS = {
    "code_assistant_get_doc_answer",
    "code_assistant_explain_code",
    "code_assistant_generate_code",
    "code_assistant_format_code",
    "code_assistant_add_comment",
    "code_assistant_find_problems",
    "code_assistant_show_examples",
    "code_assistant_refine_code",
    "code_assistant_analyze_log",
}


async def test_tier_nine_registers_all_tools_in_read_only_mode():
    """Every Tier 9 tool is classified read-only, so read-only mode withholds none of them."""
    assert await _tool_names(read_only=True) == _TIER_NINE_TOOLS
    assert await _tool_names() == _TIER_NINE_TOOLS


async def test_format_code_forwards_the_reference_rest_contract(mcp_server_with_mock_client, monkeypatch):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "data example;\nrun;"})
    monkeypatch.setattr(code_assistant, "VIYA_ENDPOINT", "https://test.viya.com")

    # The fixture's server was registered before Tier 9 was enabled, so add
    # just this optional tier using the same mock client and token provider.
    async def get_token(ctx):
        return "test-token"

    tools.register_tools(mcp, get_token, tiers="9")
    async with Client(mcp) as client:
        result = await client.call_tool(
            "code_assistant_format_code",
            {
                "code": "data example;run;",
                "language": "sas",
                "use_rag_for_sas": False,
                "product": ["SAS Studio"],
            },
        )

    assert result.data == {"formatted_code": "data example;\nrun;"}
    url = mock_client.post.call_args.args[0]
    payload = mock_client.post.call_args.kwargs["json"]
    assert url == "https://test.viya.com/genAiGateway/v1/copilotRequest"
    assert payload == {
        "applicationName": "SAS MCP Server",
        "copilot": {"id": "codeAssistant", "version": "v1"},
        "message": {
            "type": "userRequest",
            "content": "Format the selected code.",
            "context": {
                "type": "code",
                "commandId": "format",
                "useRAGForSAS": False,
                "languageId": "sas",
                "selectedText": "data example;run;",
                "currentFile": {
                    "name": "mcp-request.sas",
                    "language": "sas",
                    "content": "data example;run;",
                },
                "product": ["SAS Studio"],
            },
        },
    }


async def test_code_tools_forward_all_reference_command_ids(mcp_server_with_mock_client):
    mcp, mock_client = mcp_server_with_mock_client
    mock_client.post.return_value = _make_mock_response({"content": "assistant response"})

    async def get_token(ctx):
        return "test-token"

    tools.register_tools(mcp, get_token, tiers="9")
    async with Client(mcp) as client:
        calls = [
            ("code_assistant_generate_code", {"prompt": "Create a DATA step", "language": "sas"}),
            ("code_assistant_explain_code", {"code": "data x; run;", "language": "sas"}),
            ("code_assistant_add_comment", {"code": "data x; run;", "language": "sas"}),
            ("code_assistant_find_problems", {"code": "data x; run;", "language": "sas"}),
            ("code_assistant_show_examples", {"code": "Read a CSV file", "language": "sas"}),
            ("code_assistant_format_code", {"code": "data x;run;", "language": "sas"}),
            ("code_assistant_refine_code", {"code": "data x; run;", "language": "sas"}),
            ("code_assistant_analyze_log", {"log": "ERROR: Invalid option", "language": "sas"}),
        ]
        for tool_name, arguments in calls:
            await client.call_tool(tool_name, arguments)

    assert [call.kwargs["json"]["message"]["context"]["commandId"] for call in mock_client.post.call_args_list] == [
        "generate",
        "explain",
        "comment",
        "findProblems",
        "examples",
        "format",
        "refine",
        "analyzeLog",
    ]
