"""None-mode sign-in on the webhook's scoped OAuth views (issue #1969).

``custom_components/ha_mcp_tools/oauth_autoapprove.py`` on Core's own HTTP app:
the invisible ``/authorize`` (a PKCE code and a 302, no page) and ``/token``
(public-client PKCE exchange, cosmetic opaque token), and the views' answers
once the entry is unloaded or the mode changes.
"""

from __future__ import annotations

import pytest
from aiohttp import ClientResponse
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools.const import (
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.mcp_webhook import async_unregister_webhook
from custom_components.ha_mcp_tools.oauth_legacy import MAX_PENDING_CODES

from ._oauth_autoapprove_core import (
    AUTHORIZE_URL,
    CLAUDE_REDIRECT,
    DISCOVERY_URL,
    PUBLIC_HEADERS,
    REVOKE_URL,
    TOKEN_URL,
    OAuthSurface,
    OpenSurface,
    authorize_query,
    parse_location,
    pkce_pair,
)
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface


async def authorize(surface: OAuthSurface, **overrides: str) -> ClientResponse:
    """GET ``/authorize`` the way a client behind a proxy does, unfollowed."""
    return await surface.client.get(
        AUTHORIZE_URL,
        params=authorize_query(**overrides),
        headers=PUBLIC_HEADERS,
        allow_redirects=False,
    )


async def sign_in(surface: OAuthSurface) -> tuple[str, str]:
    """Authorize with a fresh PKCE pair; the code the redirect carries, and its verifier."""
    verifier, challenge = pkce_pair()
    resp = await authorize(surface, code_challenge=challenge)
    assert resp.status == 302
    return parse_location(resp.headers["Location"])[1]["code"], verifier


async def redeem(
    surface: OAuthSurface, code: str, verifier: str | None, **overrides: str
) -> ClientResponse:
    """POST ``/token`` for ``code``; ``verifier=None`` leaves the verifier out."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_REDIRECT,
    }
    if verifier is not None:
        form["code_verifier"] = verifier
    form.update(overrides)
    return await surface.client.post(TOKEN_URL, data=form)


async def test_authorize_redirects_to_the_callback_and_names_the_issuer(
    open_oauth_surface: OpenSurface,
) -> None:
    """A connector's sign-in lands on its callback with a code and the issuer
    the discovery document advertises, or RFC 9207 clients refuse it."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    resp = await authorize(surface)

    assert resp.status == 302
    base, params = parse_location(resp.headers["Location"])
    assert base == CLAUDE_REDIRECT
    assert params["state"] == "st-1"
    assert params["code"]
    advertised = await surface.client.get(DISCOVERY_URL, headers=PUBLIC_HEADERS)
    assert params["iss"] == (await advertised.json())["issuer"]


async def test_the_issued_code_redeems_for_an_opaque_bearer_token(
    open_oauth_surface: OpenSurface,
) -> None:
    """A client that never held a secret completes sign-in and gets a token it
    may store, with no refresh token and nothing cacheable."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    code, verifier = await sign_in(surface)

    resp = await redeem(surface, code, verifier)

    assert resp.status == 200
    body = await resp.json()
    assert body["token_type"] == "Bearer"
    assert body["access_token"]
    assert isinstance(body["expires_in"], int)
    assert "refresh_token" not in body
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Pragma"] == "no-cache"


async def test_every_sign_in_gets_its_own_token(
    open_oauth_surface: OpenSurface,
) -> None:
    """Two sign-ins are never handed the same bearer string."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    tokens = []
    for _ in range(2):
        code, verifier = await sign_in(surface)
        tokens.append(
            (await (await redeem(surface, code, verifier)).json())["access_token"]
        )

    assert len(tokens[0]) >= 20
    assert tokens[0] != tokens[1]


async def test_a_wrong_code_verifier_is_refused(
    open_oauth_surface: OpenSurface,
) -> None:
    """A stolen code is useless without the verifier its PKCE challenge commits to."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    code, _ = await sign_in(surface)

    resp = await redeem(surface, code, pkce_pair()[0])

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_grant"


async def test_a_code_redeems_only_once(open_oauth_surface: OpenSurface) -> None:
    """A replayed code is refused, so an intercepted redirect cannot be reused."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    code, verifier = await sign_in(surface)

    first = await redeem(surface, code, verifier)
    second = await redeem(surface, code, verifier)

    assert first.status == 200
    assert second.status == 400
    assert (await second.json())["error"] == "invalid_grant"


async def test_a_token_request_without_a_verifier_is_invalid_request(
    open_oauth_surface: OpenSurface,
) -> None:
    """The verifier is required, not optional, for the public client."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    code, _ = await sign_in(surface)

    resp = await redeem(surface, code, None)

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_request"


async def test_the_refresh_grant_is_unsupported(
    open_oauth_surface: OpenSurface,
) -> None:
    """None mode issues no refresh token, so it does not honour a refresh grant."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    resp = await surface.client.post(TOKEN_URL, data={"grant_type": "refresh_token"})

    assert resp.status == 400
    assert (await resp.json())["error"] == "unsupported_grant_type"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"code_challenge_method": "plain"}, id="plain-method"),
        pytest.param({"response_type": "token"}, id="implicit-response"),
        pytest.param({"code_challenge": "too-short"}, id="short-challenge"),
        pytest.param({"code_challenge": "a" * 43 + "\n"}, id="trailing-newline"),
        pytest.param(
            {"redirect_uri": "https://evil.example/cb#frag"}, id="malformed-redirect"
        ),
    ],
)
async def test_authorize_refuses_a_request_it_cannot_bind_a_code_to(
    open_oauth_surface: OpenSurface, overrides: dict[str, str]
) -> None:
    """A weak PKCE request or a malformed callback gets a 400, never a code."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    resp = await authorize(surface, **overrides)

    assert resp.status == 400
    assert "Location" not in resp.headers


async def test_the_resource_parameter_is_tolerated_on_both_legs(
    open_oauth_surface: OpenSurface,
) -> None:
    """RFC 8707 clients must send ``resource``; neither endpoint may reject it."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    resource = "https://ha.example/api/webhook/abc"
    verifier, challenge = pkce_pair()

    authorized = await authorize(surface, code_challenge=challenge, resource=resource)
    code = parse_location(authorized.headers["Location"])[1]["code"]
    redeemed = await redeem(surface, code, verifier, resource=resource)

    assert authorized.status == 302
    assert redeemed.status == 200


