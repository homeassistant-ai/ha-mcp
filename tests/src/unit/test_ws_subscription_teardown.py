"""A closing WebSocket keeps the subscription events that arrived before it."""

import asyncio

import pytest

from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient


@pytest.mark.asyncio
async def test_events_queued_before_the_close_stay_readable() -> None:
    client = HomeAssistantWebSocketClient(
        url="http://homeassistant.local:8123", token="test-token"
    )
    queue = client._state.register_subscription_queue(7)
    event = {"type": "event", "event": {"manager_state": "restore_backup"}}
    queue.put_nowait(event)

    client._state.reset_connection()

    assert queue.get_nowait() == event
    with pytest.raises(asyncio.QueueShutDown):
        queue.get_nowait()
    assert client._state.get_subscription_queue(7) is None
