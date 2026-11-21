"""The scoped ``/token`` view in ha_auth mode, against Core's own ``/auth/token``.

Untranslated identities are sent to Core with a 307; a translated identity or
a signed refresh envelope (#2248) is forwarded server-side, and Core's answer
(a real access token, a real refresh token) is relayed with its
``refresh_token`` wrapped.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.ha_mcp_tools import oauth_dcr
from custom_components.ha_mcp_tools.const import DATA_WEBHOOK, DOMAIN, WEBHOOK_AUTH_HA
from custom_components.ha_mcp_tools.oauth_autoapprove import CFG_CIMD_SESSION
from custom_components.ha_mcp_tools.oauth_ha_auth import (
    EnvelopeState,
    unwrap_refresh_token,
)

from ._oauth_autoapprove_core import (
    CLAUDE_CLIENT_ID,
    DCR_KEY,
    TOKEN_URL,
    OAuthSurface,
    OpenSurface,
    serve_cimd_documents,
)
from ._oauth_autoapprove_core import open_oauth_surface as open_oauth_surface

LOOPBACK_CALLBACK = "http://127.0.0.1:19877/mcp/oauth/callback"
RUNTIME_CALLBACK = "http://127.0.0.1:54321/mcp/oauth/callback"
RUNTIME_ORIGIN = "http://127.0.0.1:54321"
WEB_CALLBACK = "https://a.example/cb"
SPARK_CALLBACKS = [
    "https://oauth-redirect.googleusercontent.com/r/ha-mcp",
    "https://oauth-redirect-sandbox.googleusercontent.com/r/ha-mcp",
]
CIMD_CLIENT_ID = "https://app.example/cimd.json"
CIMD_SAME_ORIGIN_REDIRECT = "https://app.example/cb"
HYBRID_CIMD_REDIRECTS = [CIMD_SAME_ORIGIN_REDIRECT, "https://eu.example/cb"]


async def refresh(
    surface: OAuthSurface, token: str, client_id: str, **extra: str
) -> tuple[int, dict[str, Any]]:
    """POST a refresh grant without following a redirect; status and body."""
    resp = await surface.client.post(
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": token,
            "client_id": client_id,
            **extra,
        },
        allow_redirects=False,
    )
    return resp.status, (await resp.json() if resp.status != 307 else {})


async def test_a_translated_code_exchange_reaches_core_under_the_translated_id(
    open_oauth_surface: OpenSurface,
) -> None:
    """A registered client's id is not a URL Core accepts, so Core must be given
    the origin it can verify; the grant it mints is bound to that origin."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [WEB_CALLBACK])

    body = await surface.exchange_code(client_id, WEB_CALLBACK, "https://a.example")

    assert surface.client_id_of(body["access_token"]) == "https://a.example"


async def test_a_translated_refresh_reaches_core_under_the_translated_id(
    open_oauth_surface: OpenSurface,
) -> None:
    """A refresh by a registered client re-derives the identity its grant is bound to."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [WEB_CALLBACK])
    token = await surface.core_refresh_token("https://a.example")

    status, body = await refresh(surface, token, client_id)

    assert status == 200
    assert surface.client_id_of(body["access_token"]) == "https://a.example"


async def test_an_unregistered_identity_exchanges_directly_with_core(
    open_oauth_surface: OpenSurface,
) -> None:
    """Core must see the CLIENT's address (its ban counters, trusted networks and
    last_used_ip key on it), so an exchange needing no rewrite is a 307, not a proxy.

    307 rather than 308 keeps method and body without a cacheable redirect that
    could outlive a later mode switch; the Location is relative so no forwarded
    header chooses where the credential is posted.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    form = {
        "grant_type": "authorization_code",
        "client_id": "https://claude.ai",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
    }

    unfollowed = await surface.client.post(
        TOKEN_URL,
        data={**form, "code": surface.core_code("https://claude.ai")},
        allow_redirects=False,
    )
    followed = await surface.client.post(
        TOKEN_URL, data={**form, "code": surface.core_code("https://claude.ai")}
    )

    assert unfollowed.status == 307
    assert unfollowed.headers["Location"] == "/auth/token"
    assert unfollowed.headers["Cache-Control"] == "no-store"
    assert followed.status == 200
    body = await followed.json()
    assert surface.core_knows(body["refresh_token"])


