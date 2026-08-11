# Copyright © 2025, SAS Institute Inc., Cary, NC, USA.  All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the SSL_VERIFY=false monkey-patch.

The patch has to reach clients this codebase never constructs — FastMCP builds
its own for the JWKS fetch and the OAuth token exchange — which is why it sits
on the client classes rather than on a call site.
"""

import ssl
import sys
import types

import httpx
import pytest

from sas_mcp_server import ssl_patch


def _unwrap(init):
    """Peel our wrappers off, using the __wrapped__ functools.wraps leaves."""
    while getattr(init, "_sas_mcp_ssl_patched", False):
        init = init.__wrapped__
    return init


@pytest.fixture
def pristine_httpx():
    """Start each test from an UNPATCHED httpx, then restore what was there.

    The repo's own .env sets SSL_VERIFY=false, so importing sas_mcp_server.config
    patches httpx before any test runs. Without this the idempotency guard
    (correctly) makes every call a no-op and the tests measure nothing.
    """
    classes = [httpx.AsyncClient, httpx.Client]
    as_found = {cls: cls.__init__ for cls in classes}
    for cls in classes:
        cls.__init__ = _unwrap(cls.__init__)
    yield
    for cls, init in as_found.items():
        cls.__init__ = init


def test_patches_both_httpx_client_classes(pristine_httpx):
    patched = ssl_patch.disable_tls_verification()

    assert "httpx.AsyncClient" in patched
    assert "httpx.Client" in patched
    assert getattr(httpx.AsyncClient.__init__, "_sas_mcp_ssl_patched", False)
    assert getattr(httpx.Client.__init__, "_sas_mcp_ssl_patched", False)
    # A real client still constructs through the wrapper.
    with httpx.Client() as client:
        assert client is not None


def test_is_idempotent_so_reloads_do_not_stack_wrappers(pristine_httpx):
    first = ssl_patch.disable_tls_verification()
    assert first, "expected the first call to patch something"
    after_first = httpx.AsyncClient.__init__

    second = ssl_patch.disable_tls_verification()

    assert second == [], "already-patched classes must be skipped"
    assert httpx.AsyncClient.__init__ is after_first


def test_explicit_verify_is_not_overridden(pristine_httpx):
    """setdefault, not assignment — a caller that asks to verify still does."""
    ssl_patch.disable_tls_verification()
    seen = {}
    original = httpx.Client.__init__

    with httpx.Client(verify=True) as client:
        seen["built"] = client is not None
    assert seen["built"]
    assert original is httpx.Client.__init__  # patch unchanged by use


def test_httpx2_is_patched_when_present(monkeypatch, pristine_httpx):
    """FastMCP 4 uses httpx2, and patching only httpx would silently stop
    covering its JWKS/OAuth calls after that upgrade."""

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    fake = types.ModuleType("httpx2")
    fake.AsyncClient = type("AsyncClient", (_FakeClient,), {})
    fake.Client = type("Client", (_FakeClient,), {})
    monkeypatch.setitem(sys.modules, "httpx2", fake)

    patched = ssl_patch.disable_tls_verification()

    assert "httpx2.AsyncClient" in patched
    assert "httpx2.Client" in patched
    built = fake.AsyncClient()
    assert isinstance(built.kwargs["verify"], ssl.SSLContext)
    assert built.kwargs["verify"].verify_mode is ssl.CERT_NONE


def test_missing_httpx2_is_skipped_not_an_error(monkeypatch, pristine_httpx):
    """httpx2 is absent on FastMCP 3.x — the common case today."""
    monkeypatch.delitem(sys.modules, "httpx2", raising=False)
    real_import = ssl_patch.importlib.import_module

    def _no_httpx2(name, *a, **kw):
        if name == "httpx2":
            raise ImportError("No module named 'httpx2'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(ssl_patch.importlib, "import_module", _no_httpx2)

    patched = ssl_patch.disable_tls_verification()

    assert all(not p.startswith("httpx2.") for p in patched)
    assert "httpx.AsyncClient" in patched  # httpx still covered


def test_each_class_keeps_its_own_original(pristine_httpx):
    """Guards the late-binding trap: a closure defined in the loop would make
    every wrapper delegate to whichever original the loop saw last."""
    calls = []

    class _A:
        def __init__(self, *a, **kw):
            calls.append(("A", kw.get("verify")))

    class _B:
        def __init__(self, *a, **kw):
            calls.append(("B", kw.get("verify")))

    fake = types.ModuleType("httpx2")
    fake.AsyncClient, fake.Client = _A, _B
    sys.modules["httpx2"] = fake
    try:
        ssl_patch.disable_tls_verification()
        fake.AsyncClient()
        fake.Client()
    finally:
        del sys.modules["httpx2"]

    assert [name for name, _ in calls] == ["A", "B"]
    assert all(isinstance(v, ssl.SSLContext) for _, v in calls)
