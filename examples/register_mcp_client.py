#!/usr/bin/env python3
"""Register (or re-register) the sas-mcp OAuth client on a SAS Viya instance.

Reads VIYA_ENDPOINT, CLIENT_ID, HOST_PORT and MCP_BASE_URL from the
environment (or .env). Set VIYA_USERNAME and VIYA_PASSWORD to run without
prompts. Use --dry-run to see exactly what would be registered first.

This deletes and re-creates the client, which is privileged and briefly leaves
the deployment without one, so it is a deliberate manual step -- the running
server never does it.
"""

import argparse
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


def int_env(name: str, default: str) -> int:
    """A whole-number setting, with a readable failure instead of a traceback."""
    raw = os.getenv(name, default).strip() or default
    try:
        return int(raw)
    except ValueError:
        print(f"Error: {name} must be a whole number, got {raw!r}.")
        raise SystemExit(1) from None


VIYA_ENDPOINT = os.getenv("VIYA_ENDPOINT", "").rstrip("/")
CLIENT_ID = os.getenv("CLIENT_ID", "sas-mcp")
HOST_PORT = int_env("HOST_PORT", "8134")
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
# Deployments reachable at more than one URL (a tunnel beside a shared host,
# blue/green hostnames) can name the extra callbacks here, comma separated.
# Full redirect URIs, not base URLs.
EXTRA_REDIRECT_URIS = [
    uri.strip()
    for uri in os.getenv("MCP_EXTRA_REDIRECT_URIS", "").split(",")
    if uri.strip() and not uri.strip().startswith("#")
]
_ssl_verify_env = os.getenv("SSL_VERIFY", "true").lower() not in ("false", "0", "no")

if _ssl_verify_env:
    ssl_context = True
else:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

# The path the server actually serves its OAuth callback on.
CALLBACK_PATH = "/auth/callback"

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

# What each status means for *this* endpoint, so a failure reads as a cause
# rather than a stack trace.
HTTP_HINTS = {
    400: "SASLogon rejected the request; the body below usually names the field.",
    401: "Credentials were rejected, or the token is no longer valid.",
    403: "Authenticated, but this user may not manage OAuth clients (the clients.admin scope is required).",
    409: "A client with this ID already exists.",
}


