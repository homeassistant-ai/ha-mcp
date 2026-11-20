"""A closing WebSocket keeps the subscription events that arrived before it."""

import asyncio

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient

from ._ws_transport import abort_transport, connected_ws, until


async def test_events_queued_before_the_close_stay_readable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    async with connected_ws(core_client) as ws:
        _, queue = await ws.subscribe_command(
            "subscribe_events", event_type="teardown_probe"
        )
        hass.bus.async_fire("teardown_probe", {"manager_state": "restore_backup"})
        await until(lambda: queue.qsize() == 1)

        abort_transport(ws)
        await until(lambda: not ws.is_connected)

        event = queue.get_nowait()
        assert event["event"]["data"] == {"manager_state": "restore_backup"}
        with pytest.raises(asyncio.QueueShutDown):
            queue.get_nowait()
