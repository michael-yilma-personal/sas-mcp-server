#!/usr/bin/env python3
"""Register (or re-register) the sas-mcp OAuth client on a SAS Viya instance."""

import getpass
import json
import os
import ssl
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from textwrap import indent

import httpx
from dotenv import load_dotenv

load_dotenv()

VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
HOST_PORT = int(os.getenv("HOST_PORT", "8134"))
# Mirrors the rule in src/sas_mcp_server/config.py: an empty value is the
# documented .env.sample default, and a value left as a commented-out
# placeholder is not a URL -- both fall back to localhost. Keep the two in
# sync. If they disagree, the running server presents Viya a redirect_uri that
# this script never registered, and SASLogon rejects the sign-in with
# "Invalid redirect ... did not match one of the registered values".
_mcp_base_url = os.getenv("MCP_BASE_URL", "").strip()
if not _mcp_base_url or _mcp_base_url.startswith("#"):
    _mcp_base_url = f"http://localhost:{HOST_PORT}"
MCP_BASE_URL = _mcp_base_url.rstrip("/")
_ssl_verify_env = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

if _ssl_verify_env:
    ssl_context = True
else:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

# Fields SASLogon accepts on POST /oauth/clients that are worth carrying over
# when rebuilding a client from its previous definition. GET never returns
# client_secret, so only a public client can be restored this way -- which is
# what this script registers (allowpublic, PKCE).
RESTORE_FIELDS = (
    "client_id",
    "scope",
    "authorized_grant_types",
    "redirect_uri",
    "autoapprove",
    "allowpublic",
    "access_token_validity",
    "refresh_token_validity",
    "authorities",
)

# Fields worth showing when reporting what Viya actually stored.
REPORTED_FIELDS = (
    "client_id",
    "scope",
    "authorized_grant_types",
    "redirect_uri",
    "autoapprove",
    "allowpublic",
    "access_token_validity",
)


