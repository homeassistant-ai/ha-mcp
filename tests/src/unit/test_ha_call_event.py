"""ha_call_event on a real Home Assistant Core.

An event fires through the component's ``ha_mcp_tools/call_event`` when it is
installed, otherwise through Core's own WebSocket ``fire_event``; REST's
``POST /api/events`` serves ``state_changed`` and an install whose WebSocket
API is unreachable. Every reply below is Core's: events land on the real bus,
and listener counts come from listeners registered on it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from homeassistant.components import websocket_api
from homeassistant.const import EVENT_STATE_CHANGED, MATCH_ALL
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import async_capture_events

from custom_components.ha_mcp_tools.websocket_api import async_register_commands
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import event_call
from ha_mcp.tools.component_api import component_probe_auth_kind
from ha_mcp.tools.tools_service import ServiceTools

from ._client_spies import sent_messages
from ._command_faults import dropped_on, pooled_socket
from ._receipt_faults import revoke
from ._tool_errors import error_of

# Core refuses a longer event type (its MAX_LENGTH_EVENT_EVENT_TYPE).
TOO_LONG_EVENT = "e" * 65


def _wildcards(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(MATCH_ALL, 0)


def _listen(hass: HomeAssistant, event_type: str, times: int) -> None:
    for _ in range(times):
        hass.bus.async_listen(event_type, callback(lambda event: None))


async def _closed(socket: HomeAssistantWebSocketClient) -> None:
    """Return once the client has seen Core close ``socket``."""
    for _ in range(10_000):
        if not socket.is_connected:
            return
        await asyncio.sleep(0)
    raise AssertionError("the socket is still open")


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(core_client, None)


@pytest.fixture
def rest_fires(core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on the client's REST fire: it calls through to Core."""
    with patch.object(core_client, "fire_event", wraps=core_client.fire_event) as spy:
        yield spy


