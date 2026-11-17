"""Unit tests for the WebSocket listener service.

The listener subscribes to Home Assistant's ``state_changed`` events and marks
pending device operations COMPLETED (or FAILED) from them. HA nests the
``entity_id`` / ``new_state`` / ``old_state`` fields inside ``event["data"]``;
the listener must read them from there so async device operations finish
instead of expiring. Every event here is fired by Core on a real ``hass`` and
reaches the listener over the real WebSocket.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from itertools import count
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_listener import WebSocketListenerService
from ha_mcp.config import reset_global_settings
from ha_mcp.utils.operation_manager import (
    OperationStatus,
    get_operation_from_memory,
    store_pending_operation,
    update_pending_operations,
)

from ._ws_transport import until


@pytest.fixture
async def listener_service(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> AsyncIterator[WebSocketListenerService]:
    """A started listener whose server settings point at the real ``hass``."""
    monkeypatch.setenv("HOMEASSISTANT_URL", core_client.base_url)
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", core_client.token)
    reset_global_settings()
    service = WebSocketListenerService()
    assert await service.start() is True
    try:
        yield service
    finally:
        await service.stop()


@pytest.fixture
def dispatched(listener_service: WebSocketListenerService) -> Iterator[MagicMock]:
    """A spy on the listener's hand-off to the operation manager: it calls
    through, so the operations still change."""
    with patch(
        "ha_mcp.client.websocket_listener.update_pending_operations",
        wraps=update_pending_operations,
    ) as spy:
        yield spy


SENTINELS = count()


def pending_turn_on(entity_id: str) -> str:
    """Register a pending ``turn_on`` operation that waits for state ``on``."""
    return store_pending_operation(
        entity_id,
        "turn_on",
        "light",
        "turn_on",
        {"entity_id": entity_id},
        expected_state={"state": "on"},
    )


def status_of(operation_id: str) -> OperationStatus:
    operation = get_operation_from_memory(operation_id)
    assert operation is not None
    return operation.status


def dispatched_for_others(dispatched: MagicMock) -> list[str]:
    """Entities handed to the operation manager, apart from the sentinels."""
    entities = [call.args[0] for call in dispatched.call_args_list]
    return [e for e in entities if not e.startswith("light.sentinel_")]


async def event_queue_drained(hass: HomeAssistant) -> None:
    """Return once the listener has handled every event Core fired before.

    A sentinel operation completes only when its own ``state_changed`` is
    handled, and events reach the listener in the order Core fired them.
    """
    sentinel_id = f"light.sentinel_{next(SENTINELS)}"
    sentinel = pending_turn_on(sentinel_id)
    hass.states.async_set(sentinel_id, "on")
    await until(lambda: status_of(sentinel) is OperationStatus.COMPLETED)


async def test_state_change_completes_the_pending_operation(
    hass: HomeAssistant, listener_service: WebSocketListenerService
) -> None:
    """HA's state_changed event nests entity_id / new_state inside ``data``.

    When the handler read those keys from the top level it got ``None`` for
    every event, returned early, and operations stayed PENDING until they
    expired. A real state change must complete the operation with the new
    state.
    """
    hass.states.async_set("light.bedroom", "off")
    operation_id = pending_turn_on("light.bedroom")

    hass.states.async_set("light.bedroom", "on", {"brightness": 200})
    await until(lambda: status_of(operation_id) is OperationStatus.COMPLETED)

    operation = get_operation_from_memory(operation_id)
    assert operation is not None
    assert operation.result_state is not None
    assert operation.result_state["state"] == "on"
    assert operation.result_state["attributes"] == {"brightness": 200}


async def test_state_change_to_another_state_leaves_the_operation_pending(
    hass: HomeAssistant, listener_service: WebSocketListenerService
) -> None:
    """Only the state the operation waits for completes it."""
    operation_id = pending_turn_on("light.hall")

    hass.states.async_set("light.hall", "off")
    await event_queue_drained(hass)

    assert status_of(operation_id) is OperationStatus.PENDING


async def test_event_without_data_is_dropped(
    hass: HomeAssistant,
    listener_service: WebSocketListenerService,
    dispatched: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A ``state_changed`` event with no ``data`` (malformed) is dropped: the
    guard at the top of the handler returns before dispatching."""
    hass.bus.async_fire("state_changed")
    await event_queue_drained(hass)

    assert dispatched_for_others(dispatched) == []
    assert "Error in event handler" not in caplog.text


async def test_event_without_entity_id_is_dropped(
    hass: HomeAssistant,
    listener_service: WebSocketListenerService,
    dispatched: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``data`` present but without an entity_id: the handler returns without
    dispatching (defensive guard kept from the original handler)."""
    hass.bus.async_fire(
        "state_changed", {"new_state": {"state": "on"}, "old_state": None}
    )
    await event_queue_drained(hass)

    assert dispatched_for_others(dispatched) == []
    assert "Error in event handler" not in caplog.text


async def test_removing_an_entity_does_not_complete_its_operation(
    hass: HomeAssistant,
    listener_service: WebSocketListenerService,
    dispatched: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA fires state_changed with ``new_state=None`` on entity removal.
    Operation matching is only meaningful for a transition to a real new
    state, so the handler returns without dispatching."""
    hass.states.async_set("light.removed_bulb", "off")
    await event_queue_drained(hass)
    dispatched.reset_mock()
    operation_id = store_pending_operation(
        "light.removed_bulb", "turn_off", "light", "turn_off", {}
    )

    hass.states.async_remove("light.removed_bulb")
    await event_queue_drained(hass)

    assert dispatched_for_others(dispatched) == []
    assert "Error in event handler" not in caplog.text
    assert status_of(operation_id) is OperationStatus.PENDING
