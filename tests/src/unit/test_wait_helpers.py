"""The waits a write or service call runs before its tool returns (#381, #1152, #1395).

Each wait watches Core's events over the WebSocket and samples REST, or polls
REST alone when Core serves no WebSocket. Core answers every sample and sends
every event: a test changes Core's state once Core has answered a given sample
(``answered``), so what each sample saw is fixed, and no test waits on a clock
beyond a shortened timeout.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import ExitStack, asynccontextmanager, contextmanager
from typing import Any

import httpx
import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import ws_waiters
from ha_mcp.tools.ws_waiters import (
    wait_for_automation_entity_by_unique_id,
    wait_for_entity_registered,
    wait_for_entity_removed,
    wait_for_state_change,
)

from ._command_faults import dropped_on, pooled_socket, received, unanswered
from ._ws_transport import abort_transport, answers_unread

ENTITY = "light.test"
# Long enough for every Core answer on loopback; a broken path fails in this.
SETTLES_S = 5.0
# A waiter's own timeout when the test must see it end before that: its last
# sample at the timeout would hide a broken event path.
NEVER_S = 3600.0
# How long a negative test waits for a wake-up that must not come.
QUIET_S = 0.1
POLL_S = 0.01


@contextmanager
def answered(
    client: HomeAssistantClient,
    changes: Mapping[int, Callable[[], object]] | None = None,
) -> Iterator[list[int]]:
    """The status of each REST answer ``client`` gets meanwhile.

    ``changes[n]`` runs as the n-th answer arrives: Core has already read its
    state for that answer, and the waiter has not seen it yet.
    """
    statuses: list[int] = []

    async def on_answer(response: httpx.Response) -> None:
        statuses.append(response.status_code)
        change = (changes or {}).get(len(statuses))
        if change is not None:
            result = change()
            if inspect.isawaitable(result):
                await result

    hooks = client.httpx_client.event_hooks["response"]
    hooks.append(on_answer)
    try:
        yield statuses
    finally:
        hooks.remove(on_answer)


@contextmanager
def delivered(socket: HomeAssistantWebSocketClient) -> Iterator[list[str]]:
    """Entity ids of the ``state_changed`` events ``socket`` dispatched meanwhile.

    Added before a waiter's handler, so it runs just before it on each event.
    """
    seen: list[str] = []

    async def record(event: dict[str, Any]) -> None:
        seen.append(event["data"]["entity_id"])

    socket.add_event_handler("state_changed", record)
    try:
        yield seen
    finally:
        socket.remove_event_handler("state_changed", record)


@contextmanager
def dropped_on_second_subscribe(
    hass: HomeAssistant, socket: HomeAssistantWebSocketClient
) -> Iterator[None]:
    """Core takes the first ``subscribe_events``; ``socket`` drops at the second."""
    core_handler, schema = hass.data[websocket_api.DOMAIN]["subscribe_events"]
    calls: list[str] = []

    def drop_second(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        calls.append(msg["event_type"])
        if len(calls) == 2:
            abort_transport(socket)
            return
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, "subscribe_events", drop_second, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(
            hass, "subscribe_events", core_handler, schema
        )


@asynccontextmanager
async def _served_client(
    hass: HomeAssistant, aiohttp_client: Any, token: str
) -> AsyncIterator[HomeAssistantClient]:
    from ha_mcp.client.websocket_client import websocket_manager

    server = await aiohttp_client(hass.http.app)
    client = HomeAssistantClient(
        base_url=str(server.make_url("")).rstrip("/"), token=token, verify_ssl=False
    )
    try:
        yield client
    finally:
        try:
            await websocket_manager.disconnect()
        finally:
            await client.close()


@pytest.fixture
async def rest_only_client(
    hass: HomeAssistant,
    aiohttp_client: Any,
    hass_access_token: str,
    socket_enabled: None,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a Home Assistant serving REST but no WebSocket."""
    assert await async_setup_component(hass, "api", {})
    async with _served_client(hass, aiohttp_client, hass_access_token) as client:
        yield client


