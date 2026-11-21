"""ha_auth token and revocation legs, forwarded to Core's own endpoints.

A client signs in at Core, redeems its code through the proxy and gets a
refresh token wrapped in a signed envelope; every later refresh or revocation
must reach Core carrying Core's own token and the identity Core bound it to.
Core's real ``/auth/token`` and ``/auth/revoke`` decide whether the proxy got
that right.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ._webhook_proxy_oauth_core import (
    GOOGLE_REDIRECT_URIS,
    LOOPBACK_CLIENT,
    LOOPBACK_REDIRECT,
    CimdWeb,
    CoreAuth,
    ProxyStarter,
    sign_in_through_proxy,
)
from ._webhook_proxy_oauth_core import (
    cimd_web as cimd_web,
)
from ._webhook_proxy_oauth_core import (
    component as component,
)
from ._webhook_proxy_oauth_core import (
    core_auth as core_auth,
)
from ._webhook_proxy_oauth_core import (
    fs_root as fs_root,
)
from ._webhook_proxy_oauth_core import (
    proxy_components_root as proxy_components_root,
)
from ._webhook_proxy_oauth_core import (
    relay_posts as relay_posts,
)
from ._webhook_proxy_oauth_core import (
    start_proxy_in_mode as start_proxy_in_mode,
)

CIMD_CLIENT_ID = "https://app.example/cimd.json"
CIMD_SAME_ORIGIN_REDIRECT = "https://app.example/cb"
HYBRID_CIMD_REDIRECTS = [CIMD_SAME_ORIGIN_REDIRECT, "https://eu.example/cb"]
GOOGLE_ORIGIN = "https://oauth-redirect.googleusercontent.com"


async def test_code_exchange_comes_back_with_a_wrapped_refresh_token(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """A loopback client's code exchange returns an envelope naming the
    identity Core bound the grant to and carrying Core's own refresh token."""
    proxy = await start_proxy_in_mode("ha_auth")

    session = await sign_in_through_proxy(proxy, core_auth)

    assert session.envelope.startswith("hamcp-rt-")
    assert core_auth.refresh_token_exists(session.core_refresh_token)
    assert proxy.module("oauth_indirect").unwrap_refresh_token(
        proxy.dcr_key, session.envelope, session.client_id
    ) == (session.core_refresh_token, LOOPBACK_CLIENT)


async def test_refreshing_an_envelope_restores_core_token_and_identity(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """A redirect-less loopback refresh reaches Core with the envelope's token
    and client_id; Core accepts it only when both are the pair it issued."""
    proxy = await start_proxy_in_mode("ha_auth")
    session = await sign_in_through_proxy(proxy, core_auth)

    response = await proxy.post(
        "token",
        {
            "grant_type": "refresh_token",
            "refresh_token": session.envelope,
            "client_id": session.client_id,
        },
    )

    assert response.status == 200
    assert (await response.json())["access_token"]


async def test_core_never_redeems_the_envelope_itself(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """The reason the proxy unwraps: Core cannot refresh with the envelope."""
    proxy = await start_proxy_in_mode("ha_auth")
    session = await sign_in_through_proxy(proxy, core_auth)

    response = await core_auth.client.post(
        "/auth/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": session.envelope,
            "client_id": LOOPBACK_CLIENT,
        },
    )

    assert response.status == 400


async def test_refresh_with_redirect_translates_the_presented_origin(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """A redirect-carrying refresh of a token Core issued before envelopes
    shipped stays usable for a multi-origin client."""
    proxy = await start_proxy_in_mode("ha_auth")
    client_id = await proxy.register(GOOGLE_REDIRECT_URIS)
    tokens = await core_auth.tokens_for(GOOGLE_ORIGIN, GOOGLE_REDIRECT_URIS[0])
    form = {
        "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"],
        "client_id": client_id,
    }

    answered = await proxy.post(
        "token", {**form, "redirect_uri": GOOGLE_REDIRECT_URIS[0]}
    )
    unanswerable = await proxy.post("token", form)

    assert answered.status == 200
    assert (await answered.json())["access_token"]
    # Without the redirect the origin is unrecoverable, so it is refused.
    assert unanswerable.status == 400


async def test_refresh_rejects_an_unreproducible_identity_without_asking_core(
    start_proxy_in_mode: ProxyStarter, relay_posts: MagicMock
) -> None:
    """A pre-#2248 multi-origin refresh never enters Core's failed-login
    accounting. Only tokens minted before the signed envelope reach this
    guard: an envelope names its identity outright."""
    proxy = await start_proxy_in_mode("ha_auth")
    client_id = await proxy.register(GOOGLE_REDIRECT_URIS)

    response = await proxy.post(
        "token",
        {
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": "opaque",
        },
    )

    assert response.status == 400
    assert (await response.json())["error"] == "invalid_grant"
    relay_posts.assert_not_called()


async def test_unchanged_token_exchange_is_redirected_to_core(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth, relay_posts: MagicMock
) -> None:
    """An unchanged token body stays client-side so Core sees the real IP."""
    proxy = await start_proxy_in_mode("ha_auth")
    code = await core_auth.code_for(LOOPBACK_CLIENT, LOOPBACK_REDIRECT)

    response = await proxy.post(
        "token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": LOOPBACK_CLIENT,
            "redirect_uri": LOOPBACK_REDIRECT,
        },
    )

    assert response.status == 307
    assert response.headers["Location"] == "/auth/token"
    assert response.headers["Cache-Control"] == "no-store"
    relay_posts.assert_not_called()


