"""Revoking an ha_auth session through the scoped views, against Core's real revocation (#2248).

The refresh token the client holds is a signed envelope Core cannot recognise,
and Core answers 200 to a revocation of any token it does not know. Fronting
revocation is what makes the answer true: the envelope is swapped for Core's own
token, which Core then revokes. Core takes a revocation as ``action=revoke`` on
``/auth/token`` and as ``/auth/revoke``, so both are covered.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from aiohttp import ClientResponse

from custom_components.ha_mcp_tools import oauth_dcr
from custom_components.ha_mcp_tools.const import (
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.oauth_ha_auth import (
    MAX_REVOKE_ENVELOPE_LEN,
    unwrap_refresh_token,
    wrap_refresh_token,
)

from ._oauth_autoapprove_core import (
    DCR_KEY,
    REVOKE_URL,
    TOKEN_URL,
    OAuthSurface,
    OpenSurface,
)
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface

LOOPBACK_CALLBACK = "http://127.0.0.1:19877/mcp/oauth/callback"
RUNTIME_CALLBACK = "http://127.0.0.1:54321/mcp/oauth/callback"
RUNTIME_ORIGIN = "http://127.0.0.1:54321"
ROTATED_DCR_KEY = b"r" * 32
# Both spellings Core accepts a revocation in, and the Core path each reaches.
REVOKE_SURFACES = {"token-action": "/auth/token", "scoped-revoke": "/auth/revoke"}
SURFACES = sorted(REVOKE_SURFACES)


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
