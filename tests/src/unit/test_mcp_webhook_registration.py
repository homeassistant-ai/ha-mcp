"""Registering and tearing down the in-process server's webhook (#1527, #1803).

The component's own entry points run on a real ``hass``; every request goes
through Core's HTTP server and the relay forwards to a real loopback upstream.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from aiohttp import web
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator

import custom_components.ha_mcp_tools.mcp_webhook as mw
from custom_components.ha_mcp_tools import oauth_legacy
from custom_components.ha_mcp_tools.const import (
    DATA_WEBHOOK,
    DOMAIN,
    OAUTH_BASE,
    WEBHOOK_AUTH_HA,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from custom_components.ha_mcp_tools.oauth_legacy import OAUTH_ROUTE_OWNER_KEY

from ._mcp_webhook_core import (
    OAUTH_CLIENT_ID,
    OAUTH_CLIENT_SECRET,
    OAUTH_SIGNING_KEY,
    PUBLIC_HEADERS,
    READONLY_PATH,
    SECRET_PATH,
    WEBHOOK_PATH,
    Register,
    Upstream,
    fail_view_registration,
    leave_stale_endpoint,
    legacy_provider,
)
from ._mcp_webhook_core import (
    register as register,
)
from ._mcp_webhook_core import (
    upstream as upstream,
)
from ._readonly_webhook import readonly_webhook_modules as readonly_webhook_modules

AS_URL = f"{OAUTH_BASE}/authorization-server"
REGISTER_URL = f"{OAUTH_BASE}/register"
DCR_BODY = {"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]}
DCR_KEY = "ab" * 32


async def test_readonly_alias_forwards_until_the_webhook_is_unloaded(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await register(WEBHOOK_AUTH_NONE)
    client = await hass_client_no_auth()

    resp = await client.post(READONLY_PATH, data=b"{}")
    assert resp.status == 200
    assert [seen.path for seen in upstream.requests] == [f"{SECRET_PATH}/readonly"]
    other = await client.post("/api/webhook/other-webhook/readonly", data=b"{}")
    assert other.status == 404
    assert len(upstream.requests) == 1

    await mw.async_unregister_webhook(hass)

    assert (await client.post(READONLY_PATH, data=b"{}")).status == 404
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("disable", [False, True], ids=["failed-setup", "disabled"])
async def test_readonly_alias_is_inactive_after_failed_setup_or_disable(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    disable: bool,
) -> None:
    client = await hass_client_no_auth()
    if disable:
        await register(WEBHOOK_AUTH_HA)
        assert (await client.post(READONLY_PATH, data=b"{}")).status == 401
        await mw.async_unregister_webhook(hass)
        await register(WEBHOOK_AUTH_HA, register_endpoint=False)
    else:
        fail_view_registration(hass, monkeypatch, 12)
        with pytest.raises(RuntimeError, match="router frozen"):
            await register(WEBHOOK_AUTH_HA)

    assert (await client.post(READONLY_PATH, data=b"{}")).status == 404
    assert upstream.requests == []


async def test_registering_replaces_an_endpoint_a_crashed_unload_left_behind(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A reload after a crashed unload must not trip over the old registration."""
    stale = leave_stale_endpoint(hass)
    client = await hass_client_no_auth()
    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 204
    assert stale.await_count == 1

    assert await register(WEBHOOK_AUTH_NONE) is False

    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 200
    assert stale.await_count == 1
    assert len(upstream.requests) == 1


@pytest.mark.parametrize(
    ("auth_mode", "serves_dcr"),
    [
        (WEBHOOK_AUTH_NONE, True),
        (WEBHOOK_AUTH_HA, True),
        (WEBHOOK_AUTH_LEGACY, False),
    ],
)
async def test_client_registration_is_served_by_the_modes_that_advertise_it(
    hass: HomeAssistant,
    register: Register,
    hass_client_no_auth: ClientSessionGenerator,
    auth_mode: str,
    serves_dcr: bool,
) -> None:
    """The none and ha_auth documents advertise ``/register``; legacy does not,
    so its entry must not carry the signing key that mints client ids."""
    await register(WEBHOOK_AUTH_NONE, dcr_signing_key=DCR_KEY)
    client = await hass_client_no_auth()
    assert (await client.post(REGISTER_URL, json=DCR_BODY)).status == 201
    await mw.async_unregister_webhook(hass)
    await register(auth_mode, dcr_signing_key=DCR_KEY)

    resp = await client.post(REGISTER_URL, json=DCR_BODY)

    assert resp.status == (201 if serves_dcr else 404)