async def test_core_errors_are_relayed_unwrapped(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """Only a 200 gets its refresh_token wrapped; an error is Core's answer."""
    proxy = await start_proxy_in_mode("ha_auth")
    client_id = await proxy.register([LOOPBACK_REDIRECT])
    form = {
        "grant_type": "authorization_code",
        "code": "never-issued",
        "client_id": LOOPBACK_CLIENT,
        "redirect_uri": LOOPBACK_REDIRECT,
    }

    relayed = await proxy.post("token", {**form, "client_id": client_id})
    direct = await core_auth.client.post("/auth/token", data=form)

    assert relayed.status == direct.status == 400
    assert await relayed.json() == await direct.json()


async def test_code_exchange_for_a_hybrid_client_is_proxied_and_wrapped(
    start_proxy_in_mode: ProxyStarter,
    core_auth: CoreAuth,
    cimd_web: CimdWeb,
    monkeypatch: pytest.MonkeyPatch,
    relay_posts: MagicMock,
) -> None:
    """A hybrid CIMD identity presenting its same-origin redirect gets wrapped.

    The authorize leg's same-origin fast path returns the client_id without
    fetching, so this exchange used to 307 and hand back Core's RAW refresh
    token, whose redirect-less refresh then derived UNREPRODUCIBLE (two web
    origins) and answered invalid_grant forever. The code leg now pays that one
    fetch, proxies, and records the identity in the token instead.
    """
    proxy = await start_proxy_in_mode("ha_auth")
    await cimd_web.attach(proxy, monkeypatch)
    cimd_web.serve(CIMD_CLIENT_ID, HYBRID_CIMD_REDIRECTS)
    code = await core_auth.code_for(CIMD_CLIENT_ID, CIMD_SAME_ORIGIN_REDIRECT)

    response = await proxy.post(
        "token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CIMD_CLIENT_ID,
            "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
        },
    )

    assert response.status == 200
    relay_posts.assert_called_once()
    unwrapped = proxy.module("oauth_indirect").unwrap_refresh_token(
        proxy.dcr_key, (await response.json())["refresh_token"], CIMD_CLIENT_ID
    )
    assert isinstance(unwrapped, tuple)
    core_token, forward_id = unwrapped
    assert forward_id == CIMD_CLIENT_ID
    assert core_auth.refresh_token_exists(core_token)


