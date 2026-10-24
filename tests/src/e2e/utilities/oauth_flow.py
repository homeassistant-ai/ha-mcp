"""Drive the standalone OAuth entrypoint's public-client PKCE flow over HTTP."""

import base64
import hashlib
import secrets
import time
from urllib.parse import parse_qs, urlparse

import httpx

DISCOVERY_PATH = "/.well-known/oauth-authorization-server"


async def wait_for_discovery(http: httpx.AsyncClient, timeout: float = 30) -> None:
    """Wait until the server answers discovery, so the port is really bound.

    The launcher's lifespan signal fires before uvicorn binds its socket.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            if (await http.get(DISCOVERY_PATH)).status_code == 200:
                return
        except httpx.TransportError:
            if time.monotonic() > deadline:
                raise
        if time.monotonic() > deadline:
            raise TimeoutError(f"{DISCOVERY_PATH} did not answer within {timeout}s")


async def register_public_client(
    http: httpx.AsyncClient, redirect_uri: str, *, client_name: str
) -> str:
    """Register a public PKCE client and return its ``client_id``."""
    registered = await http.post(
        "/register",
        json={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "client_name": client_name,
        },
    )
    assert registered.status_code == 201, registered.text
    return registered.json()["client_id"]


async def consent(
    http: httpx.AsyncClient,
    client_id: str,
    redirect_uri: str,
    ha_token: str,
    *,
    state: str,
) -> tuple[str, str]:
    """Run authorize + consent for one user; return the code and its verifier."""
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    authorize = await http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "homeassistant",
        },
    )
    assert authorize.status_code == 302, authorize.text
    txn_id = parse_qs(urlparse(authorize.headers["location"]).query)["txn_id"][0]
    submitted = await http.post(
        "/consent", data={"txn_id": txn_id, "ha_token": ha_token}
    )
    assert submitted.status_code == 303, submitted.text
    callback = parse_qs(urlparse(submitted.headers["location"]).query)
    assert callback["state"] == [state]
    return callback["code"][0], verifier


async def exchange(
    http: httpx.AsyncClient,
    client_id: str,
    redirect_uri: str,
    code: str,
    verifier: str,
) -> httpx.Response:
    """Redeem ``code`` at the token endpoint; the caller asserts the outcome."""
    return await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