def get_bearer_token(base_url: str, username: str, password: str) -> str:
    """Authenticate with SASLogon using the sas.cli client and return an access token."""
    resp = httpx.post(
        f"{base_url}/SASLogon/oauth/token",
        auth=("sas.cli", ""),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "password", "username": username, "password": password},
        verify=ssl_context,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_client(base_url: str, token: str, client_id: str) -> dict | None:
    """The client's current definition in Viya, or None if it isn't registered."""
    resp = httpx.get(
        f"{base_url}/SASLogon/oauth/clients/{client_id}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ssl_context,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def back_up_client(existing: dict) -> Path | None:
    """Write the pre-change definition to a file so it can be restored.

    The definition is printed to the console as well; this file is the copy
    that survives a closed terminal.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(tempfile.gettempdir()) / f"{existing.get('client_id', 'oauth-client')}-{stamp}.json"
    try:
        path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"  (could not write a backup file: {exc})")
        return None
    return path


def print_restore_hint(base_url: str, existing: dict, backup: Path | None) -> None:
    """Print exactly how to put the previous definition back."""
    payload = {field: existing[field] for field in RESTORE_FIELDS if field in existing}
    print("\nTo restore the previous registration, get an admin token and run:\n")
    print(f'  curl -X POST "{base_url}/SASLogon/oauth/clients" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -H "Authorization: Bearer $BEARER_TOKEN" \\')
    print(f"    -d '{json.dumps(payload)}'")
    if backup:
        print(f"\nThe same definition is saved at {backup}")


def delete_client(base_url: str, token: str, client_id: str) -> bool:
    """Delete an existing OAuth client. Returns True if deleted, False if not found."""
    resp = httpx.delete(
        f"{base_url}/SASLogon/oauth/clients/{client_id}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ssl_context,
    )
    if resp.status_code == 200:
        print(f"Deleted existing client '{client_id}'.")
        return True
    elif resp.status_code == 404:
        return False
    else:
        resp.raise_for_status()
        return False


def redirect_uris() -> list[str]:
    """Every OAuth callback the server may present to Viya.

    Localhost is always registered so the same client still works for a
    developer running the server locally, and MCP_BASE_URL is added when the
    server is also reachable at an external URL (reverse proxy, tunnel, k8s).
    SASLogon rejects any redirect_uri it was not registered with, so the list
    has to cover both.
    """
    uris = [f"http://localhost:{HOST_PORT}/auth/callback"]
    external = f"{MCP_BASE_URL}/auth/callback"
    if external not in uris:
        uris.append(external)
    return uris


def register_client(base_url: str, token: str, client_id: str, redirect_uri: list[str]):
    """Register a new OAuth client with SASLogon."""
    payload = {
        "client_id": client_id,
        "scope": ["openid"],
        "authorized_grant_types": ["authorization_code", "refresh_token"],
        "redirect_uri": redirect_uri,
        "autoapprove": True,
        "allowpublic": True,
        "access_token_validity": 36000,
    }
    resp = httpx.post(
        f"{base_url}/SASLogon/oauth/clients",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        verify=ssl_context,
    )
    resp.raise_for_status()
    print(f"Client '{client_id}' registered successfully.")
    for uri in redirect_uri:
        print(f"  Redirect URI: {uri}")
    print("  Scopes: openid")
    print("  Grant types: authorization_code, refresh_token")


def verify_registration(base_url: str, token: str, client_id: str, expected: list[str]) -> bool:
    """Read the client back and check Viya stored what was asked for.

    Printing the fields is not enough. The failure this guards against is Viya
    holding a redirect_uri the server never presents, which looks unremarkable
    in a console dump but rejects every sign-in.
    """
    print("\nVerifying registration...")
    try:
        client_data = fetch_client(base_url, token, client_id)
    except httpx.HTTPError as exc:
        print(f"  FAIL: could not read the client back: {exc}")
        return False

    if client_data is None:
        print(f"  FAIL: client '{client_id}' is not registered.")
        return False

    for field in REPORTED_FIELDS:
        print(f"  {field}: {client_data.get(field)}")

    registered = client_data.get("redirect_uri") or []
    if isinstance(registered, str):
        registered = [registered]

    missing = [uri for uri in expected if uri not in registered]
    if missing:
        print("\n  FAIL: Viya did not store these redirect URIs:")
        for uri in missing:
            print(f"    {uri}")
        print("  Sign-in will fail with 'Invalid redirect ... did not match one of the")
        print("  registered values' until they are registered.")
        return False

    if client_data.get("access_token_validity") is None:
        print("\n  Note: Viya reported no access_token_validity, so its default applies.")

    print("\n  OK: every expected redirect URI is registered.")
    return True


def main() -> int:
    if not VIYA_ENDPOINT:
        print("Error: VIYA_ENDPOINT is not set. Check your .env file.")
        return 1

    expected = redirect_uris()
    print(f"Viya endpoint: {VIYA_ENDPOINT}")
    print(f"Client ID: {CLIENT_ID}")
    for uri in expected:
        print(f"Redirect URI: {uri}")
    print()

    username = input("Viya admin username: ")
    password = getpass.getpass("Viya admin password: ")

    print("\nAuthenticating...")
    try:
        token = get_bearer_token(VIYA_ENDPOINT, username, password)
    except httpx.HTTPError as exc:
        print(f"Authentication failed: {exc}")
        return 1
    print("Authenticated successfully.\n")

    # Registering means deleting and re-creating the client, so a failure part
    # way through leaves the deployment with no client at all and nobody able
    # to sign in. This read is the only chance to record what to put back.
    try:
        existing = fetch_client(VIYA_ENDPOINT, token, CLIENT_ID)
    except httpx.HTTPError as exc:
        print(f"Could not read the existing client: {exc}")
        return 1

    backup = None
    if existing is None:
        print(f"No existing client '{CLIENT_ID}'; registering a new one.\n")
    else:
        print(f"Current registration for '{CLIENT_ID}' (about to be replaced):")
        print(indent(json.dumps(existing, indent=2), "  "))
        backup = back_up_client(existing)
        if backup:
            print(f"  Backed up to {backup}")
        print()

    try:
        delete_client(VIYA_ENDPOINT, token, CLIENT_ID)
    except httpx.HTTPError as exc:
        print(f"Could not delete the existing client: {exc}")
        print("Nothing was changed.")
        return 1

    try:
        register_client(VIYA_ENDPOINT, token, CLIENT_ID, expected)
    except httpx.HTTPError as exc:
        print(f"\nRegistration FAILED: {exc}")
        body = getattr(getattr(exc, "response", None), "text", "")
        if body:
            print(f"Viya said: {body}")
        if existing is not None:
            print(
                f"\nThe old client was already deleted, so '{CLIENT_ID}' does not exist "
                "right now\nand nobody can sign in until it is registered again."
            )
            print_restore_hint(VIYA_ENDPOINT, existing, backup)
        return 1

    return 0 if verify_registration(VIYA_ENDPOINT, token, CLIENT_ID, expected) else 1


if __name__ == "__main__":
    sys.exit(main())
