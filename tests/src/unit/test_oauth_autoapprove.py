"""The scoped ``/authorize`` and revocation views under legacy and ha_auth mode.

Legacy serves its consent page from the scoped route; ha_auth 302s the browser
into Core's own ``/auth/authorize`` with the query re-encoded and the client_id
translated when the client's registration can be verified (#1969, #2188).

Revoking an ha_auth session runs against Core's real revocation (#2248). The
refresh token the client holds is a signed envelope Core cannot recognise, and
Core answers 200 to a revocation of any token it does not know. Fronting
revocation swaps the envelope for Core's own token, which Core then revokes.
Core takes a revocation as ``action=revoke`` on ``/auth/token`` and as
``/auth/revoke``, so both are covered.
"""

from __future__ import annotations

import base64
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp import ClientResponse
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import oauth_dcr
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DOMAIN,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.oauth_autoapprove import CFG_CIMD_SESSION
from custom_components.ha_mcp_tools.oauth_ha_auth import (
    MAX_REVOKE_ENVELOPE_LEN,
    unwrap_refresh_token,
    wrap_refresh_token,
)

from ._oauth_autoapprove_core import (
    AUTHORIZE_URL,
    CLAUDE_REDIRECT,
    DCR_KEY,
    DISCOVERY_URL,
    LEGACY_CLIENT_ID,
    REVOKE_URL,
    TOKEN_URL,
    OAuthSurface,
    OpenSurface,
    serve_cimd_documents,
)
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface

PKCE_CHALLENGE = "a" * 43
LOOPBACK_CALLBACK = "http://127.0.0.1:19877/mcp/oauth/callback"
RUNTIME_CALLBACK = "http://127.0.0.1:54321/mcp/oauth/callback"
RUNTIME_ORIGIN = "http://127.0.0.1:54321"
ROTATED_DCR_KEY = b"r" * 32
# Both spellings Core accepts a revocation in, and the Core path each reaches.
REVOKE_SURFACES = {"token-action": "/auth/token", "scoped-revoke": "/auth/revoke"}
SURFACES = sorted(REVOKE_SURFACES)


def redirect_query(location: str) -> dict[str, list[str]]:
    """The query of a redirect into Core's authorize page."""
    assert location.startswith("/auth/authorize?")
    return parse_qs(urlparse(location).query)


def revoke_post(surface: OAuthSurface, spelling: str, token: str, **extra: str) -> Any:
    """POST ``token`` as a revocation in one of the two spellings, unfollowed."""
    if spelling == "token-action":
        return surface.client.post(
            TOKEN_URL,
            data={"action": "revoke", "token": token, **extra},
            allow_redirects=False,
        )
    return surface.client.post(
        REVOKE_URL, data={"token": token, **extra}, allow_redirects=False
    )


async def signed_in_envelope(surface: OAuthSurface) -> tuple[str, str, str]:
    """A loopback client's sign-in: its client_id, the envelope it holds, and
    Core's own refresh token inside."""
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    body = await surface.exchange_code(client_id, RUNTIME_CALLBACK, RUNTIME_ORIGIN)
    envelope = unwrap_refresh_token(DCR_KEY, body["refresh_token"], client_id)
    assert isinstance(envelope, tuple)
    return client_id, body["refresh_token"], envelope[0]


def prefixed(payload: bytes) -> str:
    """A ``hamcp-rt-`` value over ``payload``, signed with nothing we minted."""
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    return f"hamcp-rt-{encoded}.not-a-real-signature"


async def assert_unavailable_with_retry(resp: ClientResponse) -> None:
    """The 503 RFC 7009 §2.2.1 gives a revocation: assume the token lives, retry."""
    assert resp.status == 503
    assert (await resp.json())["error"] == "temporarily_unavailable"
    assert int(resp.headers["Retry-After"]) > 0


# -- scoped authorize view ----------------------------------------------------


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


# -- revocation ---------------------------------------------------------------


@pytest.mark.parametrize("spelling", SURFACES)
async def test_revoking_the_envelope_revokes_cores_own_token(
    open_oauth_surface: OpenSurface, spelling: str
) -> None:
    """Core must revoke ITS token, not the envelope we handed the client.

    Forwarding the envelope would report success while leaving the session live.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    _, envelope, core_token = await signed_in_envelope(surface)
    assert surface.core_knows(core_token)

    resp = await revoke_post(surface, spelling, envelope)

    assert resp.status == 200
    assert not surface.core_knows(core_token)


@pytest.mark.parametrize("spelling", SURFACES)
async def test_revoking_a_token_that_is_not_ours_reaches_core_from_the_client(
    open_oauth_surface: OpenSurface, spelling: str
) -> None:
    """Nothing needs rewriting, so Core observes the CLIENT's address, via a
    relative Location, and revokes the token itself."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    token = await surface.core_refresh_token("https://claude.ai")

    unfollowed = await revoke_post(surface, spelling, token)

    assert unfollowed.status == 307
    assert unfollowed.headers["Location"] == REVOKE_SURFACES[spelling]
    assert unfollowed.headers["Cache-Control"] == "no-store"
    assert surface.core_knows(token)
    form = {"token": token}
    if spelling == "token-action":
        form["action"] = "revoke"
    target = TOKEN_URL if spelling == "token-action" else REVOKE_URL
    followed = await surface.client.post(target, data=form)
    assert followed.status == 200
    assert not surface.core_knows(token)


