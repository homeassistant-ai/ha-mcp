"""The ``ha_mcp_tools`` capability gate (``component_api``) on real Home Assistant Core.

The server's own client probes ``ha_mcp_tools/info`` on a real ``hass``: the
component's commands answer it (``component_core_client``), or no component is
installed (``core_client``) and Core answers ``unknown_command``. Another
component version is the component's own constants changed; a Core restart is
the pooled WebSocket connection closing or being replaced.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from freezegun.api import TickingDateTimeFactory
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.core_config import Config
from pytest_homeassistant.common import CLIENT_ID, MockUser
from websockets.exceptions import ConnectionClosedError

from custom_components.ha_mcp_tools import websocket_api as component_commands
from custom_components.ha_mcp_tools.const import COMPONENT_VERSION
from custom_components.ha_mcp_tools.websocket_api import system
from custom_components.ha_mcp_tools.websocket_api.constants import (
    CAPABILITIES,
    LIMITS,
    SCHEMA_VERSION,
    WS_INFO,
    WS_OVERVIEW,
    WS_REGISTRY_FIELDS,
)
from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp._vendor.fastmcp.server.auth import AccessToken
from ha_mcp._vendor.mcp.server.auth.middleware.auth_context import auth_context_var
from ha_mcp._vendor.mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.tools import component_api
from ha_mcp.tools.component_api import (
    SUPPORTED_SCHEMA_VERSION,
    ComponentCaps,
    ComponentDiscoveryError,
    ask_component,
    component_probe_auth_kind,
    component_supports,
    get_component_caps,
    invalidate_caps,
)

from ._client_spies import sent_commands
from ._command_faults import dropped_on, pooled_socket, received
from ._component_frames import older_component

# ``get_component_caps``: an inconclusive probe holds for 30 seconds, a
# component found absent for five minutes.
FAILURE_WINDOW_S = 30
ABSENCE_WINDOW_S = 300
# A capability the running component gains when it is upgraded.
UPGRADED = "added_by_the_upgrade"
# Core's freezer also freezes the event loop's clock; a ticking one keeps
# the client's WebSocket handshake polling.
TICKING = pytest.mark.freeze_time(tick=True)


def _at(freezer: TickingDateTimeFactory, mark: datetime, seconds: float) -> None:
    """Move the clock to ``seconds`` after ``mark``, a reading taken right after
    the probe that opened the window, so setup time never counts against it."""
    freezer.move_to(mark + timedelta(seconds=seconds))


def _info_probes(spy: AsyncMock) -> int:
    return sum(1 for call in spy.call_args_list if call.args[0] == WS_INFO)


async def _restart_core(client: HomeAssistantClient) -> None:
    """Core restarted: the pooled connection closed and the pool opened another."""
    await websocket_manager.disconnect()
    await pooled_socket(client)


def _component_not_set_up(hass: HomeAssistant, *commands: str) -> None:
    """Core is up, but the component has not registered ``commands`` yet."""
    for command in commands:
        del hass.data[websocket_api.DOMAIN][command]


async def test_the_component_info_is_what_routing_may_use(
    component_core_client: HomeAssistantClient,
) -> None:
    """Routing goes through exactly the commands the running component lists."""
    caps = await get_component_caps(component_core_client)

    assert caps is not None
    assert caps.capabilities == frozenset(CAPABILITIES)
    assert caps.schema_version == SCHEMA_VERSION == SUPPORTED_SCHEMA_VERSION
    assert caps.component_version == COMPONENT_VERSION
    assert caps.limits == LIMITS
    assert all(component_supports(caps, name) for name in CAPABILITIES)
    assert not component_supports(caps, UPGRADED)
    assert not component_supports(None, CAPABILITIES[0])


async def test_a_client_without_tls_verification_probes_on_its_own_connection(
    component_core_client: HomeAssistantClient,
) -> None:
    """A ``verify_ssl=False`` client that probed over a verifying connection
    could never reach the component on an HTTPS install with its own
    certificate."""
    assert component_core_client.verify_ssl is False

    assert await get_component_caps(component_core_client) is not None

    assert (
        websocket_manager.pooled_client(
            url=component_core_client.base_url,
            token=component_core_client.token,
            verify_ssl=False,
        )
        is not None
    )


async def test_one_info_probe_serves_every_later_call(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    with received(hass, WS_INFO) as seen:
        first = await get_component_caps(component_core_client)
        second = await get_component_caps(component_core_client)

    assert first is second
    assert len(seen[WS_INFO]) == 1


@pytest.mark.parametrize("absence", ["no_component", "unsupported_schema"])
@TICKING
async def test_an_absent_component_is_not_asked_again_for_five_minutes(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
    absence: str,
) -> None:
    """A definitive absence keeps its five minutes past the failure window, and
    strict callers fall back to Core's own commands meanwhile."""
    if absence == "unsupported_schema":
        component_commands.async_register_commands(hass)
        monkeypatch.setattr(system, "SCHEMA_VERSION", SUPPORTED_SCHEMA_VERSION + 1)
    socket = await pooled_socket(core_client)

    with sent_commands(socket) as sent:
        assert await get_component_caps(core_client) is None
        probed = datetime.now(UTC)
        _at(freezer, probed, FAILURE_WINDOW_S + 1)
        assert await get_component_caps(core_client, strict=True) is None
        _at(freezer, probed, ABSENCE_WINDOW_S - 1)
        assert await get_component_caps(core_client) is None

    assert _info_probes(sent) == 1