async def test_an_unregistered_refresh_goes_directly_to_core(
    open_oauth_surface: OpenSurface,
) -> None:
    """A plain refresh grant has no identity to translate, so it is a 307 too."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    token = await surface.core_refresh_token("https://claude.ai")
    form = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": "https://claude.ai",
    }

    unfollowed = await surface.client.post(TOKEN_URL, data=form, allow_redirects=False)
    followed = await surface.client.post(TOKEN_URL, data=form)

    assert unfollowed.status == 307
    assert unfollowed.headers["Location"] == "/auth/token"
    assert followed.status == 200
    assert surface.client_id_of((await followed.json())["access_token"])


async def test_a_code_exchange_without_a_redirect_is_left_to_core(
    open_oauth_surface: OpenSurface,
) -> None:
    """A malformed code exchange is not mistaken for a refresh grant: with no
    redirect to translate from, Core stays the authority."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [WEB_CALLBACK])

    resp = await surface.client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
        },
        allow_redirects=False,
    )

    assert resp.status == 307
    assert resp.headers["Location"] == "/auth/token"


async def test_a_refresh_without_a_signing_key_goes_to_core_instead_of_raising(
    open_oauth_surface: OpenSurface,
) -> None:
    """Registration mints the DCR key, so ha_auth can be live before any client
    has registered; the envelope check then has nothing to verify with."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA, dcr_key=None)
    token = await surface.core_refresh_token(CLAUDE_CLIENT_ID)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": CLAUDE_CLIENT_ID,
    }

    unfollowed = await surface.client.post(TOKEN_URL, data=form, allow_redirects=False)
    followed = await surface.client.post(TOKEN_URL, data=form)

    assert unfollowed.status == 307
    assert unfollowed.headers["Location"] == "/auth/token"
    assert followed.status == 200


async def test_a_core_that_does_not_answer_is_temporarily_unavailable(
    open_oauth_surface: OpenSurface,
) -> None:
    """A failed forward is a 503 the client can retry, not a fabricated grant.

    Only translated exchanges are forwarded server-side; untranslated ones are
    redirected before any forward is attempted.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [WEB_CALLBACK])
    surface.cut_off_core()

    resp = await surface.client.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": "code-1",
            "client_id": client_id,
            "redirect_uri": WEB_CALLBACK,
        },
    )

    assert resp.status == 503
    assert (await resp.json())["error"] == "temporarily_unavailable"


@pytest.mark.parametrize(
    "registered",
    [
        pytest.param(
            ["http://localhost/callback", "http://127.0.0.1/callback"],
            id="loopback-only",
        ),
        pytest.param(SPARK_CALLBACKS, id="multi-origin"),
        pytest.param(["http://localhost/callback", WEB_CALLBACK], id="hybrid"),
    ],
)
async def test_a_pre_envelope_refresh_with_no_reproducible_origin_is_answered_locally(
    open_oauth_surface: OpenSurface, registered: list[str]
) -> None:
    """A refresh token minted before #2248 names no identity, and these
    registrations cannot re-derive the origin Core bound it to, so the answer is
    a local invalid_grant naming the one-time re-authorize that fixes it, rather
    than a guaranteed failure fed into Core's failed-login accounting."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, registered)
    token = await surface.core_refresh_token("https://a.example")

    status, body = await refresh(surface, token, client_id)

    assert status == 400
    assert body["error"] == "invalid_grant"
    assert "predates the signed identity envelope" in body["error_description"]
    assert surface.core_knows(token)


async def test_a_pre_envelope_refresh_of_a_multi_origin_metadata_client_is_answered_locally(
    open_oauth_surface: OpenSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified metadata-document identity with no reproducible origin gets
    the same local invalid_grant as the equivalent registration, not a 307 into
    Core's failed-login accounting on every token expiry."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = "https://spark.example/client-metadata.json"
    serve_cimd_documents(monkeypatch, {client_id: SPARK_CALLBACKS})
    token = await surface.core_refresh_token("https://a.example")

    status, body = await refresh(surface, token, client_id)

    assert status == 400
    assert body["error"] == "invalid_grant"


async def test_a_refresh_carrying_a_redirect_translates_like_authorize(
    open_oauth_surface: OpenSurface,
) -> None:
    """Sending the redirect keeps a multi-origin client refreshable even though
    the redirect-less derivation cannot pick one of its origins."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, SPARK_CALLBACKS)
    token = await surface.core_refresh_token(
        "https://oauth-redirect.googleusercontent.com"
    )

    status, body = await refresh(
        surface, token, client_id, redirect_uri=SPARK_CALLBACKS[0]
    )

    assert status == 200
    assert (
        surface.client_id_of(body["access_token"])
        == "https://oauth-redirect.googleusercontent.com"
    )