@pytest.mark.parametrize("spelling", SURFACES)
async def test_revocation_without_a_signing_key_goes_to_core(
    open_oauth_surface: OpenSurface, spelling: str
) -> None:
    """No signing key means no envelope could have been minted, so there is
    nothing to unwrap: the revocation is redirected like any unrecognised token."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA, dcr_key=None)

    resp = await revoke_post(surface, spelling, "hamcp-rt-whatever.sig")

    assert resp.status == 307


@pytest.mark.parametrize("mode", [WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_LEGACY])
async def test_the_scoped_revoke_view_is_not_found_outside_ha_auth(
    open_oauth_surface: OpenSurface, mode: str
) -> None:
    """Only ha_auth hands out an envelope, so only ha_auth fronts revocation."""
    surface = await open_oauth_surface(mode)

    resp = await surface.client.post(
        REVOKE_URL, data={"token": "anything"}, allow_redirects=False
    )

    assert resp.status == 404
    assert (await resp.json())["error"] == "not_found"


async def test_a_revoke_body_in_an_unknown_charset_is_invalid_request(
    open_oauth_surface: OpenSurface,
) -> None:
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)

    resp = await surface.client.post(
        REVOKE_URL,
        data=b"token=anything",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=nope"},
        allow_redirects=False,
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_request"


@pytest.mark.parametrize("spelling", SURFACES)
async def test_a_core_that_does_not_answer_a_revocation_is_temporarily_unavailable(
    open_oauth_surface: OpenSurface, spelling: str
) -> None:
    """A failed forward is a 503 naming the retry delay, not a fabricated success.

    The retry contract belongs to the REQUEST, not the URL: ``action=revoke`` on
    /token is the same revocation as one posted to the scoped view, so neither
    answers a barer 503 than the other.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    _, envelope, core_token = await signed_in_envelope(surface)
    surface.cut_off_core()

    resp = await revoke_post(surface, spelling, envelope)

    await assert_unavailable_with_retry(resp)
    assert surface.core_knows(core_token)


async def test_a_failed_refresh_forward_carries_no_retry_after(
    open_oauth_surface: OpenSurface,
) -> None:
    """RFC 6749 gives an ordinary token failure no retry contract, so the header
    belongs to revocation alone; the envelope forces the same proxy leg, which
    makes this the counterpart of the test above."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id, envelope, _ = await signed_in_envelope(surface)
    surface.cut_off_core()

    resp = await surface.client.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": envelope,
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 503
    assert "Retry-After" not in resp.headers


@pytest.mark.parametrize("spelling", SURFACES)
@pytest.mark.parametrize("unverifiable", ["tampered", "rotated-key"])
async def test_an_envelope_that_no_longer_verifies_still_revokes_cores_token(
    open_oauth_surface: OpenSurface, spelling: str, unverifiable: str
) -> None:
    """Core must see ITS token even when the envelope no longer verifies.

    Rotating the DCR signing key, which removing and re-adding the integration
    does, invalidates every envelope in a client's hands. Treating those as "not
    ours" would 307 the ``hamcp-rt-`` string to Core, whose revoke answers 200
    for any token it cannot resolve: the client is told the session died while
    the grant stays live for its full 90 days (#2249 review). Tampering takes the
    same branch; forwarding an unverified body is safe here because RFC 7009
    authorizes the bearer and Core's endpoint is anonymous and idempotent.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    core_token = await surface.core_refresh_token(RUNTIME_ORIGIN)
    signing_key = ROTATED_DCR_KEY if unverifiable == "rotated-key" else DCR_KEY
    envelope = wrap_refresh_token(signing_key, core_token, RUNTIME_ORIGIN, client_id)
    if unverifiable == "tampered":
        body, _, signature = envelope.rpartition(".")
        envelope = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"

    resp = await revoke_post(surface, spelling, envelope)

    assert resp.status == 200
    assert not surface.core_knows(core_token)


@pytest.mark.parametrize("spelling", SURFACES)
@pytest.mark.parametrize(
    "token",
    [
        pytest.param("hamcp-rt-nodot", id="no-separator"),
        pytest.param("hamcp-rt-###.not-a-real-signature", id="undecodable-base64"),
        pytest.param(prefixed(b"not json"), id="not-json"),
        pytest.param(prefixed(b'["not","an","object"]'), id="json-array"),
        pytest.param(prefixed(b'{"v":1,"t":42}'), id="int-t"),
    ],
)
async def test_a_prefixed_value_carrying_no_token_goes_to_core_as_presented(
    open_oauth_surface: OpenSurface, spelling: str, token: str
) -> None:
    """Our prefix over an unreadable body is not a token we can substitute.

    The best-effort unwrap gives up rather than inventing a value, so Core
    answers it as presented and no outbound call is made from here.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)

    resp = await revoke_post(surface, spelling, token)

    assert resp.status == 307
    assert resp.headers["Location"] == REVOKE_SURFACES[spelling]


@pytest.mark.parametrize("spelling", SURFACES)
async def test_an_over_long_prefixed_value_is_never_decoded(
    open_oauth_surface: OpenSurface, spelling: str
) -> None:
    """Past the cap the body is never decoded, so there is nothing to swap in.

    The value is a well-formed envelope under another key: only its SIZE stops
    it being parsed, which is the point of the cap on an anonymous view. Core
    keeps the token it names, proving nothing was unwrapped and forwarded.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    core_token = await surface.core_refresh_token(RUNTIME_ORIGIN)
    oversized = wrap_refresh_token(
        ROTATED_DCR_KEY, core_token, "x" * MAX_REVOKE_ENVELOPE_LEN, "cid"
    )

    resp = await revoke_post(surface, spelling, oversized)

    assert len(oversized) > MAX_REVOKE_ENVELOPE_LEN
    assert resp.status == 307
    assert surface.core_knows(core_token)
