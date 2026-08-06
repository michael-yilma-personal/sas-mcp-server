# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Tests for the HTTP-mode MCP server: auth middleware, health route, the
token getter, and the AuthenticationError type.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from mcp.server.auth.provider import AccessToken

from sas_mcp_server import config, mcp_server
from sas_mcp_server.mcp_server import AuthenticationError


@pytest.fixture(autouse=True)
def _force_auth_enabled():
    with patch.object(mcp_server, "AUTH_ENABLED", True):
        yield


def test_authentication_error():
    """AuthenticationError carries its message and renders a prefixed string."""
    error = AuthenticationError("Test error message")
    assert error.message == "Test error message"
    assert str(error) == "AuthenticationError: Test error message"


@pytest.mark.asyncio
async def test_lifespan_cleans_up_sessions_on_shutdown():
    """The HTTP server lifespan tears down warm compute sessions on exit."""
    with patch(
        "sas_mcp_server.mcp_server.shutdown_session_cache", new=AsyncMock()
    ) as mock_shutdown:
        async with mcp_server._lifespan(mcp_server.mcp):
            mock_shutdown.assert_not_awaited()
        mock_shutdown.assert_awaited_once()


# ---------------------------------------------------------------------------
# _http_get_token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_get_token_present():
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value="VIYATOK")
    assert await mcp_server._http_get_token(ctx) == "VIYATOK"


@pytest.mark.asyncio
async def test_http_get_token_missing_raises():
    ctx = MagicMock()
    ctx.get_state = AsyncMock(return_value=None)
    with pytest.raises(AuthenticationError):
        await mcp_server._http_get_token(ctx)


# ---------------------------------------------------------------------------
# health_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_ok():
    resp = await mcp_server.health_check(MagicMock())
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body))
    assert body["status"] == "healthy"
    assert body["service"] == "sas-viya-execution-mcp"


# ---------------------------------------------------------------------------
# AuthMiddleware.on_call_tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_middleware_valid_bearer_sets_state():
    mw = mcp_server.AuthMiddleware()
    req = MagicMock()
    req.headers.get.return_value = "Bearer CLIENTJWT"
    info = MagicMock()
    info.token = "VIYATOK"
    ctx = MagicMock()
    ctx.fastmcp_context.set_state = AsyncMock()
    call_next = AsyncMock(return_value="RESULT")

    with patch("sas_mcp_server.mcp_server.get_http_request", return_value=req), \
         patch.object(mcp_server.viya_auth, "load_access_token",
                      AsyncMock(return_value=info)) as mock_load:
        result = await mw.on_call_tool(ctx, call_next)

    assert result == "RESULT"
    mock_load.assert_awaited_once_with("CLIENTJWT")
    ctx.fastmcp_context.set_state.assert_awaited_once_with("access_token", "VIYATOK")
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_auth_middleware_no_header_raises():
    mw = mcp_server.AuthMiddleware()
    req = MagicMock()
    req.headers.get.return_value = None
    call_next = AsyncMock()

    with patch("sas_mcp_server.mcp_server.get_http_request", return_value=req), pytest.raises(AuthenticationError):
        await mw.on_call_tool(MagicMock(), call_next)

    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_auth_middleware_swap_failure_does_not_set_state():
    mw = mcp_server.AuthMiddleware()
    req = MagicMock()
    req.headers.get.return_value = "Bearer X"
    ctx = MagicMock()
    ctx.fastmcp_context.set_state = AsyncMock()
    call_next = AsyncMock(return_value="R")

    with patch("sas_mcp_server.mcp_server.get_http_request", return_value=req), \
         patch.object(mcp_server.viya_auth, "load_access_token",
                      AsyncMock(return_value=None)):
        result = await mw.on_call_tool(ctx, call_next)

    assert result == "R"
    ctx.fastmcp_context.set_state.assert_not_awaited()
    call_next.assert_awaited_once_with(ctx)


# ---------------------------------------------------------------------------
# Real-HTTP auth stack (field finding: this layer had no non-mocked coverage)
# ---------------------------------------------------------------------------
#
# These drive mcp_server.app — the exact ASGI app uvicorn serves — through
# httpx's ASGITransport, so the starlette AuthenticationMiddleware,
# BearerAuthBackend, and PermissiveOAuthProxy.load_access_token all run for
# real. Only the innermost JWKS verifier is faked (no live Viya in CI).

_INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "id": 1,
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "auth-test", "version": "0"},
    },
}


def _mcp_headers(token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _post_initialize(token: str | None) -> httpx.Response:
    app = mcp_server.app
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://testserver") as client,
    ):
        return await client.post("/mcp", headers=_mcp_headers(token), json=_INIT_PAYLOAD)


@pytest.mark.asyncio
async def test_http_raw_bearer_accepted_end_to_end():
    """With ALLOW_RAW_BEARER on, a raw upstream JWT authenticates a real /mcp request.

    The token is not a proxy-issued JWT, so the standard swap fails and the
    request is only accepted because the raw-bearer fallback reaches the
    upstream token verifier through the real starlette middleware chain.
    """
    verifier = AsyncMock(
        return_value=AccessToken(token="RAWJWT", client_id="test", scopes=["openid"], expires_at=None)
    )
    with patch.object(config, "ALLOW_RAW_BEARER", True), \
         patch.object(config.viya_auth, "_token_validator") as validator:
        validator.verify_token = verifier
        resp = await _post_initialize("RAWJWT")
    assert resp.status_code == 200
    verifier.assert_awaited_once_with("RAWJWT")


@pytest.mark.asyncio
async def test_http_raw_bearer_rejected_when_disabled():
    """With ALLOW_RAW_BEARER off (the default), the same raw JWT is refused with 401."""
    verifier = AsyncMock(
        return_value=AccessToken(token="RAWJWT", client_id="test", scopes=["openid"], expires_at=None)
    )
    with patch.object(config, "ALLOW_RAW_BEARER", False), \
         patch.object(config.viya_auth, "_token_validator") as validator:
        validator.verify_token = verifier
        resp = await _post_initialize("RAWJWT")
    assert resp.status_code == 401
    assert "invalid_token" in resp.headers.get("www-authenticate", "")
    verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_no_bearer_is_401():
    """A /mcp request with no Authorization header is refused by the HTTP auth layer."""
    resp = await _post_initialize(None)
    assert resp.status_code == 401