@TICKING
async def test_a_component_installed_after_the_probe_is_adopted_after_five_minutes(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
) -> None:
    assert await get_component_caps(core_client) is None
    probed = datetime.now(UTC)
    component_commands.async_register_commands(hass)
    _at(freezer, probed, ABSENCE_WINDOW_S + 1)

    assert isinstance(await get_component_caps(core_client), ComponentCaps)


async def test_an_unsupported_schema_version_is_logged_once(
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(system, "SCHEMA_VERSION", SUPPORTED_SCHEMA_VERSION + 1)

    with caplog.at_level(logging.WARNING, logger=component_api.__name__):
        first = await get_component_caps(component_core_client)
        second = await get_component_caps(component_core_client)

    assert first is None
    assert second is None
    warnings = [
        r.getMessage() for r in caplog.records if "schema_version" in r.getMessage()
    ]
    assert len(warnings) == 1
    assert str(SUPPORTED_SCHEMA_VERSION + 1) in warnings[0]
    assert str(SUPPORTED_SCHEMA_VERSION) in warnings[0]


@TICKING
async def test_a_probe_that_lost_its_connection_is_retried_after_30_seconds(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
) -> None:
    """While Home Assistant is unreachable each call answers from the cache
    instead of waiting on another connect (review-5 M8); once the window has
    passed the component is found again."""
    socket = await pooled_socket(component_core_client)
    with dropped_on(hass, WS_INFO, socket):
        assert await get_component_caps(component_core_client) is None
    probed = datetime.now(UTC)

    with received(hass, WS_INFO) as seen:
        _at(freezer, probed, FAILURE_WINDOW_S - 1)
        assert await get_component_caps(component_core_client) is None
        assert not seen[WS_INFO]
        _at(freezer, probed, FAILURE_WINDOW_S + 1)
        assert isinstance(
            await get_component_caps(component_core_client), ComponentCaps
        )

    assert len(seen[WS_INFO]) == 1


async def test_invalidated_caps_are_probed_again(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await get_component_caps(component_core_client)

    with received(hass, WS_INFO) as seen:
        invalidate_caps(component_core_client)
        await get_component_caps(component_core_client)

    assert len(seen[WS_INFO]) == 1


@TICKING
async def test_working_caps_outlive_every_negative_window(
    component_core_client: HomeAssistantClient, freezer: TickingDateTimeFactory
) -> None:
    first = await get_component_caps(component_core_client)
    _at(freezer, datetime.now(UTC), ABSENCE_WINDOW_S * 10)

    assert isinstance(first, ComponentCaps)
    assert await get_component_caps(component_core_client) is first


async def test_a_component_upgraded_across_a_core_restart_is_adopted(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server outlives the restart that loaded the new component, and must
    not keep routing on the old one's caps (#2967)."""
    first = await get_component_caps(component_core_client)
    monkeypatch.setattr(system, "CAPABILITIES", [*CAPABILITIES, UPGRADED])
    await _restart_core(component_core_client)

    with received(hass, WS_INFO) as seen:
        second = await get_component_caps(component_core_client)
        third = await get_component_caps(component_core_client)

    assert not component_supports(first, UPGRADED)
    assert component_supports(second, UPGRADED)
    assert third is second
    assert len(seen[WS_INFO]) == 1


async def test_caps_are_probed_again_once_their_connection_closed(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core restarting closed the connection and the pool has not replaced it
    yet: caps from the component that went away are not trusted."""
    await get_component_caps(component_core_client)
    await (await pooled_socket(component_core_client)).disconnect()

    with received(hass, WS_INFO) as seen:
        await get_component_caps(component_core_client)

    assert len(seen[WS_INFO]) == 1


async def test_a_reprobe_that_loses_its_connection_keeps_the_caps_that_worked(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Right after a Core restart the re-probe can fail before Core answers; a
    cached failure would refuse strict callers once Core is back. The caps
    that worked stay, and are probed again even when no connection is left to
    compare them with."""
    working = await get_component_caps(component_core_client)
    await _restart_core(component_core_client)
    with dropped_on(hass, WS_INFO, await pooled_socket(component_core_client)):
        during = await get_component_caps(component_core_client, strict=True)
    await websocket_manager.disconnect()
    monkeypatch.setattr(system, "CAPABILITIES", [*CAPABILITIES, UPGRADED])

    after = await get_component_caps(component_core_client, strict=True)

    assert during is working
    assert component_supports(after, UPGRADED)


@TICKING
async def test_a_component_still_starting_after_a_restart_is_found_within_30_seconds(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
) -> None:
    """Core answers before the component has registered its commands; that
    ``unknown_command`` must not hide the component for five minutes."""
    await get_component_caps(component_core_client)
    _component_not_set_up(hass, WS_INFO)
    await _restart_core(component_core_client)
    during = await get_component_caps(component_core_client)
    probed = datetime.now(UTC)

    component_commands.async_register_commands(hass)
    _at(freezer, probed, FAILURE_WINDOW_S + 1)

    assert during is None
    assert isinstance(await get_component_caps(component_core_client), ComponentCaps)


async def test_a_command_the_component_does_not_advertise_is_not_sent(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A component from before ``registry_fields`` is installed: the question
    is answered with None and the component is sent nothing."""
    older_component(monkeypatch, "registry_fields")

    with received(hass, WS_REGISTRY_FIELDS) as seen:
        reply = await ask_component(
            component_core_client, "registry_fields", registry="label"
        )

    assert reply is None
    assert not seen[WS_REGISTRY_FIELDS]


@TICKING
async def test_a_command_refused_while_starting_leaves_the_short_window(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
) -> None:
    """A component command refused in the same restart gap drops the caps; the
    probe that follows, still in the gap, gets the 30 seconds too."""
    await get_component_caps(component_core_client)
    _component_not_set_up(hass, WS_INFO, WS_OVERVIEW)

    assert await ask_component(component_core_client, "overview") is None
    assert await get_component_caps(component_core_client) is None
    probed = datetime.now(UTC)

    component_commands.async_register_commands(hass)
    _at(freezer, probed, FAILURE_WINDOW_S + 1)
    assert isinstance(await get_component_caps(component_core_client), ComponentCaps)


@TICKING
async def test_a_failed_reprobe_never_hands_a_malformed_snapshot_to_a_strict_caller(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
) -> None:
    """Caps read from a malformed ``info`` are not caps that worked."""
    socket = await pooled_socket(component_core_client)
    with patch.object(system, "SCHEMA_VERSION", "1"):
        assert await get_component_caps(component_core_client) is not None
    _at(freezer, datetime.now(UTC), FAILURE_WINDOW_S + 1)

    with (
        dropped_on(hass, WS_INFO, socket),
        pytest.raises(ComponentDiscoveryError),
    ):
        await get_component_caps(component_core_client, strict=True)


@contextmanager
def _signed_in(ha_token: str) -> Iterator[None]:
    """An OAuth request of the user whose Home Assistant token is ``ha_token``."""
    user = AuthenticatedUser(
        AccessToken(
            token="mcp-access-token",
            client_id="mcp-client",
            scopes=[],
            claims={"ha_token": ha_token},
        )
    )
    reset = auth_context_var.set(user)
    try:
        yield
    finally:
        auth_context_var.reset(reset)


@pytest.fixture
async def oauth_proxy(
    core_client: HomeAssistantClient,
) -> AsyncIterator[OAuthProxyClient]:
    """The server's OAuth client: one object naming each request's own client."""
    proxy = OAuthProxyClient(core_client.base_url)
    try:
        yield proxy
    finally:
        await proxy.close()


async def test_each_oauth_user_is_probed_once_however_requests_alternate(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    oauth_proxy: OAuthProxyClient,
    hass_access_token: str,
    hass_owner_user: MockUser,
) -> None:
    owner = await hass.auth.async_create_refresh_token(hass_owner_user, CLIENT_ID)
    tokens = [hass_access_token, hass.auth.async_create_access_token(owner)]

    with received(hass, WS_INFO) as seen:
        for token in [*tokens, *tokens, tokens[0]]:
            with _signed_in(token):
                assert isinstance(await get_component_caps(oauth_proxy), ComponentCaps)

    assert len(seen[WS_INFO]) == 2


async def test_a_non_admins_refused_probe_leaves_the_admins_caps(
    component_core_client: HomeAssistantClient,
    oauth_proxy: OAuthProxyClient,
    hass_access_token: str,
    hass_read_only_access_token: str,
) -> None:
    """``info`` is admin-only: a non-admin's refused probe must not fail the
    administrator's strict reads for the failure window."""
    with _signed_in(hass_access_token):
        admin_caps = await get_component_caps(oauth_proxy, strict=True)
    with (
        _signed_in(hass_read_only_access_token),
        pytest.raises(ComponentDiscoveryError),
    ):
        await get_component_caps(oauth_proxy, strict=True)

    with _signed_in(hass_access_token):
        assert await get_component_caps(oauth_proxy, strict=True) is admin_caps
        assert component_probe_auth_kind(oauth_proxy) is None


@asynccontextmanager
async def _failing_info(
    failure: str, hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[type[BaseException] | None]:
    """``info`` fails as ``failure`` meanwhile; yields the error the probe meets."""
    socket = await pooled_socket(client)
    if failure == "connection":
        with dropped_on(hass, WS_INFO, socket):
            yield HomeAssistantConnectionError
    elif failure == "command":
        # The component's handler fails, and Core answers with an error.
        with patch.object(
            Config,
            "time_zone",
            new_callable=PropertyMock,
            create=True,
            side_effect=RuntimeError("core drift"),
        ):
            yield HomeAssistantCommandError
    elif failure == "unexpected":
        # A close seen while sending is re-raised as it is, not as Home
        # Assistant's error.
        with patch.object(
            socket,
            "send_command",
            AsyncMock(side_effect=ConnectionClosedError(None, None)),
        ):
            yield ConnectionClosedError
    elif failure == "malformed":
        with patch.object(system, "SCHEMA_VERSION", "one"):
            yield None
    else:
        with patch.object(system, "CAPABILITIES", [*CAPABILITIES, 17]):
            yield None


@pytest.mark.parametrize(
    "failure", ["connection", "command", "unexpected", "malformed", "malformed_caps"]
)
@TICKING
async def test_failed_discovery_refuses_strict_callers_for_30_seconds(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    freezer: TickingDateTimeFactory,
    failure: str,
) -> None:
    """A strict caller (a write) must not take a failed probe for an absent
    component, while reads keep what they can use; the failure then clears."""
    client = component_core_client
    async with _failing_info(failure, hass, client) as error:
        with pytest.raises(ComponentDiscoveryError) as fresh:
            await get_component_caps(client, strict=True)
        probed = datetime.now(UTC)
        # The strict caller whose own probe failed gets that error.
        cause = fresh.value.__cause__
        assert (cause is None) if error is None else isinstance(cause, error)
        first_read = await get_component_caps(client)
        assert component_supports(first_read, "search") is (failure == "malformed_caps")
        with pytest.raises(ComponentDiscoveryError) as cached:
            await get_component_caps(client, strict=True)
        # A failure read back from the cache carries only its text.
        assert cached.value.__cause__ is None
        assert component_probe_auth_kind(client) is None

    _at(freezer, probed, FAILURE_WINDOW_S - 1)
    assert await get_component_caps(client) is first_read
    with pytest.raises(ComponentDiscoveryError):
        await get_component_caps(client, strict=True)
    _at(freezer, probed, FAILURE_WINDOW_S + 1)
    assert component_supports(await get_component_caps(client, strict=True), "search")


async def test_a_component_without_a_command_lets_strict_callers_fall_back(
    component_core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A component that predates ``dashboard_edit`` answered: the write uses
    Core's own commands instead of being refused."""
    assert "dashboard_edit" in CAPABILITIES
    older = [name for name in CAPABILITIES if name != "dashboard_edit"]
    monkeypatch.setattr(system, "CAPABILITIES", older)

    caps = await get_component_caps(component_core_client, strict=True)

    assert caps is not None
    assert not component_supports(caps, "dashboard_edit")


@TICKING
async def test_a_refused_probe_is_a_permission_refusal_until_one_succeeds(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
    hass_read_only_user: MockUser,
    freezer: TickingDateTimeFactory,
) -> None:
    """A fire tool stops before sending with a token Core refused; the mark
    goes once the user may use the component."""
    assert await get_component_caps(read_only_client) is None
    probed = datetime.now(UTC)
    assert component_probe_auth_kind(read_only_client) == "permission"

    await hass.auth.async_update_user(hass_read_only_user, group_ids=[GROUP_ID_ADMIN])
    _at(freezer, probed, FAILURE_WINDOW_S + 1)

    assert await get_component_caps(read_only_client) is not None
    assert component_probe_auth_kind(read_only_client) is None


# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_rejected_token_is_marked_as_a_token_rejection(
    core_client: HomeAssistantClient,
) -> None:
    client = HomeAssistantClient(
        base_url=core_client.base_url, token="not-a-token", verify_ssl=False
    )
    try:
        assert await get_component_caps(client) is None
        assert component_probe_auth_kind(client) == "token"
    finally:
        await client.close()
