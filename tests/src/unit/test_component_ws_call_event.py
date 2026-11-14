"""The component's ``ha_mcp_tools/call_event`` command on a real ``hass``.

The command fires the event on Core's own bus and returns the event's context
plus the listener counts read in the same synchronous pass, so the server's
``ha_call_event`` needs no separate ``GET /api/events`` before a fire. The
listeners are Core's, registered with ``hass.bus.async_listen``.
"""

from __future__ import annotations

from homeassistant.const import MATCH_ALL
from homeassistant.core import Event, HomeAssistant, callback
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_result


def _wildcards(hass: HomeAssistant) -> int:
    return hass.bus.async_listeners().get(MATCH_ALL, 0)


async def test_a_fire_returns_the_events_context_and_its_audience(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    fired: list[Event] = []

    @callback
    def record(event: Event) -> None:
        fired.append(event)

    hass.bus.async_listen("my_event", record)
    hass.bus.async_listen("my_event", callback(lambda event: None))
    hass.bus.async_listen(MATCH_ALL, callback(lambda event: None))
    wildcards = _wildcards(hass)

    result = await ws_result(
        component_ws,
        "ha_mcp_tools/call_event",
        event_type="my_event",
        event_data={"key": "value"},
    )
    await hass.async_block_till_done()

    (event,) = fired
    assert event.data == {"key": "value"}
    assert result["event_type"] == "my_event"
    assert result["context"]["id"] == event.context.id
    assert (result["listener_count"], result["wildcard_listener_count"]) == (
        2,
        wildcards,
    )


async def test_a_listener_that_removes_itself_on_the_fire_still_counts(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The counts describe the audience the event reached."""
    heard: list[Event] = []

    @callback
    def hear(event: Event) -> None:
        heard.append(event)

    hass.bus.async_listen_once("my_event", hear)
    hass.bus.async_listen("my_event", callback(lambda event: None))

    result = await ws_result(
        component_ws, "ha_mcp_tools/call_event", event_type="my_event"
    )
    await hass.async_block_till_done()

    assert len(heard) == 1
    assert result["listener_count"] == 2


async def test_an_event_nobody_listens_to_has_no_audience(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    result = await ws_result(
        component_ws, "ha_mcp_tools/call_event", event_type="new_event"
    )

    assert result["listener_count"] == 0
    assert result["wildcard_listener_count"] == _wildcards(hass)
