# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Authentication provider for the HTTP-mode server.

Split out of :mod:`sas_mcp_server.config`, which should read as a settings
module rather than carry behaviour. The proxy takes its one policy switch as a
constructor argument instead of reading a module global, so it depends on
nothing in ``config`` — that keeps the import one-directional (``config``
imports ``auth``) and makes the class testable on its own.
"""

import logging

from fastmcp.server.auth import OAuthProxy
from mcp.server.auth.provider import AccessToken

logger = logging.getLogger(__name__)


class PermissiveOAuthProxy(OAuthProxy):
    """OAuthProxy that optionally accepts raw upstream JWTs.

    When *allow_raw_bearer* is set, a bearer token that fails the standard MCP
    JWT swap (because it isn't a proxy-issued JWT) falls through to the
    configured ``token_verifier``. If the verifier accepts it (i.e. the token is
    a valid Viya JWT signed by the upstream JWKS), the request proceeds with the
    raw token used directly as the upstream credential.

    This lets PKCE clients and pre-authenticated programmatic clients hit the
    same MCP endpoint without conflict — the additive path only kicks in after
    the standard swap has already failed.

    NOTE on scope: the fallback trusts whatever ``token_verifier`` accepts. With
    the server's default ``JWTVerifier(jwks_uri=..., audience=[])`` that is any
    JWT the upstream SASLogon signed, including tokens minted for other clients,
    with no client registration and no consent step. Enable deliberately.
    """

    def __init__(self, *args, allow_raw_bearer: bool = False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._allow_raw_bearer = allow_raw_bearer

    async def load_access_token(self, token: str) -> AccessToken | None:
        validated = await super().load_access_token(token)
        if validated is not None:
            return validated
        if not self._allow_raw_bearer:
            return None
        raw = await self._token_validator.verify_token(token)
        if raw is not None:
            logger.info(
                "Accepted raw bearer token (ALLOW_RAW_BEARER=true); "
                "bypassing MCP JWT swap"
            )
        return raw
