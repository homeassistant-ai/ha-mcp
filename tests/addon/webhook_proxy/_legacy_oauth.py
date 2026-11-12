"""Helpers for the legacy OAuth tests: the proxy's own authorization server.

The legacy mode is the app (add-on)'s embedded OAuth 2.1 server with one
configured client. Core sets the proxy up from the config file ``start.py``
writes, and a client reaches the server over HTTP: the root ``/authorize`` and
``/token`` views, the discovery documents, and the webhook's bearer gate.
"""

from __future__ import annotations

import base64
import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient
from homeassistant.core import CoreState, HomeAssistant
from pytest_homeassistant.common import MockConfigEntry

from ._proxy import set_up_proxy, write_proxy_config
from ._variants import Variant

CLIENT_ID = "client-id-1234567890ABCDEF"
CLIENT_SECRET = "client-secret-very-secret"
WEBHOOK_ID = "mcp_webhook_id_aaaa"
REDIRECT_URI = "https://claude.ai/cb"
STATE = "abc123"
PUBLIC_BASE_URL = "https://legit.example"
# A well-formed target the proxy accepts; tests that never relay leave it
# unanswered.
UNUSED_TARGET = "http://127.0.0.1:9/private_zctpwlX7ZkIAr7oqdfLPxw"
# RFC 7636 §4.1: 43-128 characters from the unreserved set.
VERIFIER = "abcdefghij" * 5
WEBHOOK_ROUTE = "/api/webhook/{webhook_id}"


def legacy_config(
    *,
    target_url: str = UNUSED_TARGET,
    webhook_id: str = WEBHOOK_ID,
    client_id: str = CLIENT_ID,
    client_secret: str = CLIENT_SECRET,
    public_base_url: str | None = None,
) -> dict[str, Any]:
    """The proxy config ``start.py`` writes when legacy OAuth is on."""
    config: dict[str, Any] = {
        "target_url": target_url,
        "webhook_id": webhook_id,
        "oauth": {
            "mode": "legacy",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    }
    if public_base_url is not None:
        config["public_base_url"] = public_base_url
    return config


async def set_up_legacy(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    *,
    at_boot: bool = False,
    **config: Any,
) -> MockConfigEntry:
    """Write a legacy OAuth config and let Core set the proxy up.

    ``at_boot`` sets the entry up while Home Assistant is still starting, the
    one time Core binds new HTTP views cleanly; otherwise the setup happens
    mid-session, as when the app starts after Home Assistant.
    """
    write_proxy_config(fs_root, variant, legacy_config(**config))
    if at_boot:
        hass.set_state(CoreState.not_running)
    try:
        return await set_up_proxy(hass, variant)
    finally:
        hass.set_state(CoreState.running)


async def reload_with(
    hass: HomeAssistant,
    variant: Variant,
    fs_root: Path,
    entry: MockConfigEntry,
    **config: Any,
) -> None:
    """Rewrite the config file and reload the entry mid-session."""
    write_proxy_config(fs_root, variant, legacy_config(**config))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()


def bound_routes(hass: HomeAssistant) -> set[str]:
    """Every path Home Assistant's HTTP server routes."""
    return {resource.canonical for resource in hass.http.app.router.resources()}


def oauth_routes_added(hass: HomeAssistant, before: set[str]) -> set[str]:
    """Paths bound since ``before``, leaving out the MCP forwarding routes."""
    return {
        route
        for route in bound_routes(hass) - before
        if not route.startswith(f"{WEBHOOK_ROUTE}/")
    }


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def s256(verifier: str) -> str:
    """The RFC 7636 S256 code challenge for ``verifier``."""
    return b64url(hashlib.sha256(verifier.encode()).digest())


def token_claims(token: str) -> dict[str, Any]:
    """The claims a legacy token carries before its signature."""
    body = token.rsplit(".", 1)[0]
    claims: dict[str, Any] = json.loads(
        base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    )
    return claims


def basic_auth(
    client_id: str = CLIENT_ID, client_secret: str = CLIENT_SECRET
) -> dict[str, str]:
    raw = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    return {"Authorization": f"Basic {raw}"}


def redirect_query(response: ClientResponse) -> dict[str, list[str]]:
    """The query parameters of an authorization redirect."""
    return parse_qs(urlparse(response.headers["Location"]).query)


async def submit_consent(
    client: TestClient, action: str = "approve", **fields: str
) -> ClientResponse:
    """Post the consent form the way the consent page submits it."""
    form = {
        "action": action,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": s256(VERIFIER),
        "state": STATE,
    }
    form.update(fields)
    return await client.post("/authorize", data=form, allow_redirects=False)


async def issue_code(
    client: TestClient, verifier: str = VERIFIER, redirect_uri: str = REDIRECT_URI
) -> str:
    """Approve consent and return the authorization code it redirects with."""
    response = await submit_consent(
        client, code_challenge=s256(verifier), redirect_uri=redirect_uri
    )
    assert response.status == 302, await response.text()
    return redirect_query(response)["code"][0]


async def request_token(
    client: TestClient, form: dict[str, str], headers: dict[str, str] | None = None
) -> ClientResponse:
    """POST the token endpoint, as the configured client unless ``headers``."""
    return await client.post(
        "/token", data=form, headers=basic_auth() if headers is None else headers
    )


async def exchange_code(
    client: TestClient,
    code: str,
    verifier: str = VERIFIER,
    redirect_uri: str = REDIRECT_URI,
) -> ClientResponse:
    return await request_token(
        client,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
    )


async def obtain_tokens(client: TestClient) -> dict[str, Any]:
    """Run the whole authorization-code flow and return the token response."""
    response = await exchange_code(client, await issue_code(client))
    assert response.status == 200, await response.text()
    tokens: dict[str, Any] = await response.json()
    return tokens


async def get_json(
    client: TestClient, path: str, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """GET a discovery document, which must be served."""
    response = await client.get(path, headers=headers)
    assert response.status == 200, await response.text()
    document: dict[str, Any] = await response.json()
    return document


async def post_webhook(
    client: TestClient, authorization: str | None, webhook_id: str = WEBHOOK_ID
) -> ClientResponse:
    """An MCP request to the webhook, with ``authorization`` as its header."""
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    return await client.post(
        f"/api/webhook/{webhook_id}",
        data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}',
        headers=headers,
    )


class RenderedPage(HTMLParser):
    """The tags of a rendered page, read as a browser would parse them."""

    def __init__(self, html: str) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def inputs(self) -> dict[str, dict[str, str | None]]:
        """Each ``<input>`` by name, with all of its attributes."""
        return {
            str(attrs["name"]): attrs
            for tag, attrs in self.tags
            if tag == "input" and attrs.get("name")
        }
