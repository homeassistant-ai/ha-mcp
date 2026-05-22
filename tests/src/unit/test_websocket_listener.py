"""Unit tests for the WebSocket listener service.

Pins the HA-wire-contract semantics for ``_handle_state_change``: HA's
``state_changed`` event nests the ``entity_id`` / ``new_state`` /
``old_state`` fields inside ``event["data"]``, not at the top level.
The handler must read from the nested location so that
``OperationManager.process_state_change`` actually receives the event
and async device operations get marked COMPLETED instead of expiring.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest


def _make_state_changed_event(
    entity_id: str = "light.test_bulb",
    new_state_value: str = "on",
    old_state_value: str = "off",
) -> dict:
    """Build a state_changed event shaped exactly as HA's WS API emits it.

    Verified against HA Core ``homeassistant/core.py`` Event.as_dict()
    and ``websocket_api/messages.py`` event_message() — entity_id and
    state objects live inside ``data``, not at the top level.
    """
    return {
        "event_type": "state_changed",
        "data": {
            "entity_id": entity_id,
            "new_state": {
                "entity_id": entity_id,
                "state": new_state_value,
                "attributes": {"brightness": 200},
                "last_changed": "2026-05-19T15:00:00+00:00",
                "last_updated": "2026-05-19T15:00:00+00:00",
                "context": {"id": "abc", "parent_id": None, "user_id": None},
            },
            "old_state": {
                "entity_id": entity_id,
                "state": old_state_value,
                "attributes": {},
                "last_changed": "2026-05-19T14:00:00+00:00",
                "last_updated": "2026-05-19T14:00:00+00:00",
                "context": {"id": "old", "parent_id": None, "user_id": None},
            },
        },
        "time_fired": "2026-05-19T15:00:00+00:00",
        "origin": "LOCAL",
        "context": {"id": "abc", "parent_id": None, "user_id": None},
    }


@pytest.fixture
def listener_service():
    """Construct a fresh listener with a real ``stats`` dict so the
    handler's mutations don't crash, but no real WS connection.
    """
    from ha_mcp.client.websocket_listener import WebSocketListenerService

    service = WebSocketListenerService()
    # Real-ish stats dict so the int-isinstance checks pass.
    service.stats = {
        "events_processed": 0,
        "operations_updated": 0,
        "connection_errors": 0,
        "last_event_time": None,
        "start_time": datetime.now(),
    }
    return service


@pytest.mark.asyncio
async def test_state_change_handler_reads_from_event_data(listener_service):
    """HA's state_changed event nests entity_id / new_state inside ``data``.

    Before the fix, ``_handle_state_change`` read those keys from the
    top level (``event.get("entity_id")``), got ``None`` for every
    event, and the early-return guard fired without ever calling
    ``update_pending_operations``. Operations stayed in PENDING until
    expiration. This test asserts the handler now reaches
    ``update_pending_operations`` with the unwrapped values.
    """
    event = _make_state_changed_event(entity_id="light.bedroom")

    with patch(
        "ha_mcp.client.websocket_listener.update_pending_operations"
    ) as mock_update:
        mock_update.return_value = ["op-xyz"]
        await listener_service._handle_state_change(event)

    mock_update.assert_called_once()
    call_args = mock_update.call_args
    assert call_args[0][0] == "light.bedroom", (
        f"entity_id should be unwrapped from event['data']['entity_id'], "
        f"got {call_args[0][0]!r}"
    )
    new_state = call_args[0][1]
    assert isinstance(new_state, dict), (
        f"new_state should be the dict from event['data']['new_state'], "
        f"got {type(new_state).__name__}"
    )
    assert new_state.get("state") == "on"


@pytest.mark.asyncio
async def test_state_change_handler_ignores_event_missing_data(listener_service):
    """Event with no ``data`` key (malformed or non-state_changed) is
    silently dropped — the guard at the top of the handler returns
    early when entity_id can't be resolved.
    """
    event_without_data = {"event_type": "state_changed", "time_fired": "..."}

    with patch(
        "ha_mcp.client.websocket_listener.update_pending_operations"
    ) as mock_update:
        await listener_service._handle_state_change(event_without_data)

    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_state_change_handler_ignores_event_missing_entity_id(listener_service):
    """``event["data"]`` present but missing entity_id → handler returns
    without dispatching. Defensive guard kept from the original handler.
    """
    event = {
        "event_type": "state_changed",
        "data": {"new_state": {"state": "on"}, "old_state": None},
    }

    with patch(
        "ha_mcp.client.websocket_listener.update_pending_operations"
    ) as mock_update:
        await listener_service._handle_state_change(event)

    mock_update.assert_not_called()


@pytest.mark.asyncio
async def test_state_change_handler_ignores_event_missing_new_state(listener_service):
    """``event["data"]`` present with entity_id but missing new_state →
    handler returns without dispatching.

    HA fires state_changed with ``new_state=None`` on entity removal —
    operation matching is only meaningful for state transitions to a
    real new state. Symmetric mirror of the missing-entity_id case.
    """
    event = {
        "event_type": "state_changed",
        "data": {
            "entity_id": "light.removed_bulb",
            "new_state": None,
            "old_state": {"state": "off"},
        },
    }

    with patch(
        "ha_mcp.client.websocket_listener.update_pending_operations"
    ) as mock_update:
        await listener_service._handle_state_change(event)

    mock_update.assert_not_called()