@pytest.fixture
def lookups(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """A spy on the ``GET /api/events`` listener lookup: Core still answers."""
    spy = AsyncMock(wraps=event_call.get_event_listeners)
    monkeypatch.setattr(event_call, "get_event_listeners", spy)
    return spy


@pytest.fixture
async def rest_only_client(
    hass: HomeAssistant,
    aiohttp_client: Any,
    hass_access_token: str,
    socket_enabled: None,
) -> AsyncIterator[HomeAssistantClient]:
    """The server's client on a Core that serves REST but no WebSocket API,
    as behind a proxy that does not pass WebSocket upgrades."""
    assert await async_setup_component(hass, "api", {})
    server = await aiohttp_client(hass.http.app)
    client = HomeAssistantClient(
        base_url=str(server.make_url("")).rstrip("/"),
        token=hass_access_token,
        verify_ssl=False,
    )
    try:
        yield client
    finally:
        try:
            await websocket_manager.disconnect()
        finally:
            await client.close()


@pytest.fixture
async def silent_client(
    hass_access_token: str, socket_enabled: None, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[HomeAssistantClient]:
    """A client whose Home Assistant takes each request and never answers;
    the client stops waiting for an answer after 10 ms."""

    async def take(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read()
        writer.close()

    server = await asyncio.start_server(take, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = HomeAssistantClient(
        base_url=f"http://127.0.0.1:{port}",
        token=hass_access_token,
        verify_ssl=False,
        # Known already, so the admin-only POST does not first ask over a
        # WebSocket that would never open.
        is_admin=True,
    )
    monkeypatch.setattr(client.httpx_client, "timeout", httpx.Timeout(5, read=0.01))
    try:
        yield client
    finally:
        await client.close()
        server.close()
        await server.wait_closed()


# --- Firing ---------------------------------------------------------------


@pytest.mark.parametrize("component", [False, True], ids=["core", "component"])
@pytest.mark.parametrize(
    ("data", "delivered"),
    [
        (None, {}),
        ({"key": "value"}, {"key": "value"}),
        ('{"temperature": 22}', {"temperature": 22}),
    ],
    ids=["no_data", "dict", "json_string"],
)
async def test_an_event_reaches_the_bus_with_its_payload_and_context(
    hass: HomeAssistant,
    tools: ServiceTools,
    rest_fires: AsyncMock,
    component: bool,
    data: Any,
    delivered: dict[str, Any],
) -> None:
    """Core types the payload as a dict and refuses an explicit null, so a
    call with no data must still fire; the context lets a caller find the
    automation run the event triggered."""
    if component:
        async_register_commands(hass)
    fired = async_capture_events(hass, "my_event")

    result = await tools.ha_call_event("my_event", data)
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data == delivered
    assert result["success"] is True
    assert result["event_type"] == "my_event"
    assert result["context_id"] == event.context.id
    assert result["context"]["id"] == event.context.id
    rest_fires.assert_not_awaited()


async def test_the_component_reports_counts_without_a_separate_lookup(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    lookups: AsyncMock,
) -> None:
    """The component reads the audience in the same pass as the fire."""
    _listen(hass, "my_event", 2)

    result = await ServiceTools(component_core_client, None).ha_call_event("my_event")

    assert (result["listener_count"], result["wildcard_listener_count"]) == (
        2,
        _wildcards(hass),
    )
    assert "warnings" not in result
    lookups.assert_not_awaited()


async def test_without_the_component_counts_come_from_the_listener_lookup(
    hass: HomeAssistant, tools: ServiceTools, lookups: AsyncMock
) -> None:
    _listen(hass, "my_event", 2)

    result = await tools.ha_call_event("my_event")

    assert (result["listener_count"], result["wildcard_listener_count"]) == (
        2,
        _wildcards(hass),
    )
    assert "warnings" not in result
    lookups.assert_awaited_once()


async def test_an_event_nobody_listens_to_warns_without_claiming_delivery(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """The count is read before the fire: it says who was registered, not
    who received the event."""
    assert _wildcards(hass) == 0

    result = await tools.ha_call_event("my_event")

    assert (result["listener_count"], result["wildcard_listener_count"]) == (0, 0)
    warning = " ".join(result["warnings"]).lower()
    assert "no event listeners" in warning
    assert "received" not in warning


async def test_match_all_listeners_do_not_hide_a_missing_audience(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """The recorder listens to every event and only stores it."""
    _listen(hass, MATCH_ALL, 3)

    result = await tools.ha_call_event("my_event")

    assert (result["listener_count"], result["wildcard_listener_count"]) == (0, 3)
    warning = " ".join(result["warnings"]).lower()
    assert "no event listeners" in warning
    assert "match-all" in warning
    assert "recorder" in warning


async def test_an_event_type_with_url_delimiters_fires_as_written(
    hass: HomeAssistant, tools: ServiceTools
) -> None:
    """The WebSocket route carries the event type in JSON, not a URL path."""
    fired = async_capture_events(hass, "mcp_probe?key=go")

    result = await tools.ha_call_event("mcp_probe?key=go")
    await hass.async_block_till_done()

    (event,) = fired
    assert result["context_id"] == event.context.id


async def test_state_changed_keeps_the_rest_route_that_rebuilds_its_states(
    hass: HomeAssistant,
    tools: ServiceTools,
    rest_fires: AsyncMock,
    sent: AsyncMock,
) -> None:
    """Only REST turns the old_state/new_state dicts into State objects, which
    every state_changed listener expects."""
    old = State("light.x", "off").as_dict()
    new = State("light.x", "on").as_dict()
    fired = async_capture_events(hass, EVENT_STATE_CHANGED)

    result = await tools.ha_call_event(
        EVENT_STATE_CHANGED,
        {"entity_id": "light.x", "old_state": old, "new_state": new},
    )
    await hass.async_block_till_done()

    (event,) = [e for e in fired if e.data.get("entity_id") == "light.x"]
    assert isinstance(event.data["new_state"], State)
    assert event.data["new_state"].state == "on"
    assert result["success"] is True
    rest_fires.assert_awaited_once()
    sent.assert_not_awaited()


# --- Refusals -------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "data"),
    [("   ", None), ("my_event", "[1, 2, 3]"), ("my_event", "{not valid json")],
    ids=["blank_event_type", "list_data", "invalid_json"],
)
async def test_an_invalid_call_is_refused_before_anything_fires(
    hass: HomeAssistant, tools: ServiceTools, event_type: str, data: str | None
) -> None:
    fired = async_capture_events(hass, "my_event")

    with pytest.raises(ToolError):
        await tools.ha_call_event(event_type, data)
    await hass.async_block_till_done()

    assert fired == []


@pytest.mark.parametrize("component", [False, True], ids=["core", "component"])
async def test_cores_rejection_comes_back_with_event_advice(
    hass: HomeAssistant, tools: ServiceTools, component: bool
) -> None:
    """Core's 64-character limit is the error, with a hint at the cause."""
    if component:
        async_register_commands(hass)

    with pytest.raises(ToolError) as exc_info:
        await tools.ha_call_event(TOO_LONG_EVENT)

    assert any("64 characters" in s for s in error_of(exc_info)["suggestions"])


async def test_a_non_admin_gets_the_admin_required_error(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """Core's fire_event is admin-only; its refusal must not read as a generic
    failure."""
    fired = async_capture_events(hass, "my_event")

    with pytest.raises(ToolError) as exc_info:
        await ServiceTools(read_only_client, None).ha_call_event("my_event")

    assert error_of(exc_info)["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    assert fired == []


async def test_a_non_admins_refused_probe_stops_before_the_fire(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    read_only_client: HomeAssistantClient,
    lookups: AsyncMock,
) -> None:
    """The component refuses its capability probe to a non-admin: nothing
    else is sent with that token."""
    fired = async_capture_events(hass, "my_event")

    with (
        sent_messages(read_only_client) as sent,
        pytest.raises(ToolError) as exc_info,
    ):
        await ServiceTools(read_only_client, None).ha_call_event("my_event")

    assert error_of(exc_info)["code"] == "AUTH_INSUFFICIENT_PERMISSIONS"
    assert fired == []
    sent.assert_not_awaited()
    lookups.assert_not_awaited()


# Core closes a socket whose token it refused, leaving its heartbeat timer.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_a_rejected_token_is_named_and_nothing_else_is_sent(
    hass: HomeAssistant, core_client: HomeAssistantClient, lookups: AsyncMock
) -> None:
    """A token Core refuses is a token problem, not a permission one; every
    further request with it adds a 401 toward Home Assistant's ban."""
    client = HomeAssistantClient(
        base_url=core_client.base_url, token="not-a-token", verify_ssl=False
    )
    try:
        with sent_messages(client) as sent, pytest.raises(ToolError) as exc_info:
            await ServiceTools(client, None).ha_call_event("my_event")

        error = error_of(exc_info)
        assert error["code"] != "AUTH_INSUFFICIENT_PERMISSIONS"
        assert "Check the Home Assistant token" in error["suggestions"]
        assert component_probe_auth_kind(client) == "token"
        sent.assert_not_awaited()
        lookups.assert_not_awaited()
    finally:
        await client.close()


# Revoking the token closes the server socket, leaving its heartbeat timer.
@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.parametrize("component", [False, True], ids=["core", "component"])
async def test_a_token_revoked_mid_session_is_not_retried_over_rest(
    hass: HomeAssistant,
    tools: ServiceTools,
    core_client: HomeAssistantClient,
    rest_fires: AsyncMock,
    component: bool,
) -> None:
    """Re-sending a rejected token adds a 401 toward Home Assistant's ban."""
    if component:
        async_register_commands(hass)
    await tools.ha_call_event("warm_up")
    socket = await pooled_socket(core_client)
    revoke(hass, core_client.token)
    await _closed(socket)
    fired = async_capture_events(hass, "my_event")

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await tools.ha_call_event("my_event")

    assert "Check the Home Assistant token" in error_of(exc_info)["suggestions"]
    assert fired == []
    assert sent.await_count == 1
    rest_fires.assert_not_awaited()


# --- No WebSocket: the REST fallback --------------------------------------


async def test_without_a_websocket_the_event_fires_over_rest(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient, lookups: AsyncMock
) -> None:
    """The fallback says why the context is missing, and reuses the counts
    read before the WebSocket attempt instead of waiting for them twice."""
    fired = async_capture_events(hass, "my_event")
    _listen(hass, "my_event", 2)
    audience = hass.bus.async_listeners()["my_event"]

    result = await ServiceTools(rest_only_client, None).ha_call_event(
        "my_event", {"key": "value"}
    )
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data == {"key": "value"}
    assert result["success"] is True
    assert (result["context"], result["context_id"]) == ({}, None)
    assert result["listener_count"] == audience
    assert any("WebSocket fire_event was unavailable" in w for w in result["warnings"])
    lookups.assert_awaited_once()


@pytest.mark.parametrize(
    "event_type", ["bad/event", "bad?event", "bad#event", "bad%3F"]
)
async def test_the_rest_fallback_refuses_url_delimiters_and_says_why(
    hass: HomeAssistant, rest_only_client: HomeAssistantClient, event_type: str
) -> None:
    """In a URL path the type would be truncated or decoded; the refusal names
    the fallback and the WebSocket failure, not the event type alone."""
    fired = async_capture_events(hass, event_type)

    with pytest.raises(ToolError) as exc_info:
        await ServiceTools(rest_only_client, None).ha_call_event(event_type)

    message = str(exc_info.value)
    assert "REST fallback" in message
    assert "WebSocket fire_event command was not sent" in message
    assert "cannot contain path separators" in message
    assert fired == []


# --- No answer --------------------------------------------------------------


async def test_a_listener_lookup_past_its_budget_does_not_hold_the_fire(
    hass: HomeAssistant,
    tools: ServiceTools,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The counts are optional: the fire goes out without them, and an
    unknown count is omitted rather than reported as zero."""
    monkeypatch.setattr(event_call, "EVENT_LISTENER_LOOKUP_BUDGET_SECONDS", 0)
    fired = async_capture_events(hass, "my_event")

    with caplog.at_level(logging.DEBUG, logger=event_call.__name__):
        result = await tools.ha_call_event("my_event")
    await hass.async_block_till_done()

    (event,) = fired
    assert result["context_id"] == event.context.id
    assert "listener_count" not in result
    assert "wildcard_listener_count" not in result
    assert "listener lookup failed" in caplog.text.lower()


@pytest.mark.parametrize("component", [False, True], ids=["core", "component"])
async def test_a_connection_lost_after_the_send_is_partial_and_not_retried(
    hass: HomeAssistant,
    tools: ServiceTools,
    core_client: HomeAssistantClient,
    rest_fires: AsyncMock,
    component: bool,
) -> None:
    """Core may have fired the event: a retry could fire it twice, and the
    counts read before the send still hold."""
    if component:
        async_register_commands(hass)
    command = event_call.WS_CALL_EVENT if component else event_call.CORE_FIRE_EVENT
    _listen(hass, "my_event", 2)
    socket = await pooled_socket(core_client)

    with sent_messages(core_client) as sent, dropped_on(hass, command, socket):
        result = await tools.ha_call_event("my_event")

    assert result["success"] is True
    assert result["partial"] is True
    assert any("may have been dispatched" in w for w in result["warnings"])
    assert (result["context"], result["context_id"]) == ({}, None)
    if not component:
        assert result["listener_count"] == 2
    assert sent.await_count == 1
    rest_fires.assert_not_awaited()


async def test_an_unanswered_rest_fire_is_partial_not_an_error(
    hass: HomeAssistant,
    silent_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The POST went out; Home Assistant may have fired the event."""
    monkeypatch.setattr(event_call, "EVENT_LISTENER_LOOKUP_BUDGET_SECONDS", 0)

    result = await ServiceTools(silent_client, None).ha_call_event(EVENT_STATE_CHANGED)

    assert result["success"] is True
    assert result["partial"] is True
    assert "warning" not in result
    assert any("did not confirm" in w.lower() for w in result["warnings"])


async def test_a_refused_connection_is_an_error_not_a_partial_success(
    unreachable_client: HomeAssistantClient,
) -> None:
    """A connect failure happens before the request: nothing was dispatched."""
    with pytest.raises(ToolError) as exc_info:
        await ServiceTools(unreachable_client, None).ha_call_event("my_event")

    assert "Check Home Assistant connection" in error_of(exc_info)["suggestions"]


async def test_a_component_that_lost_call_event_falls_back_to_cores_fire(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A component downgraded under a running server answers unknown_command:
    the event goes through Core's own fire_event instead."""
    tools = ServiceTools(component_core_client, None)
    await tools.ha_call_event("warm_up")
    hass.data[websocket_api.DOMAIN].pop(event_call.WS_CALL_EVENT)
    fired = async_capture_events(hass, "my_event")

    with sent_messages(component_core_client) as sent:
        result = await tools.ha_call_event("my_event")
    await hass.async_block_till_done()

    (event,) = fired
    assert result["context_id"] == event.context.id
    assert [call.args[0]["type"] for call in sent.await_args_list] == [
        event_call.WS_CALL_EVENT,
        event_call.CORE_FIRE_EVENT,
    ]
