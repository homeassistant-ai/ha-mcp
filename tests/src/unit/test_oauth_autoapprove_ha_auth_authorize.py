"""The scoped ``/authorize`` view when legacy or ha_auth mode is live (#1969, #2188).

Legacy serves its consent page from the scoped route; ha_auth 302s the browser
into Core's own ``/auth/authorize`` with the query re-encoded and the client_id
translated when the client's registration can be verified.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DOMAIN,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
)
from custom_components.ha_mcp_tools.oauth_autoapprove import CFG_CIMD_SESSION

from ._oauth_autoapprove_core import (
    AUTHORIZE_URL,
    CLAUDE_REDIRECT,
    DISCOVERY_URL,
    LEGACY_CLIENT_ID,
    REVOKE_URL,
    TOKEN_URL,
    OpenSurface,
    serve_cimd_documents,
)
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface

PKCE_CHALLENGE = "a" * 43


def redirect_query(location: str) -> dict[str, list[str]]:
    """The query of a redirect into Core's authorize page."""
    assert location.startswith("/auth/authorize?")
    return parse_qs(urlparse(location).query)


async def test_legacy_mode_serves_its_consent_page_from_the_scoped_route(
    open_oauth_surface: OpenSurface,
) -> None:
    """Legacy mode asks the user to authorize on the unified scoped route."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_LEGACY)

    resp = await surface.client.get(
        AUTHORIZE_URL,
        params={
            "response_type": "code",
            "client_id": LEGACY_CLIENT_ID,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_challenge": PKCE_CHALLENGE,
            "code_challenge_method": "S256",
        },
    )

    assert resp.status == 200
    text = await resp.text()
    assert "Authorize MCP Connector" in text
    assert f'action="{AUTHORIZE_URL}"' in text


async def test_ha_auth_authorize_redirects_into_core_with_the_query_intact(
    open_oauth_surface: OpenSurface,
) -> None:
    """The user logs in on Core's own page, carrying every original parameter."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)

    resp = await surface.client.get(
        AUTHORIZE_URL,
        params={
            "response_type": "code",
            "client_id": "https://claude.ai",
            "redirect_uri": CLAUDE_REDIRECT,
            "code_challenge": PKCE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        allow_redirects=False,
    )

    assert resp.status == 302
    query = redirect_query(resp.headers["Location"])
    # Same-origin client_id passes through untranslated.
    assert query["client_id"] == ["https://claude.ai"]
    assert query["state"] == ["xyz"]
    assert query["redirect_uri"] == [CLAUDE_REDIRECT]
    assert query["code_challenge_method"] == ["S256"]


async def test_ha_auth_authorize_keeps_every_resource_parameter(
    open_oauth_surface: OpenSurface,
) -> None:
    """RFC 8707 allows ``resource`` twice; both values reach Core."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)

    resp = await surface.client.get(
        AUTHORIZE_URL
        + "?response_type=code&client_id=https%3A%2F%2Fclaude.ai"
        + "&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback"
        + f"&code_challenge={PKCE_CHALLENGE}&code_challenge_method=S256"
        + "&resource=https%3A%2F%2Fha.example%2Ffirst"
        + "&resource=https%3A%2F%2Fha.example%2Fsecond",
        allow_redirects=False,
    )

    assert resp.status == 302
    query = redirect_query(resp.headers["Location"])
    assert query["resource"] == [
        "https://ha.example/first",
        "https://ha.example/second",
    ]


async def test_ha_auth_authorize_percent_encodes_the_forwarded_query(
    open_oauth_surface: OpenSurface,
) -> None:
    """A loopback callback is forwarded percent-encoded, not as a literal URL.

    Reverse proxies shipping a "block common exploits" ruleset (Nginx Proxy
    Manager is a common one) match ``[a-zA-Z0-9_]=http://`` and answer 403
    before Core sees the request, stranding every native-app client behind
    them.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    callback = "http://127.0.0.1:19877/mcp/oauth/callback"

    resp = await surface.client.get(
        AUTHORIZE_URL,
        params={
            "response_type": "code",
            "client_id": "http://127.0.0.1:19877",
            "redirect_uri": callback,
            "code_challenge": PKCE_CHALLENGE,
            "code_challenge_method": "S256",
            "state": "xyz",
        },
        allow_redirects=False,
    )

    location = resp.headers["Location"]
    assert re.search(r"[a-zA-Z0-9_]=http://", location) is None
    raw_query = urlparse(location).query
    assert ":" not in raw_query
    assert "/" not in raw_query
    query = parse_qs(raw_query)
    assert query["redirect_uri"] == [callback]
    assert query["client_id"] == ["http://127.0.0.1:19877"]
    assert query["state"] == ["xyz"]


async def test_ha_auth_authorize_resolves_client_metadata_on_its_own_pool(
    open_oauth_surface: OpenSurface,
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow public metadata host cannot exhaust the pool that carries the
    authenticated MCP traffic, and a verified identity is translated."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = "https://app.example/cimd.json"
    sessions = serve_cimd_documents(monkeypatch, {client_id: ["https://eu.example/cb"]})

    resp = await surface.client.get(
        AUTHORIZE_URL,
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://eu.example/cb",
        },
        allow_redirects=False,
    )

    cfg = hass.data[DOMAIN][DATA_WEBHOOK]
    assert resp.status == 302
    assert redirect_query(resp.headers["Location"])["client_id"] == [
        "https://eu.example"
    ]
    assert sessions == [cfg[CFG_CIMD_SESSION]]
    assert cfg[CFG_CIMD_SESSION] is not cfg["session"]


async def test_every_endpoint_the_ha_auth_document_advertises_is_bound(
    open_oauth_surface: OpenSurface,
) -> None:
    """A bundle missing a view would advertise a route that 404s: the sign-in,
    the exchange, or the revocation that the document tells clients to use."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    doc = await (await surface.client.get(DISCOVERY_URL)).json()

    assert doc["authorization_endpoint"].endswith(AUTHORIZE_URL)
    assert doc["token_endpoint"].endswith(TOKEN_URL)
    assert doc["revocation_endpoint"].endswith(REVOKE_URL)
    answers = [
        (await surface.client.get(AUTHORIZE_URL, allow_redirects=False)).status,
        (await surface.client.post(TOKEN_URL, allow_redirects=False)).status,
        (await surface.client.post(REVOKE_URL, allow_redirects=False)).status,
    ]
    assert 404 not in answers