def http_detail(exc: httpx.HTTPError) -> str:
    """A one-line explanation of an httpx failure, with Viya's own message."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return f"{exc} (no response -- check VIYA_ENDPOINT, network access and SSL_VERIFY)"
    parts = [f"HTTP {resp.status_code}"]
    hint = HTTP_HINTS.get(resp.status_code)
    if hint:
        parts.append(hint)
    body = (resp.text or "").strip()
    if body:
        parts.append(f"Viya said: {body[:500]}")
    return " -- ".join(parts)


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


def get_credentials() -> tuple[str, str] | None:
    """Viya admin credentials from the environment, else prompted for.

    Returns None when neither is possible, so an unattended run fails with a
    message instead of an EOFError from input().
    """
    username = os.getenv("VIYA_USERNAME", "").strip()
    password = os.getenv("VIYA_PASSWORD", "")

    if username and password:
        print(f"Using VIYA_USERNAME/VIYA_PASSWORD from the environment ({username}).")
        return username, password

    missing = " and ".join(
        name for name, value in (("VIYA_USERNAME", username), ("VIYA_PASSWORD", password)) if not value
    )

    if not sys.stdin.isatty():
        print(f"Error: no terminal to prompt on, and {missing} is not set.")
        print("Set VIYA_USERNAME and VIYA_PASSWORD to run this unattended.")
        return None

    # isatty() is not a reliable guard on its own -- a redirected or wrapped
    # stdin can claim to be a terminal and still have nothing to read -- so the
    # prompts themselves have to fail into the same message.
    try:
        if not username:
            username = input("Viya admin username: ")
        if not password:
            password = getpass.getpass("Viya admin password: ")
    except EOFError:
        print(f"\nError: no input available at the prompt, and {missing} is not set.")
        print("Set VIYA_USERNAME and VIYA_PASSWORD to run this unattended.")
        return None
    except KeyboardInterrupt:
        print("\nCancelled.")
        return None

    return username, password


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


def redirect_uris(extra: list[str] | None = None) -> list[str]:
    """Every OAuth callback the server may present to Viya.

    Localhost is always registered so the same client still works for a
    developer running the server locally, and MCP_BASE_URL is added when the
    server is also reachable at an external URL (reverse proxy, tunnel, k8s).
    SASLogon rejects any redirect_uri it was not registered with, so the list
    has to cover every URL in play -- hence MCP_EXTRA_REDIRECT_URIS and
    --redirect-uri for deployments answering on more than one.
    """
    candidates = [
        f"http://localhost:{HOST_PORT}{CALLBACK_PATH}",
        f"{MCP_BASE_URL}{CALLBACK_PATH}",
        *EXTRA_REDIRECT_URIS,
        *(extra or []),
    ]
    uris: list[str] = []
    for uri in candidates:
        cleaned = uri.strip()
        if cleaned and cleaned not in uris:
            uris.append(cleaned)
    return uris


def warn_unexpected_callbacks(uris: list[str]) -> None:
    """Flag a URI that does not end in the path the server actually serves."""
    for uri in uris:
        if not uri.endswith(CALLBACK_PATH):
            print(f"  Warning: {uri} does not end with {CALLBACK_PATH}, which is the path")
            print("           the server's callback is served on. Registering it anyway.")


def client_payload(client_id: str, redirect_uri: list[str]) -> dict:
    """The body sent to POST /oauth/clients.

    Shared with --dry-run so what is previewed is what would be sent. The
    client is public (allowpublic) because the MCP spec requires
    authorization-code flow with PKCE.
    """
    return {
        "client_id": client_id,
        "scope": ["openid"],
        "authorized_grant_types": ["authorization_code", "refresh_token"],
        "redirect_uri": redirect_uri,
        "autoapprove": True,
        "allowpublic": True,
        "access_token_validity": 36000,
    }


def register_client(base_url: str, token: str, client_id: str, redirect_uri: list[str]):
    """Register a new OAuth client with SASLogon."""
    resp = httpx.post(
        f"{base_url}/SASLogon/oauth/clients",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=client_payload(client_id, redirect_uri),
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
        print(f"  FAIL: could not read the client back: {http_detail(exc)}")
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register (or re-register) the MCP server's OAuth client in SAS Viya.",
        epilog="Set VIYA_USERNAME and VIYA_PASSWORD to run without prompts.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the redirect URIs and request body that would be sent, then exit "
        "without contacting Viya or changing anything.",
    )
    parser.add_argument(
        "--redirect-uri",
        action="append",
        default=[],
        metavar="URI",
        help="An additional full redirect URI to register. Repeatable. Adds to MCP_EXTRA_REDIRECT_URIS.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not _ssl_verify_env:
        print("WARNING: SSL_VERIFY is false, so TLS certificate verification is disabled.\n")

    if not VIYA_ENDPOINT:
        print("Error: VIYA_ENDPOINT is not set. Check your .env file.")
        return 1

    expected = redirect_uris(args.redirect_uri)
    print(f"Viya endpoint: {VIYA_ENDPOINT}")
    print(f"Client ID: {CLIENT_ID}")
    for uri in expected:
        print(f"Redirect URI: {uri}")
    warn_unexpected_callbacks(expected)
    print()

    if args.dry_run:
        print("--dry-run: nothing was contacted and nothing was changed.")
        print("This is the request body that would be sent to POST /SASLogon/oauth/clients:\n")
        print(indent(json.dumps(client_payload(CLIENT_ID, expected), indent=2), "  "))
        return 0

    credentials = get_credentials()
    if credentials is None:
        return 1
    username, password = credentials

    print("\nAuthenticating...")
    try:
        token = get_bearer_token(VIYA_ENDPOINT, username, password)
    except httpx.HTTPError as exc:
        print(f"Authentication failed: {http_detail(exc)}")
        return 1
    print("Authenticated successfully.\n")

    # Registering means deleting and re-creating the client, so a failure part
    # way through leaves the deployment with no client at all and nobody able
    # to sign in. This read is the only chance to record what to put back.
    try:
        existing = fetch_client(VIYA_ENDPOINT, token, CLIENT_ID)
    except httpx.HTTPError as exc:
        print(f"Could not read the existing client: {http_detail(exc)}")
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
        print(f"Could not delete the existing client: {http_detail(exc)}")
        print("Nothing was changed.")
        return 1

    try:
        register_client(VIYA_ENDPOINT, token, CLIENT_ID, expected)
    except httpx.HTTPError as exc:
        print(f"\nRegistration FAILED: {http_detail(exc)}")
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