async def test_a_refresh_resolves_client_metadata_on_its_own_pool(
    open_oauth_surface: OpenSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refresh leg keeps slow metadata hosts off the relay pool too."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    sessions = serve_cimd_documents(
        monkeypatch, {CIMD_CLIENT_ID: [CIMD_SAME_ORIGIN_REDIRECT]}
    )
    token = await surface.core_refresh_token(CIMD_CLIENT_ID)

    status, _ = await refresh(surface, token, CIMD_CLIENT_ID)

    assert status == 307
    assert sessions == [surface.hass.data[DOMAIN][DATA_WEBHOOK][CFG_CIMD_SESSION]]


@pytest.mark.parametrize(
    ("registered", "presented", "forwarded"),
    [
        pytest.param(
            [LOOPBACK_CALLBACK],
            "http://127.0.0.1:19877/mcp/oauth/callback",
            "http://127.0.0.1:19877",
            id="registered-port",
        ),
        pytest.param(
            [LOOPBACK_CALLBACK], RUNTIME_CALLBACK, RUNTIME_ORIGIN, id="rfc8252-port"
        ),
    ],
)
async def test_the_code_leg_hands_a_loopback_client_an_envelope_not_cores_token(
    open_oauth_surface: OpenSurface,
    registered: list[str],
    presented: str,
    forwarded: str,
) -> None:
    """Core binds the grant to the runtime origin, which a redirect-less refresh
    cannot re-derive (#2248); recording it in the token is what closes that."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, registered)

    body = await surface.exchange_code(client_id, presented, forwarded)

    assert surface.client_id_of(body["access_token"]) == forwarded
    envelope = unwrap_refresh_token(DCR_KEY, body["refresh_token"], client_id)
    assert isinstance(envelope, tuple)
    core_refresh_token, forward_id = envelope
    assert forward_id == forwarded
    assert surface.core_knows(core_refresh_token)
    assert body["refresh_token"] != core_refresh_token


@pytest.mark.parametrize(
    ("registered", "presented", "forwarded"),
    [
        pytest.param(
            [LOOPBACK_CALLBACK], RUNTIME_CALLBACK, RUNTIME_ORIGIN, id="loopback-only"
        ),
        pytest.param(
            SPARK_CALLBACKS,
            SPARK_CALLBACKS[0],
            "https://oauth-redirect.googleusercontent.com",
            id="multi-origin",
        ),
        pytest.param(
            [LOOPBACK_CALLBACK, WEB_CALLBACK],
            RUNTIME_CALLBACK,
            RUNTIME_ORIGIN,
            id="hybrid",
        ),
    ],
)
async def test_an_envelope_refresh_restores_cores_token_and_identity(
    open_oauth_surface: OpenSurface,
    registered: list[str],
    presented: str,
    forwarded: str,
) -> None:
    """A redirect-less refresh proxies with the envelope's exact pair.

    Every registration shape the pre-#2248 derivation had to reject refreshes
    here: the identity comes out of the envelope, never out of the registered
    list, so there is nothing left to be ambiguous about.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, registered)
    signed_in = await surface.exchange_code(client_id, presented, forwarded)

    status, body = await refresh(surface, signed_in["refresh_token"], client_id)

    assert status == 200
    assert surface.client_id_of(body["access_token"]) == forwarded


async def test_the_envelope_wins_over_a_presented_redirect_uri(
    open_oauth_surface: OpenSurface,
) -> None:
    """The identity Core bound the grant to beats a redirect the client happens
    to send; translating that instead would present another identity and fail."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    signed_in = await surface.exchange_code(client_id, RUNTIME_CALLBACK, RUNTIME_ORIGIN)

    status, body = await refresh(
        surface,
        signed_in["refresh_token"],
        client_id,
        redirect_uri="http://127.0.0.1:60999/mcp/oauth/callback",
    )

    assert status == 200
    assert surface.client_id_of(body["access_token"]) == RUNTIME_ORIGIN


async def test_a_tampered_envelope_is_answered_locally(
    open_oauth_surface: OpenSurface,
) -> None:
    """A forged envelope is answered here, not relayed and not re-derived.

    It carries our prefix, so it is INVALID rather than ABSENT: Core cannot
    redeem it either, and the pre-envelope derivation would answer a message
    about registration shape that has nothing to do with what happened.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [WEB_CALLBACK])
    signed_in = await surface.exchange_code(
        client_id, WEB_CALLBACK, "https://a.example"
    )
    body, _, signature = signed_in["refresh_token"].rpartition(".")
    tampered = f"{body}.{'B' if signature[0] != 'B' else 'C'}{signature[1:]}"

    status, answer = await refresh(surface, tampered, client_id)

    assert status == 400
    assert answer["error"] == "invalid_grant"
    assert "could not be verified" in answer["error_description"]