@pytest.fixture
async def api_less_client(
    hass: HomeAssistant,
    aiohttp_client: Any,
    hass_access_token: str,
    socket_enabled: None,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a Home Assistant answering 404 to every API call."""
    assert await async_setup_component(hass, "http", {})
    async with _served_client(hass, aiohttp_client, hass_access_token) as client:
        yield client


@pytest.fixture
def events_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only an event wakes a WebSocket waiter: its REST backstop never fires."""
    monkeypatch.setattr(ws_waiters, "_POLLING_BACKSTOP_INTERVAL", 3600.0)


@pytest.fixture
def fast_backstop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A WebSocket waiter samples REST every ``POLL_S`` with no event."""
    monkeypatch.setattr(ws_waiters, "_POLLING_BACKSTOP_INTERVAL", POLL_S)


# --- REST polling, when Core serves no WebSocket ---------------------------


async def test_rest_registered_entity_found_on_first_poll(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """A created entity that already has its state is confirmed at once."""
    hass.states.async_set(ENTITY, "on")
    with answered(rest_only_client) as statuses:
        assert await wait_for_entity_registered(rest_only_client, ENTITY) is True
    assert statuses == [200]


async def test_rest_registered_keeps_polling_past_404(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """An entity whose state appears only after a few polls is still confirmed,
    instead of the write being reported as not queryable."""
    create = {2: lambda: hass.states.async_set(ENTITY, "off")}
    with answered(rest_only_client, create) as statuses:
        result = await wait_for_entity_registered(
            rest_only_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is True
    assert statuses == [404, 404, 200]


async def test_rest_registered_never_appearing_times_out(
    rest_only_client: HomeAssistantClient,
) -> None:
    """An entity that never appears is reported as not registered."""
    result = await wait_for_entity_registered(
        rest_only_client, ENTITY, timeout=QUIET_S, poll_interval=POLL_S
    )
    assert result is False


async def test_rest_registered_unreachable_core_raises(
    unreachable_client: HomeAssistantClient,
) -> None:
    """An unreachable Home Assistant is an error, not a slow registration."""
    with pytest.raises(HomeAssistantConnectionError):
        await wait_for_entity_registered(
            unreachable_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )


async def test_rest_removed_entity_already_gone(
    rest_only_client: HomeAssistantClient,
) -> None:
    """A deleted entity Core no longer knows is confirmed removed at once."""
    with answered(rest_only_client) as statuses:
        assert await wait_for_entity_removed(rest_only_client, ENTITY) is True
    assert statuses == [404]


async def test_rest_removed_keeps_polling_until_gone(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """An entity that lingers for a few polls after its delete is still
    confirmed removed once Core drops it."""
    remove = {2: lambda: hass.states.async_remove(ENTITY)}
    hass.states.async_set(ENTITY, "on")
    with answered(rest_only_client, remove) as statuses:
        result = await wait_for_entity_removed(
            rest_only_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is True
    assert statuses == [200, 200, 404]


async def test_rest_removed_lingering_entity_times_out(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """An entity still present after the wait is reported as not removed."""
    hass.states.async_set(ENTITY, "on")
    result = await wait_for_entity_removed(
        rest_only_client, ENTITY, timeout=QUIET_S, poll_interval=POLL_S
    )
    assert result is False


async def test_rest_removed_unreachable_core_raises(
    unreachable_client: HomeAssistantClient,
) -> None:
    """An unreachable Home Assistant is an error, never a confirmed deletion."""
    with pytest.raises(HomeAssistantConnectionError):
        await wait_for_entity_removed(
            unreachable_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )


async def test_rest_state_change_reaches_expected_state(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """A service call's expected state is reported once Core reaches it."""
    hass.states.async_set(ENTITY, "off")
    turn_on = {2: lambda: hass.states.async_set(ENTITY, "on")}
    with answered(rest_only_client, turn_on):
        result = await wait_for_state_change(
            rest_only_client,
            ENTITY,
            expected_state="on",
            timeout=SETTLES_S,
            poll_interval=POLL_S,
        )
    assert result is not None
    assert result["state"] == "on"


async def test_rest_state_change_any_change_is_reported(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """With no expected state, any change from the state before the call is reported."""
    hass.states.async_set(ENTITY, "off")
    turn_on = {2: lambda: hass.states.async_set(ENTITY, "on")}
    with answered(rest_only_client, turn_on):
        result = await wait_for_state_change(
            rest_only_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is not None
    assert result["state"] == "on"


async def test_rest_state_change_unchanged_times_out(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """A state that never reaches the expected value is reported as no change."""
    hass.states.async_set(ENTITY, "off")
    result = await wait_for_state_change(
        rest_only_client,
        ENTITY,
        expected_state="on",
        timeout=QUIET_S,
        poll_interval=POLL_S,
    )
    assert result is None


async def test_rest_state_change_compares_with_given_initial_state(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """The state the caller saw before its call is the baseline: a change that
    already happened is reported, without fetching a new baseline."""
    hass.states.async_set(ENTITY, "on")
    with answered(rest_only_client) as statuses:
        result = await wait_for_state_change(
            rest_only_client,
            ENTITY,
            initial_state="off",
            timeout=SETTLES_S,
            poll_interval=POLL_S,
        )
    assert result is not None
    assert result["state"] == "on"
    assert statuses == [200]


async def test_rest_state_change_expected_state_already_reached(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """An entity already in the expected state is reported at once."""
    hass.states.async_set(ENTITY, "on")
    result = await wait_for_state_change(
        rest_only_client,
        ENTITY,
        expected_state="on",
        timeout=SETTLES_S,
        poll_interval=POLL_S,
    )
    assert result is not None
    assert result["state"] == "on"


async def test_rest_state_change_adopts_first_seen_state_as_baseline(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """When the entity is missing before the call, the first state seen is the
    baseline, not a change: only the change after it is reported."""
    steps = {
        1: lambda: hass.states.async_set(ENTITY, "off"),
        2: lambda: hass.states.async_set(ENTITY, "on"),
    }
    with answered(rest_only_client, steps) as statuses:
        result = await wait_for_state_change(
            rest_only_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is not None
    assert result["state"] == "on"
    assert statuses == [404, 200, 200]


async def test_rest_state_change_survives_a_404_mid_wait(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """An error answer while polling (the entity briefly gone) does not end
    the wait: the later expected state is still reported."""
    hass.states.async_set(ENTITY, "off")
    steps = {
        1: lambda: hass.states.async_remove(ENTITY),
        2: lambda: hass.states.async_set(ENTITY, "on"),
    }
    with answered(rest_only_client, steps) as statuses:
        result = await wait_for_state_change(
            rest_only_client,
            ENTITY,
            expected_state="on",
            timeout=SETTLES_S,
            poll_interval=POLL_S,
        )
    assert result is not None
    assert result["state"] == "on"
    assert statuses == [200, 404, 200]


async def test_rest_state_change_unreachable_on_initial_fetch_raises(
    unreachable_client: HomeAssistantClient,
) -> None:
    """An unreachable Home Assistant while reading the baseline is an error."""
    with pytest.raises(HomeAssistantConnectionError):
        await wait_for_state_change(
            unreachable_client,
            ENTITY,
            expected_state="on",
            timeout=SETTLES_S,
            poll_interval=POLL_S,
        )


async def test_rest_state_change_unreachable_while_polling_raises(
    unreachable_client: HomeAssistantClient,
) -> None:
    """An unreachable Home Assistant while polling is an error, not "no change"."""
    with pytest.raises(HomeAssistantConnectionError):
        await wait_for_state_change(
            unreachable_client,
            ENTITY,
            expected_state="on",
            timeout=SETTLES_S,
            poll_interval=POLL_S,
            initial_state="off",
        )


# --- WebSocket events, sampled over REST ------------------------------------


async def test_ws_registered_existing_entity_resolves_on_first_sample(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An entity already there when the waiter subscribes is confirmed by the
    first sample, and both subscriptions are released."""
    hass.states.async_set(ENTITY, "on")
    with (
        received(hass, "subscribe_events", "unsubscribe_events") as seen,
        answered(core_client) as statuses,
    ):
        assert await wait_for_entity_registered(core_client, ENTITY) is True
    assert statuses == [200]
    subscribed = seen["subscribe_events"]
    assert [m["event_type"] for m in subscribed] == [
        "state_changed",
        "entity_registry_updated",
    ]
    assert sorted(m["subscription"] for m in seen["unsubscribe_events"]) == sorted(
        m["id"] for m in subscribed
    )


@pytest.mark.usefixtures("events_only")
async def test_ws_registered_event_resolves_wait(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The entity's first state event confirms it, with no REST poll between."""
    create = {1: lambda: hass.states.async_set(ENTITY, "on")}
    async with asyncio.timeout(SETTLES_S):
        with answered(core_client, create) as statuses:
            result = await wait_for_entity_registered(
                core_client, ENTITY, timeout=NEVER_S
            )
    assert result is True
    assert statuses == [404, 200]


@pytest.mark.usefixtures("events_only")
async def test_ws_registered_other_entity_event_does_not_wake_wait(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Another entity's state event does not make the waiter sample again."""
    socket = await pooled_socket(core_client)
    noise = {1: lambda: hass.states.async_set("light.other", "on")}
    with delivered(socket) as events, answered(core_client, noise) as statuses:
        result = await wait_for_entity_registered(core_client, ENTITY, timeout=QUIET_S)
    assert "light.other" in events
    assert result is False
    # The first sample and the last one at the timeout; none for the event.
    assert statuses == [404, 404]


async def test_ws_registered_dropped_subscribe_falls_back_to_rest(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A connection that drops while the waiter subscribes (after its first
    subscription landed) still confirms the entity over REST."""
    hass.states.async_set(ENTITY, "on")
    socket = await pooled_socket(core_client)
    with dropped_on_second_subscribe(hass, socket):
        result = await wait_for_entity_registered(
            core_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is True
    assert socket.is_connected is False


@pytest.mark.usefixtures("events_only")
async def test_ws_removed_event_resolves_wait(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The entity's removal event confirms a delete, with no REST poll between."""
    hass.states.async_set(ENTITY, "on")
    remove = {1: lambda: hass.states.async_remove(ENTITY)}
    async with asyncio.timeout(SETTLES_S):
        with answered(core_client, remove) as statuses:
            result = await wait_for_entity_removed(core_client, ENTITY, timeout=NEVER_S)
    assert result is True
    assert statuses == [200, 404]


@pytest.mark.usefixtures("events_only")
async def test_ws_state_change_event_resolves_wait(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A service call's state event reports the new state; the waiter
    subscribes to state changes only and releases that subscription."""
    hass.states.async_set(ENTITY, "off")
    turn_on = {2: lambda: hass.states.async_set(ENTITY, "on")}
    async with asyncio.timeout(SETTLES_S):
        with (
            received(hass, "subscribe_events", "unsubscribe_events") as seen,
            answered(core_client, turn_on),
        ):
            result = await wait_for_state_change(
                core_client, ENTITY, expected_state="on", timeout=NEVER_S
            )
    assert result is not None
    assert result["state"] == "on"
    assert [m["event_type"] for m in seen["subscribe_events"]] == ["state_changed"]
    assert [m["subscription"] for m in seen["unsubscribe_events"]] == [
        seen["subscribe_events"][0]["id"]
    ]


async def test_ws_state_change_timeout_releases_subscription(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A wait that times out still releases its subscription."""
    hass.states.async_set(ENTITY, "off")
    with received(hass, "subscribe_events", "unsubscribe_events") as seen:
        result = await wait_for_state_change(
            core_client, ENTITY, expected_state="on", timeout=QUIET_S
        )
    assert result is None
    assert [m["subscription"] for m in seen["unsubscribe_events"]] == [
        seen["subscribe_events"][0]["id"]
    ]


@pytest.mark.usefixtures("events_only")
async def test_ws_state_change_adopts_first_seen_state_as_baseline(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """When the entity is missing before the call, the first state seen after
    subscribing is the baseline: only the change after it is reported."""
    steps = {
        1: lambda: hass.states.async_set(ENTITY, "off"),
        2: lambda: hass.states.async_set(ENTITY, "on"),
    }
    async with asyncio.timeout(SETTLES_S):
        with answered(core_client, steps) as statuses:
            result = await wait_for_state_change(core_client, ENTITY, timeout=NEVER_S)
    assert result is not None
    assert result["state"] == "on"
    assert statuses == [404, 200, 200]


@pytest.mark.usefixtures("fast_backstop")
async def test_ws_connection_drop_while_waiting_completes_over_rest(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A connection that drops while the waiter waits does not lose the
    entity: the wait completes over REST."""
    socket = await pooled_socket(core_client)

    async def drop_then_create() -> None:
        await socket.disconnect()
        hass.states.async_set(ENTITY, "on")

    with answered(core_client, {2: drop_then_create}) as statuses:
        result = await wait_for_entity_registered(
            core_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is True
    assert statuses[:2] == [404, 404]
    assert statuses[-1] == 200


@pytest.mark.usefixtures("events_only")
async def test_ws_connection_drop_before_waiting_polls_rest_at_once(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A connection that drops right after the first sample switches to REST
    polling at once instead of waiting on a subscription that is gone."""
    socket = await pooled_socket(core_client)

    async def drop_then_create() -> None:
        await socket.disconnect()
        hass.states.async_set(ENTITY, "on")

    with answered(core_client, {1: drop_then_create}) as statuses:
        result = await wait_for_entity_registered(
            core_client, ENTITY, timeout=SETTLES_S, poll_interval=POLL_S
        )
    assert result is True
    assert statuses == [404, 200]


@pytest.mark.usefixtures("fast_backstop")
async def test_ws_backstop_resolves_when_events_stop_arriving(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A subscription that delivers nothing still lets the wait succeed through
    its periodic REST sample."""
    socket = await pooled_socket(core_client)
    with ExitStack() as unread:

        def stop_reading_then_create() -> None:
            unread.enter_context(answers_unread(socket))
            hass.states.async_set(ENTITY, "on")

        steps = {1: stop_reading_then_create, 2: unread.close}
        async with asyncio.timeout(SETTLES_S):
            with answered(core_client, steps) as statuses:
                result = await wait_for_entity_registered(
                    core_client, ENTITY, timeout=NEVER_S
                )
    assert result is True
    assert statuses == [404, 200]


async def test_ws_connection_lost_during_cleanup_keeps_result(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A connection lost while releasing the subscriptions does not turn a
    confirmed entity into an error."""
    hass.states.async_set(ENTITY, "on")
    socket = await pooled_socket(core_client)
    with dropped_on(hass, "unsubscribe_events", socket):
        result = await wait_for_entity_registered(core_client, ENTITY)
    assert result is True
    assert socket.is_connected is False


async def test_ws_unanswered_unsubscribe_keeps_result(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsubscribe Core does not answer in time neither turns a confirmed
    entity into an error nor stops the other subscription's release."""
    hass.states.async_set(ENTITY, "on")
    with received(hass, "unsubscribe_events") as seen:
        async with unanswered(hass, "unsubscribe_events", monkeypatch):
            monkeypatch.setattr(
                websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", POLL_S
            )
            result = await wait_for_entity_registered(core_client, ENTITY)
    assert result is True
    assert len(seen["unsubscribe_events"]) == 2


# --- Automation entity discovery by unique_id -------------------------------


async def test_rest_discovery_finds_published_automation(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """A created automation's entity_id is found from its unique_id."""
    hass.states.async_set("automation.target", "on", {"id": "uid_42"})
    result = await wait_for_automation_entity_by_unique_id(
        rest_only_client, "uid_42", timeout=SETTLES_S
    )
    assert result == "automation.target"


async def test_rest_discovery_skips_other_domains_with_same_id(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient
) -> None:
    """A script carrying the same id is never taken for the automation."""
    hass.states.async_set("script.distractor", "off", {"id": "uid_42"})
    hass.states.async_set("automation.real", "on", {"id": "uid_42"})
    result = await wait_for_automation_entity_by_unique_id(
        rest_only_client, "uid_42", timeout=SETTLES_S
    )
    assert result == "automation.real"


async def test_rest_discovery_unpublished_automation_times_out(
    rest_only_client: HomeAssistantClient,
) -> None:
    """An automation that never appears is reported as not found."""
    result = await wait_for_automation_entity_by_unique_id(
        rest_only_client, "uid_missing", timeout=QUIET_S, poll_interval=POLL_S
    )
    assert result is None


async def test_rest_discovery_failing_rest_names_the_last_error(
    api_less_client: HomeAssistantClient, caplog: pytest.LogCaptureFixture
) -> None:
    """When every sample gets an error answer, the wait keeps polling to its
    timeout and the log names the last error, so "REST is down" is not
    mistaken for "the automation was never published"."""
    with caplog.at_level(logging.WARNING, logger=ws_waiters.__name__):
        result = await wait_for_automation_entity_by_unique_id(
            api_less_client, "uid_wedged", timeout=QUIET_S, poll_interval=POLL_S
        )
    assert result is None
    wedged = [
        r.getMessage()
        for r in caplog.records
        if "every REST sample failing" in r.getMessage()
    ]
    assert len(wedged) == 1
    assert "404" in wedged[0]


async def test_ws_discovery_existing_automation_resolves_on_first_sample(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An automation already published when the waiter subscribes is found by
    the first sample; the waiter subscribes to state changes only."""
    hass.states.async_set("automation.early", "on", {"id": "uid_early"})
    with received(hass, "subscribe_events", "unsubscribe_events") as seen:
        result = await wait_for_automation_entity_by_unique_id(
            core_client, "uid_early", timeout=SETTLES_S
        )
    assert result == "automation.early"
    assert [m["event_type"] for m in seen["subscribe_events"]] == ["state_changed"]
    assert len(seen["unsubscribe_events"]) == 1


@pytest.mark.usefixtures("events_only")
async def test_ws_discovery_event_names_the_automation_without_rescan(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The automation's first state event names its entity_id, with no
    second scan of every state."""
    publish = {
        1: lambda: hass.states.async_set(
            "automation.discovered", "on", {"id": "uid_99"}
        )
    }
    async with asyncio.timeout(SETTLES_S):
        with answered(core_client, publish) as statuses:
            result = await wait_for_automation_entity_by_unique_id(
                core_client, "uid_99", timeout=NEVER_S
            )
    assert result == "automation.discovered"
    assert statuses == [200]


@pytest.mark.usefixtures("events_only")
@pytest.mark.parametrize(
    ("entity_id", "unique_id"),
    [("light.kitchen", "uid_42"), ("automation.other", "uid_other")],
    ids=["other-domain", "other-automation"],
)
async def test_ws_discovery_unrelated_event_does_not_wake_wait(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_id: str,
    unique_id: str,
) -> None:
    """Another domain's entity carrying the id, or another automation, does
    not wake the waiter, so concurrent creates cannot take each other's ids."""
    socket = await pooled_socket(core_client)
    noise = {1: lambda: hass.states.async_set(entity_id, "on", {"id": unique_id})}
    with delivered(socket) as events, answered(core_client, noise) as statuses:
        result = await wait_for_automation_entity_by_unique_id(
            core_client, "uid_42", timeout=QUIET_S
        )
    assert entity_id in events
    assert result is None
    # The first sample and the last one at the timeout; none for the event.
    assert statuses == [200, 200]
