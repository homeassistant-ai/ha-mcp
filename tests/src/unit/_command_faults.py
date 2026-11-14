"""What Core received, and the two ways a client gets no answer to a command.

Each helper re-registers a command's own handler with its own schema: to see
the messages Core received for it, to hold it until the client's wait has run
out, or to drop the connection once Core has it. Core's handler still answers
every message it is handed; nothing here answers for it.
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
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    get_websocket_client,
)

from ._ws_transport import abort_transport

# How long the client waits for an answer while a command is held: long enough
# for every command Core answers at once, short enough to keep the test fast.
HELD_WAIT_S = 1.0


async def pooled_socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled WebSocket the component reads and the capability probe share."""
    return await get_websocket_client(
        url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
    )


def _handler(hass: HomeAssistant, command: str) -> tuple[Any, Any]:
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]
    return core_handler, schema


@contextmanager
def received(hass: HomeAssistant, *commands: str) -> Iterator[dict[str, list[Any]]]:
    """The messages Core received for each of ``commands`` meanwhile."""
    seen: dict[str, list[Any]] = {command: [] for command in commands}
    originals = {command: _handler(hass, command) for command in commands}

    def observe(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        # A copy: a handler may add keys to the message it is handed.
        seen[msg["type"]].append(dict(msg))
        originals[msg["type"]][0](core, connection, msg)

    for command, (_, schema) in originals.items():
        websocket_api.async_register_command(hass, command, observe, schema)
    try:
        yield seen
    finally:
        for command, (core_handler, schema) in originals.items():
            websocket_api.async_register_command(hass, command, core_handler, schema)


@asynccontextmanager
async def unanswered(
    hass: HomeAssistant, command: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """Core holds ``command`` until the client stopped waiting, then answers it.

    The client's wait is cut to ``HELD_WAIT_S`` meanwhile; Core's handler runs
    once the block ends, and the test waits for it.
    """
    core_handler, schema = _handler(hass, command)
    release = asyncio.Event()

    async def hold(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        await release.wait()
        core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, command, websocket_api.async_response(hold), schema
    )
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", HELD_WAIT_S)
    try:
        yield
    finally:
        release.set()
        websocket_api.async_register_command(hass, command, core_handler, schema)
        await hass.async_block_till_done(wait_background_tasks=True)


@contextmanager
def dropped_on(
    hass: HomeAssistant, command: str, socket: HomeAssistantWebSocketClient
) -> Iterator[None]:
    """``socket`` drops, with no close frame, as soon as Core receives ``command``."""
    core_handler, schema = _handler(hass, command)

    def drop(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        abort_transport(socket)

    websocket_api.async_register_command(hass, command, drop, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)
