"""Core's real ``zwave_js`` integration, set up as Core's own tests set it up.

Only the connection to the Z-Wave JS server
(``homeassistant.components.zwave_js.ZwaveClient``) is replaced: a mock whose
driver, controller and node are zwave-js-server-python's real models, built
from the node state the server would send.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from zwave_js_server.const import NodeStatus, SecurityClass
from zwave_js_server.model.driver import Driver

HOME_ID = 3232323232
NODE_ID = 5


def node_state(values: list[dict[str, Any]]) -> dict[str, Any]:
    """The node as the server reports it: alive, routing, S2-authenticated."""
    endpoints = sorted({0, *(value.get("endpoint", 0) for value in values)})
    return {
        "nodeId": NODE_ID,
        "index": 0,
        "status": NodeStatus.ALIVE,
        "ready": True,
        "isControllerNode": False,
        "isRouting": True,
        "isSecure": True,
        "highestSecurityClass": SecurityClass.S2_AUTHENTICATED,
        "zwavePlusVersion": 2,
        "values": values,
        "endpoints": [
            {"nodeId": NODE_ID, "index": index, "commandClasses": []}
            for index in endpoints
        ],
    }


@contextmanager
def zwave_js_server_connection(node: dict[str, Any]) -> Iterator[MagicMock]:
    """The Z-Wave JS server connection, as Core's ``client`` fixture mocks it.

    Its driver is zwave-js-server-python's real ``Driver``, holding ``node``.
    """
    stop_listening = asyncio.Event()
    with patch(
        "homeassistant.components.zwave_js.ZwaveClient", autospec=True
    ) as client_class:
        client = client_class.return_value

        async def connect() -> None:
            stop_listening.clear()
            client.connected = True

        async def listen(driver_ready: asyncio.Event) -> None:
            driver_ready.set()
            await stop_listening.wait()

        async def disconnect() -> None:
            stop_listening.set()
            client.connected = False

        client.connect = AsyncMock(side_effect=connect)
        client.listen = AsyncMock(side_effect=listen)
        client.disconnect = AsyncMock(side_effect=disconnect)
        client.driver = Driver(
            client,
            {"controller": {"homeId": HOME_ID, "ownNodeId": 1}, "nodes": [node]},
            {"enabled": True, "level": "info", "logToFile": False},
        )
        # The server's reply to each setup command: nothing provisioned, no
        # device config changed.
        client.async_send_command.return_value = {}
        yield client


async def set_up_zwave_js(hass: HomeAssistant) -> MockConfigEntry:
    """Core's ``zwave_js`` integration, connected to the patched server."""
    entry = MockConfigEntry(
        domain="zwave_js", data={"url": "ws://test.org"}, unique_id=str(HOME_ID)
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