async def test_invalid_dcr_key_leaves_none_mode_a_plain_proxy(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A corrupt key must not half-enable the surface: an advertised
    ``/register`` that 404s next to a ``/authorize`` that auto-approves."""
    await register(WEBHOOK_AUTH_NONE, dcr_signing_key="zz")
    client = await hass_client_no_auth()

    assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 404
    assert (await client.post(REGISTER_URL, json=DCR_BODY)).status == 404
    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 200
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("nth", [5, 10], ids=["metadata-bundle", "autoapprove-bundle"])
async def test_none_mode_discovery_failure_keeps_the_webhook_forwarding(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    nth: int,
) -> None:
    """#1978: none mode is unauthenticated by design, so the cosmetic discovery
    layer failing partway must fail OPEN: the webhook keeps forwarding, and no
    half-bound bundle is advertised (a client would probe a route that 404s)."""
    fail_view_registration(hass, monkeypatch, nth)

    await register(WEBHOOK_AUTH_NONE)

    client = await hass_client_no_auth()
    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 200
    assert len(upstream.requests) == 1
    assert mw.active_auth_mode(hass) is None
    assert (await client.get(AS_URL, headers=PUBLIC_HEADERS)).status == 404
    assert "failed to set up none-mode auto-approve discovery" in caplog.text


async def test_ha_auth_fetches_client_metadata_on_a_separate_small_pool(
    hass: HomeAssistant, register: Register
) -> None:
    """Anonymous client-metadata fetches must not take the capacity the
    authenticated relay needs for its long-lived streams."""
    await register(WEBHOOK_AUTH_HA)
    cfg = hass.data[DOMAIN][DATA_WEBHOOK]
    relay: aiohttp.ClientSession = cfg["session"]
    cimd: aiohttp.ClientSession = cfg["cimd_session"]

    assert cimd is not relay
    assert relay.connector is not None
    assert cimd.connector is not None
    assert cimd.connector.limit < relay.connector.limit

    await mw.async_unregister_webhook(hass)

    assert relay.closed
    assert cimd.closed


async def test_unknown_auth_mode_refuses_bring_up(
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    """A corrupted or hand-edited option must raise, never fall through to the
    unauthenticated forward path."""
    with pytest.raises(ValueError, match="Unknown webhook auth mode"):
        await register("bogus")

    client = await hass_client_no_auth()
    await client.post(WEBHOOK_PATH, data=b"{}")
    assert upstream.requests == []


async def test_legacy_without_credentials_refuses_bring_up_and_leaves_no_endpoint(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    with pytest.raises(ValueError, match="legacy webhook auth mode requires"):
        await register(WEBHOOK_AUTH_LEGACY, oauth_client_secret="")

    client = await hass_client_no_auth()
    await client.post(WEBHOOK_PATH, data=b"{}")
    assert upstream.requests == []
    assert DATA_WEBHOOK not in hass.data.get(DOMAIN, {})


@pytest.mark.parametrize("booting", [True, False], ids=["boot", "mid-session"])
async def test_legacy_binds_root_routes_and_flags_a_restart_only_mid_session(
    hass: HomeAssistant,
    register: Register,
    hass_client_no_auth: ClientSessionGenerator,
    booting: bool,
) -> None:
    """Routes bound after Home Assistant started are not live until a restart."""
    if booting:
        hass.set_state(CoreState.not_running)

    restart_needed = await register(WEBHOOK_AUTH_LEGACY)

    assert restart_needed is not booting
    client = await hass_client_no_auth()
    assert (await client.post(oauth_legacy.TOKEN_PATH, data={})).status != 404
    await mw.async_unregister_webhook(hass)
    assert (await client.post(oauth_legacy.TOKEN_PATH, data={})).status == 404


async def test_legacy_stays_live_on_scoped_routes_when_the_proxy_addon_owns_root(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The webhook-proxy add-on already owns the root routes. Legacy still works
    on the component's scoped routes, which metadata-honouring clients use."""
    hass.data[OAUTH_ROUTE_OWNER_KEY] = "webhook_proxy"

    restart_needed = await register(WEBHOOK_AUTH_LEGACY)

    assert restart_needed is False
    assert hass.data[OAUTH_ROUTE_OWNER_KEY] == "webhook_proxy"
    assert "legacy OAuth will serve only" in caplog.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    client = await hass_client_no_auth()
    resp = await client.post(
        WEBHOOK_PATH,
        data=b"{}",
        headers={"Authorization": f"Bearer {legacy_provider().issue_access_token()}"},
    )
    assert resp.status == 200
    assert len(upstream.requests) == 1
    assert oauth_legacy.legacy_credentials_active(
        hass, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_SIGNING_KEY
    )

    await mw.async_unregister_webhook(hass)

    assert not oauth_legacy.legacy_credentials_active(
        hass, OAUTH_CLIENT_ID, OAUTH_CLIENT_SECRET, OAUTH_SIGNING_KEY
    )


@pytest.mark.parametrize(
    ("auth_mode", "register_endpoint"),
    [(WEBHOOK_AUTH_NONE, True), (WEBHOOK_AUTH_LEGACY, False)],
    ids=["switched-away", "webhook-turned-off"],
)
async def test_leaving_legacy_flags_a_restart_to_release_the_root_routes(
    hass: HomeAssistant, register: Register, auth_mode: str, register_endpoint: bool
) -> None:
    """aiohttp cannot unbind the root views, so the repair that asks for a restart
    must stay while this registration serves no legacy provider."""
    hass.set_state(CoreState.not_running)
    assert await register(WEBHOOK_AUTH_LEGACY) is False
    await mw.async_unregister_webhook(hass)

    restart_needed = await register(auth_mode, register_endpoint=register_endpoint)

    assert restart_needed is True


async def test_failed_endpoint_registration_leaves_nothing_forwarding(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = await hass_client_no_auth()
    with monkeypatch.context() as failing:
        failing.setattr(
            mw, "async_register", MagicMock(side_effect=RuntimeError("duplicate"))
        )
        with pytest.raises(RuntimeError, match="duplicate"):
            await register(WEBHOOK_AUTH_NONE)

    await client.post(WEBHOOK_PATH, data=b"{}")
    assert upstream.requests == []
    assert DATA_WEBHOOK not in hass.data.get(DOMAIN, {})
    await register(WEBHOOK_AUTH_NONE)
    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 200
    assert len(upstream.requests) == 1


async def test_unregistering_stops_forwarding_and_closes_the_relay_session(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
) -> None:
    await register(WEBHOOK_AUTH_NONE)
    session: aiohttp.ClientSession = hass.data[DOMAIN][DATA_WEBHOOK]["session"]
    client = await hass_client_no_auth()

    await mw.async_unregister_webhook(hass)
    await mw.async_unregister_webhook(hass)

    assert session.closed
    assert DATA_WEBHOOK not in hass.data[DOMAIN]
    await client.post(WEBHOOK_PATH, data=b"{}")
    assert upstream.requests == []


@pytest.mark.parametrize("auth_mode", [WEBHOOK_AUTH_NONE, WEBHOOK_AUTH_HA])
async def test_disabled_remote_access_keeps_only_loopback_forwarding(
    hass: HomeAssistant,
    register: Register,
    upstream: Upstream,
    hass_client_no_auth: ClientSessionGenerator,
    auth_mode: str,
) -> None:
    """#1803: with the public endpoint off, a leftover one from a crashed unload
    is cleared, and the loopback forwarding the sidebar panel proxies through
    is still there."""
    stale = leave_stale_endpoint(hass)
    client = await hass_client_no_auth()
    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 204

    await register(auth_mode, register_endpoint=False)

    assert (await client.post(WEBHOOK_PATH, data=b"{}")).status == 200
    assert stale.await_count == 1
    assert upstream.requests == []
    cfg = hass.data[DOMAIN][DATA_WEBHOOK]
    assert cfg["target_url"] == f"http://127.0.0.1:{upstream.port}{SECRET_PATH}"
    assert not cfg["session"].closed
    assert mw.active_auth_mode(hass) is None

    await mw.async_unregister_webhook(hass)

    assert DATA_WEBHOOK not in hass.data[DOMAIN]
    assert cfg["session"].closed


async def test_readonly_route_is_shared_by_embedded_and_proxy_installations(
    hass: HomeAssistant,
    hass_client_no_auth: ClientSessionGenerator,
    readonly_webhook_modules,
) -> None:
    embedded, proxy = readonly_webhook_modules
    assert await async_setup_component(hass, "http", {})
    first = AsyncMock(side_effect=lambda *_args, **_kwargs: web.Response(status=200))
    second = AsyncMock(side_effect=lambda *_args, **_kwargs: web.Response(status=200))
    embedded.register_readonly_webhook(hass, "embedded", first)
    proxy.register_readonly_webhook(hass, "proxy", second)
    client = await hass_client_no_auth()

    assert (await client.post("/api/webhook/embedded/readonly")).status == 200
    assert (await client.get("/api/webhook/proxy/readonly")).status == 200

    assert first.await_count == 1
    assert second.await_count == 1
    assert first.await_args is not None
    assert first.await_args.args[1] == "embedded"
    assert first.await_args.kwargs == {"read_only": True}
    embedded.unregister_readonly_webhook(hass, "embedded")
    assert (await client.post("/api/webhook/embedded/readonly")).status == 404
    assert (await client.post("/api/webhook/proxy/readonly")).status == 200
    assert second.await_count == 2


def test_query_string_survives_the_readonly_suffix(readonly_webhook_modules) -> None:
    _, proxy = readonly_webhook_modules

    assert (
        proxy.suffixed_url("http://localhost/private/?key=value", "/readonly")
        == "http://localhost/private/readonly?key=value"
    )
