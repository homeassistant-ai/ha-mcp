"""The none-mode auto-approve OAuth flow, as an MCP client runs it.

Discovery, the invisible authorize redirect and the PKCE token exchange are
all made over Home Assistant's real HTTP server against a proxy Core set up
with OAuth off.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import ClientResponse
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import ClientSessionGenerator

from ._auth_modes import (
    CLAUDE_REDIRECT,
    as_document_urls,
    fetch_json,
    pkce_pair,
    proxy_config,
    submodule,
)
from ._proxy import start_proxy
from ._variants import Variant


@pytest.fixture
async def proxy_client(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    component: ModuleType,
    variant: Variant,
    fs_root: Path,
) -> TestClient:
    """A client of a proxy running with OAuth off."""
    await start_proxy(hass, variant, fs_root, proxy_config())
    return await hass_client_no_auth()


async def _authorize(
    proxy_client: TestClient,
    variant: Variant,
    challenge: str,
    **overrides: str,
) -> ClientResponse:
    params = {
        "response_type": "code",
        "client_id": "https://claude.ai/oauth/mcp-oauth-client-metadata",
        "redirect_uri": CLAUDE_REDIRECT,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "st-1",
        **overrides,
    }
    return await proxy_client.get(
        f"{variant.oauth_base}/authorize",
        params=params,
        headers={"Host": "ha.example.com"},
        allow_redirects=False,
    )


def _redirect_query(response: ClientResponse) -> dict[str, list[str]]:
    assert response.status == 302
    return parse_qs(urlparse(response.headers["Location"]).query)


async def _token(
    proxy_client: TestClient, variant: Variant, code: str, verifier: str
) -> ClientResponse:
    return await proxy_client.post(
        f"{variant.oauth_base}/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_REDIRECT,
            "code_verifier": verifier,
        },
    )


async def test_none_mode_document_advertises_the_proxys_public_pkce_server(
    proxy_client: TestClient, variant: Variant
) -> None:
    """The document HA core's own root discovery lacks (#1969): the proxy's
    endpoints, a public PKCE client (``none``), CIMD, DCR, and RFC 9207
    ``iss`` on authorization responses."""
    status, doc = await fetch_json(
        proxy_client, as_document_urls(variant)[0], "x.example"
    )

    base = f"http://x.example{variant.oauth_base}"
    assert status == 200
    assert doc == {
        "issuer": base,
        "authorization_response_iss_parameter_supported": True,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
    }


async def test_authorize_redirects_straight_back_and_the_code_buys_a_token(
    proxy_client: TestClient, variant: Variant
) -> None:
    """Authorize renders nothing and 302s back with a code and the state; the
    code plus its PKCE verifier, with no client secret, buys a bearer that
    must not be cached and comes with no refresh token."""
    verifier, challenge = pkce_pair()

    query = _redirect_query(await _authorize(proxy_client, variant, challenge))
    assert query["state"] == ["st-1"]
    response = await _token(proxy_client, variant, query["code"][0], verifier)

    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"
    body: dict[str, Any] = await response.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert isinstance(body["expires_in"], int)
    assert "refresh_token" not in body


@pytest.mark.parametrize("store_full", [False, True], ids=["code", "error"])
async def test_authorization_responses_carry_the_advertised_rfc9207_iss(
    proxy_client: TestClient,
    component: ModuleType,
    variant: Variant,
    monkeypatch: pytest.MonkeyPatch,
    store_full: bool,
) -> None:
    """RFC 9207 §2: success and error redirects both name the issuer that
    produced them, exactly as the metadata document advertises it."""
    if store_full:
        # The pending-code store at its abuse cap answers temporarily_unavailable.
        monkeypatch.setattr(submodule(component, "oauth"), "MAX_PENDING_CODES", 0)
    _verifier, challenge = pkce_pair()

    query = _redirect_query(await _authorize(proxy_client, variant, challenge))

    _status, doc = await fetch_json(
        proxy_client, as_document_urls(variant)[0], "ha.example.com"
    )
    assert ("error" in query) is store_full
    assert ("code" in query) is not store_full
    assert query["iss"] == [doc["issuer"]]


async def test_any_valid_https_redirect_is_accepted(
    proxy_client: TestClient, variant: Variant
) -> None:
    """The secret webhook URL is the only credential in none mode, so by
    maintainer decision authorize serves any spec-valid redirect, not only
    claude.ai's."""
    _verifier, challenge = pkce_pair()

    response = await _authorize(
        proxy_client,
        variant,
        challenge,
        client_id="https://other.example/cimd",
        redirect_uri="https://other.example/cb",
    )

    assert response.status == 302
    assert response.headers["Location"].startswith("https://other.example/cb?")


async def test_authorize_refuses_a_non_s256_challenge(
    proxy_client: TestClient, variant: Variant
) -> None:
    """Only PKCE S256 is advertised; a ``plain`` challenge is a 400, never a
    code."""
    _verifier, challenge = pkce_pair()

    response = await _authorize(
        proxy_client, variant, challenge, code_challenge_method="plain"
    )

    assert response.status == 400


async def test_wrong_pkce_verifier_is_an_invalid_grant(
    proxy_client: TestClient, variant: Variant
) -> None:
    """A code redeemed with another verifier gets ``invalid_grant``."""
    _verifier, challenge = pkce_pair()
    other_verifier, _challenge = pkce_pair()
    query = _redirect_query(await _authorize(proxy_client, variant, challenge))

    response = await _token(proxy_client, variant, query["code"][0], other_verifier)

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_grant"


async def test_authorization_code_redeems_only_once(
    proxy_client: TestClient, variant: Variant
) -> None:
    """A replayed code is refused after its first successful exchange."""
    verifier, challenge = pkce_pair()
    code = _redirect_query(await _authorize(proxy_client, variant, challenge))["code"][
        0
    ]

    first = await _token(proxy_client, variant, code, verifier)
    replay = await _token(proxy_client, variant, code, verifier)

    assert first.status == 200
    assert replay.status == 400
    assert (await replay.json())["error"] == "invalid_grant"
