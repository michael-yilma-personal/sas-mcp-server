#!/usr/bin/env python3
"""Register (or re-register) the sas-mcp OAuth client on a SAS Viya instance."""

import getpass
import os
import ssl

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
        "access-token-validity": 36000,
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


def main():
    if not VIYA_ENDPOINT:
        print("Error: VIYA_ENDPOINT is not set. Check your .env file.")
        return

    print(f"Viya endpoint: {VIYA_ENDPOINT}")
    print(f"Client ID: {CLIENT_ID}")
    for uri in redirect_uris():
        print(f"Redirect URI: {uri}")
    print()

    username = input("Viya admin username: ")
    password = getpass.getpass("Viya admin password: ")

    print("\nAuthenticating...")
    token = get_bearer_token(VIYA_ENDPOINT, username, password)
    print("Authenticated successfully.\n")

    # Delete existing client if present, then re-register
    delete_client(VIYA_ENDPOINT, token, CLIENT_ID)
    register_client(VIYA_ENDPOINT, token, CLIENT_ID, redirect_uris())

    # Verify the registration
    print("\nVerifying registration...")
    resp = httpx.get(
        f"{VIYA_ENDPOINT}/SASLogon/oauth/clients/{CLIENT_ID}",
        headers={"Authorization": f"Bearer {token}"},
        verify=ssl_context,
    )
    if resp.status_code == 200:
        client_data = resp.json()
        print(f"  client_id: {client_data.get('client_id')}")
        print(f"  scope: {client_data.get('scope')}")
        print(f"  authorized_grant_types: {client_data.get('authorized_grant_types')}")
        print(f"  redirect_uri: {client_data.get('redirect_uri')}")
        print(f"  autoapprove: {client_data.get('autoapprove')}")
        print(f"  allowpublic: {client_data.get('allowpublic')}")
    else:
        print(f"  Failed to verify: {resp.status_code} {resp.text}")


if __name__ == "__main__":
    main()
