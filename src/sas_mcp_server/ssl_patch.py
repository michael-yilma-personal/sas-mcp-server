# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""``SSL_VERIFY=false`` support for self-signed Viya certificates.

Kept out of :mod:`sas_mcp_server.config`, which should read as settings rather
than carry behaviour.

The mechanism is a monkey-patch on the HTTP client constructors, because the
permissive context has to reach clients this codebase never builds — notably the
one FastMCP creates internally for the JWKS fetch and the OAuth token exchange.

**Both httpx and httpx2 are patched.** FastMCP 3.x builds on ``httpx``; FastMCP
4 moved to ``httpx2``, a fork, and dropped the ``httpx`` dependency entirely.
Patching only ``httpx`` would therefore keep working for *our* Viya calls while
silently ceasing to cover FastMCP's own HTTPS calls on a major upgrade — and the
failure would surface inside the auth layer as a 401 or a connect error, which
is about the worst place to debug one. ``httpx2`` is simply skipped when it is
not installed, so this is safe to run on 3.x today.
"""

import functools
import importlib
import logging
import ssl
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

# Client classes to patch, per module. Both are constructed by libraries we do
# not control, so the patch has to sit on the class rather than on a call site.
_CLIENT_CLASSES = ("AsyncClient", "Client")
_HTTP_MODULES = ("httpx", "httpx2")


def _permissive_context() -> ssl.SSLContext:
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def _patched_init(original: Callable[..., Any], context: ssl.SSLContext) -> Callable[..., Any]:
    """Wrap a client ``__init__`` so ``verify`` defaults to *context*.

    Built by a factory rather than defined in the loop: a closure defined inline
    would capture the loop variable by reference and every patch would end up
    delegating to the last original it saw.
    """

    @functools.wraps(original)
    def wrapper(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("verify", context)
        original(self, *args, **kwargs)

    wrapper._sas_mcp_ssl_patched = True  # type: ignore[attr-defined]
    return wrapper


def disable_tls_verification() -> list[str]:
    """Make every httpx/httpx2 client default to an unverified TLS context.

    Idempotent: each class carries a ``_sas_mcp_ssl_patched`` marker, so a
    module reload (which tests do) cannot stack wrappers on top of each other
    until outbound connections break.

    Returns the dotted names patched, for logging and tests.
    """
    context = _permissive_context()
    patched: list[str] = []
    for module_name in _HTTP_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue  # httpx2 is absent until FastMCP 4; nothing to do
        for class_name in _CLIENT_CLASSES:
            client_cls = getattr(module, class_name, None)
            if client_cls is None:
                continue
            if getattr(client_cls.__init__, "_sas_mcp_ssl_patched", False):
                continue
            client_cls.__init__ = _patched_init(client_cls.__init__, context)
            patched.append(f"{module_name}.{class_name}")
    if patched:
        logger.warning(
            "SSL_VERIFY=false: TLS certificate verification is DISABLED for %s. "
            "This covers the server's own Viya calls AND the OAuth token "
            "exchange; use a trusted certificate where you can.",
            ", ".join(patched),
        )
    return patched
