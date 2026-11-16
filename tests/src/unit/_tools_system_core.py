"""The health connection of ``ha_get_system_health``, watched from Core's side.

Each helper re-registers a command's own handler with its own schema: to hold
it until Core received another command, or to drop the connection once Core
has it. Core's handler answers every message it is handed; nothing here
answers for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from ha_mcp.client import websocket_client
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import tools_system

from ._ws_transport import abort_transport


@asynccontextmanager
async def answered_after(
    hass: HomeAssistant, command: str, first: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Core answers ``command`` only once it has received ``first``.

    A client that waits for ``command`` before it sends ``first`` gets no
    answer: its wait is cut to one second meanwhile.
    """
    held_handler, held_schema = hass.data[websocket_api.DOMAIN][command]
    first_handler, first_schema = hass.data[websocket_api.DOMAIN][first]
    arrived = asyncio.Event()

    def note(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        arrived.set()
        first_handler(core, connection, msg)

    async def wait(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        await arrived.wait()
        held_handler(core, connection, msg)

    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", 1.0)
    websocket_api.async_register_command(hass, first, note, first_schema)
    websocket_api.async_register_command(
        hass, command, websocket_api.async_response(wait), held_schema
    )
    try:
        yield
    finally:
        arrived.set()
        websocket_api.async_register_command(hass, first, first_handler, first_schema)
        websocket_api.async_register_command(hass, command, held_handler, held_schema)
        await hass.async_block_till_done(wait_background_tasks=True)


@contextmanager
def health_socket_dropped_on(
    hass: HomeAssistant, command: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """The connection ``ha_get_system_health`` opens for its sections drops,
    with no close frame, as soon as Core receives ``command`` on it."""
    opened: list[HomeAssistantWebSocketClient] = []
    connect = tools_system.get_connected_ws_client

    async def recording(
        *args: Any, **kwargs: Any
    ) -> tuple[HomeAssistantWebSocketClient | None, dict[str, Any] | None]:
        socket, error = await connect(*args, **kwargs)
        if socket is not None:
            opened.append(socket)
        return socket, error

    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def drop(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        abort_transport(opened[-1])

    monkeypatch.setattr(tools_system, "get_connected_ws_client", recording)
    websocket_api.async_register_command(hass, command, drop, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)