async def test_refresh_of_a_hybrid_envelope_keeps_the_client_id(
    start_proxy_in_mode: ProxyStarter,
    core_auth: CoreAuth,
    cimd_web: CimdWeb,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refresh leg of that exchange proxies the untranslated client_id,
    which Core accepts because it is the one the token was issued to."""
    proxy = await start_proxy_in_mode("ha_auth")
    await cimd_web.attach(proxy, monkeypatch)
    cimd_web.serve(CIMD_CLIENT_ID, HYBRID_CIMD_REDIRECTS)
    code = await core_auth.code_for(CIMD_CLIENT_ID, CIMD_SAME_ORIGIN_REDIRECT)
    exchanged = await proxy.post(
        "token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CIMD_CLIENT_ID,
            "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
        },
    )

    response = await proxy.post(
        "token",
        {
            "grant_type": "refresh_token",
            "refresh_token": (await exchanged.json())["refresh_token"],
            "client_id": CIMD_CLIENT_ID,
        },
    )

    assert response.status == 200
    assert (await response.json())["access_token"]


async def test_code_exchange_for_a_same_origin_only_client_is_still_redirected(
    start_proxy_in_mode: ProxyStarter,
    core_auth: CoreAuth,
    cimd_web: CimdWeb,
    monkeypatch: pytest.MonkeyPatch,
    relay_posts: MagicMock,
) -> None:
    """A document with only same-origin redirects keeps the 307.

    Its redirect-less refresh re-derives PASSTHROUGH, so there is nothing to
    record and Core must keep seeing the client's own address.
    """
    proxy = await start_proxy_in_mode("ha_auth")
    await cimd_web.attach(proxy, monkeypatch)
    cimd_web.serve(CIMD_CLIENT_ID, [CIMD_SAME_ORIGIN_REDIRECT])
    code = await core_auth.code_for(CIMD_CLIENT_ID, CIMD_SAME_ORIGIN_REDIRECT)

    response = await proxy.post(
        "token",
        {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CIMD_CLIENT_ID,
            "redirect_uri": CIMD_SAME_ORIGIN_REDIRECT,
        },
    )

    assert response.status == 307
    assert response.headers["Location"] == "/auth/token"
    # The document was consulted, and nothing was proxied because of it.
    assert cimd_web.fetches == 1
    relay_posts.assert_not_called()


async def test_token_revocation_unwraps_the_envelope_before_forwarding(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """Core must revoke ITS token, not the envelope we handed the client.

    ``/auth/token`` answers 200 to ``action=revoke`` even for a token it has
    never seen, so forwarding the envelope would report success while leaving
    the session live. The same revocation sent straight to Core shows it.
    """
    proxy = await start_proxy_in_mode("ha_auth")
    through_proxy = await sign_in_through_proxy(proxy, core_auth)
    straight_to_core = await sign_in_through_proxy(proxy, core_auth)

    revoked = await proxy.post(
        "token", {"action": "revoke", "token": through_proxy.envelope}
    )
    ignored = await core_auth.client.post(
        "/auth/token",
        data={"action": "revoke", "token": straight_to_core.envelope},
    )

    assert revoked.status == ignored.status == 200
    assert not core_auth.refresh_token_exists(through_proxy.core_refresh_token)
    assert core_auth.refresh_token_exists(straight_to_core.core_refresh_token)


async def test_token_revocation_of_a_plain_token_is_redirected_to_core(
    start_proxy_in_mode: ProxyStarter, relay_posts: MagicMock
) -> None:
    """A revoke carrying no envelope 307s to Core and the proxy makes no call."""
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.post(
        "token", {"action": "revoke", "token": "core-opaque-refresh-token"}
    )

    assert response.status == 307
    assert response.headers["Location"] == "/auth/token"
    relay_posts.assert_not_called()


async def test_scoped_revoke_swaps_the_envelope_for_cores_token(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """The envelope is swapped for Core's own token before Core sees it.

    Core's ``/auth/revoke`` answers 200 for a token it has never seen, so a
    client posting the envelope there directly would be told its session was
    revoked while it stayed live.
    """
    proxy = await start_proxy_in_mode("ha_auth")
    through_proxy = await sign_in_through_proxy(proxy, core_auth)
    straight_to_core = await sign_in_through_proxy(proxy, core_auth)

    revoked = await proxy.post("revoke", {"token": through_proxy.envelope})
    ignored = await core_auth.client.post(
        "/auth/revoke", data={"token": straight_to_core.envelope}
    )

    assert revoked.status == ignored.status == 200
    assert not core_auth.refresh_token_exists(through_proxy.core_refresh_token)
    assert core_auth.refresh_token_exists(straight_to_core.core_refresh_token)


async def test_scoped_revoke_of_a_plain_token_is_redirected_to_core(
    start_proxy_in_mode: ProxyStarter, relay_posts: MagicMock
) -> None:
    """A token that is not ours reaches Core unchanged, from the client."""
    proxy = await start_proxy_in_mode("ha_auth")

    response = await proxy.post("revoke", {"token": "core-opaque-refresh-token"})

    assert response.status == 307
    assert response.headers["Location"] == "/auth/revoke"
    relay_posts.assert_not_called()


async def test_a_revocation_core_never_answers_is_told_to_retry(
    start_proxy_in_mode: ProxyStarter, core_auth: CoreAuth
) -> None:
    """RFC 7009 section 2.2.1: a failed forward of a revocation is a 503 that
    names a retry delay, so the client keeps the token and tries again."""
    proxy = await start_proxy_in_mode("ha_auth")
    session = await sign_in_through_proxy(proxy, core_auth)
    api = proxy.hass.config.api
    assert api is not None
    api.port = 9

    response = await proxy.post("revoke", {"token": session.envelope})

    assert response.status == 503
    assert response.headers["Retry-After"] == "5"
    assert core_auth.refresh_token_exists(session.core_refresh_token)