async def test_an_envelope_presented_for_another_client_id_is_not_honoured(
    open_oauth_surface: OpenSurface,
) -> None:
    """A single-web-origin registration is used deliberately: its pre-envelope
    derivation would 307 into Core, so a local invalid_grant here can only come
    from the envelope's own verification."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    minted_for = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    other_client = oauth_dcr.mint_client_id(DCR_KEY, [WEB_CALLBACK])
    signed_in = await surface.exchange_code(
        minted_for, RUNTIME_CALLBACK, RUNTIME_ORIGIN
    )

    status, answer = await refresh(surface, signed_in["refresh_token"], other_client)

    assert status == 400
    assert answer["error"] == "invalid_grant"
    assert "could not be verified" in answer["error_description"]


async def test_cores_refusal_of_a_proxied_exchange_is_relayed_as_it_answered(
    open_oauth_surface: OpenSurface,
) -> None:
    """Only a 200 gets its refresh_token wrapped; an error comes back as Core
    said it, so the client sees Core's own reason."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    client_id = oauth_dcr.mint_client_id(DCR_KEY, [LOOPBACK_CALLBACK])
    form = {
        "grant_type": "authorization_code",
        "code": "never-issued",
        "client_id": RUNTIME_ORIGIN,
        "redirect_uri": RUNTIME_CALLBACK,
    }
    direct = await surface.client.post("/auth/token", data=form)

    relayed = await surface.client.post(
        TOKEN_URL, data={**form, "client_id": client_id}
    )

    assert direct.status == 400
    assert relayed.status == direct.status
    assert await relayed.json() == await direct.json()


async def test_the_code_leg_proxies_a_hybrid_metadata_client_exchange_and_wraps_it(
    open_oauth_surface: OpenSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hybrid metadata identity presenting its same-origin redirect gets wrapped.

    The authorize leg's same-origin fast path returns the client_id without
    fetching, so this exchange used to 307 and hand back Core's RAW refresh
    token, whose redirect-less refresh then derived UNREPRODUCIBLE (two web
    origins) and answered invalid_grant forever under a message claiming that
    re-authorizing would help. The code leg now pays that one fetch and records
    the identity in the token instead.
    """
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    serve_cimd_documents(monkeypatch, {CIMD_CLIENT_ID: HYBRID_CIMD_REDIRECTS})

    body = await surface.exchange_code(CIMD_CLIENT_ID, CIMD_SAME_ORIGIN_REDIRECT)

    envelope = unwrap_refresh_token(DCR_KEY, body["refresh_token"], CIMD_CLIENT_ID)
    assert isinstance(envelope, tuple)
    assert envelope[1] == CIMD_CLIENT_ID
    assert surface.core_knows(envelope[0])


async def test_refreshing_a_hybrid_metadata_envelope_keeps_the_client_id(
    open_oauth_surface: OpenSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The envelope names the identity Core bound the grant to, so the
    derivation that would answer UNREPRODUCIBLE is never consulted."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    serve_cimd_documents(monkeypatch, {CIMD_CLIENT_ID: HYBRID_CIMD_REDIRECTS})
    signed_in = await surface.exchange_code(CIMD_CLIENT_ID, CIMD_SAME_ORIGIN_REDIRECT)

    status, body = await refresh(surface, signed_in["refresh_token"], CIMD_CLIENT_ID)

    assert status == 200
    assert surface.client_id_of(body["access_token"]) == CIMD_CLIENT_ID


async def test_a_same_origin_only_metadata_client_keeps_the_direct_exchange(
    open_oauth_surface: OpenSurface, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its redirect-less refresh re-derives PASSTHROUGH, so there is nothing to
    record and Core keeps seeing the client's own address: Core's raw refresh
    token comes back, not an envelope."""
    surface = await open_oauth_surface(WEBHOOK_AUTH_HA)
    serve_cimd_documents(monkeypatch, {CIMD_CLIENT_ID: [CIMD_SAME_ORIGIN_REDIRECT]})
    form = {
        "grant_type": "authorization_code",
        "client_id": CIMD_CLIENT_ID,
        "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
    }

    unfollowed = await surface.client.post(
        TOKEN_URL, data={**form, "code": "code-1"}, allow_redirects=False
    )
    followed = await surface.client.post(
        TOKEN_URL, data={**form, "code": surface.core_code(CIMD_CLIENT_ID)}
    )

    assert unfollowed.status == 307
    assert unfollowed.headers["Location"] == "/auth/token"
    body = await followed.json()
    assert unwrap_refresh_token(DCR_KEY, body["refresh_token"], CIMD_CLIENT_ID) is (
        EnvelopeState.ABSENT
    )