async def test_a_full_code_store_answers_temporarily_unavailable(
    open_oauth_surface: OpenSurface,
) -> None:
    """Once the pending-code store is full a new sign-in is told to retry, on the
    callback, with the state and issuer, instead of failing silently."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    for _ in range(MAX_PENDING_CODES):
        assert (
            "code" in parse_location((await authorize(surface)).headers["Location"])[1]
        )

    resp = await authorize(surface)

    assert resp.status == 302
    _, params = parse_location(resp.headers["Location"])
    assert params["error"] == "temporarily_unavailable"
    assert params["state"] == "st-1"
    assert params["iss"]
    assert "code" not in params


@pytest.mark.parametrize("mode", [WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA])
async def test_a_token_body_in_an_unknown_charset_is_invalid_request(
    open_oauth_surface: OpenSurface, mode: str
) -> None:
    """A token body whose Content-Type names an unknown charset is a 400, not
    the 500 aiohttp's LookupError would make of this anonymous endpoint."""
    surface = await open_oauth_surface(mode)

    resp = await surface.client.post(
        TOKEN_URL,
        data=b"grant_type=authorization_code",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=nope"},
    )

    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_request"


@pytest.mark.parametrize(
    ("method", "url"),
    [("GET", AUTHORIZE_URL), ("POST", TOKEN_URL), ("POST", REVOKE_URL)],
)
async def test_the_views_answer_not_found_once_the_entry_is_unloaded(
    open_oauth_surface: OpenSurface, hass: HomeAssistant, method: str, url: str
) -> None:
    """aiohttp cannot unbind a view, so a view bound by an unloaded entry
    answers 404 itself instead of serving a flow nothing backs."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    await async_unregister_webhook(hass)

    resp = await surface.client.request(method, url, allow_redirects=False)

    assert resp.status == 404
    assert (await resp.json())["error"] == "not_found"


async def test_a_mode_switch_reuses_the_bound_views(
    open_oauth_surface: OpenSurface,
) -> None:
    """Switching mode in one HA session serves the new mode from the same
    routes, so no restart is needed (aiohttp cannot rebind a view)."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)
    assert (await authorize(surface)).headers["Location"].startswith(CLAUDE_REDIRECT)

    await surface.switch_mode(WEBHOOK_AUTH_HA)
    resp = await authorize(surface)

    assert resp.status == 302
    assert resp.headers["Location"].startswith("/auth/authorize")


@pytest.mark.parametrize(
    ("mode", "advertises_revocation"),
    [
        (WEBHOOK_AUTH_HA, True),
        (WEBHOOK_AUTH_NONE, False),
        (WEBHOOK_AUTH_LEGACY, False),
    ],
)
async def test_only_ha_auth_advertises_the_revocation_endpoint(
    open_oauth_surface: OpenSurface, mode: str, advertises_revocation: bool
) -> None:
    """Only ha_auth hands out a refresh envelope, so only its document names a
    revocation endpoint; advertising one that 404s would be worse than silence."""
    surface = await open_oauth_surface(mode)

    doc = await (await surface.client.get(DISCOVERY_URL, headers=PUBLIC_HEADERS)).json()

    assert ("revocation_endpoint" in doc) is advertises_revocation
    if advertises_revocation:
        assert doc["revocation_endpoint_auth_methods_supported"] == ["none"]


async def test_none_mode_advertises_that_redirects_carry_the_issuer(
    open_oauth_surface: OpenSurface,
) -> None:
    """RFC 9207 §3: omitting the flag reads as "not supported" to clients."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_NONE)

    doc = await (await surface.client.get(DISCOVERY_URL, headers=PUBLIC_HEADERS)).json()

    assert doc["authorization_response_iss_parameter_supported"] is True
