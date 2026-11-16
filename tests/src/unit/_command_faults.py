"""What Core received, a change in Core just before it handles a command, the
two ways a client gets no answer to a command, and a command that takes one
more key.

Each helper re-registers a command's own handler: with its own schema to see
the messages Core received for it, to change Core before the handler runs, to
hold it until the client's wait has run out, or to drop the connection once
Core has it; with one more key in that schema to stand for a Core release
whose command takes the key. Core's handler still answers every message it is
handed; nothing here answers for it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import pytest
import voluptuous as vol
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


@contextmanager
def before_handling(
    hass: HomeAssistant, command: str, change: Callable[[dict[str, Any]], None]
) -> Iterator[None]:
    """``change`` runs on each message Core receives for ``command`` meanwhile,
    before the command's handler: what happens in Core between a caller's read
    and its question."""
    core_handler, schema = _handler(hass, command)

    def changed_first(
        core: HomeAssistant, connection: Any, msg: dict[str, Any]
    ) -> None:
        change(msg)
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, command, changed_first, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


@asynccontextmanager
async def unanswered(
    hass: HomeAssistant, command: str, monkeypatch: pytest.MonkeyPatch, *also: str
) -> AsyncIterator[None]:
    """Core holds ``command``, and each command of ``also``, until the client
    stopped waiting, then answers it.

    The client's wait is cut to ``HELD_WAIT_S`` meanwhile; Core's handlers run
    once the block ends, and the test waits for them.
    """
    originals = {name: _handler(hass, name) for name in (command, *also)}
    release = asyncio.Event()

    async def hold(core: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
        await release.wait()
        originals[msg["type"]][0](core, connection, msg)

    for name, (_, schema) in originals.items():
        websocket_api.async_register_command(
            hass, name, websocket_api.async_response(hold), schema
        )
    monkeypatch.setattr(websocket_client, "DEFAULT_COMMAND_WAIT_TIMEOUT", HELD_WAIT_S)
    try:
        yield
    finally:
        release.set()
        for name, (core_handler, schema) in originals.items():
            websocket_api.async_register_command(hass, name, core_handler, schema)
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


@contextmanager
def taking(hass: HomeAssistant, command: str, key: str) -> Iterator[None]:
    """While the block runs, Core's ``command`` also takes ``key``, as on a
    Core release whose command grew that field."""
    core_handler, schema = _handler(hass, command)
    websocket_api.async_register_command(
        hass, command, core_handler, schema.extend({vol.Optional(key): str})
    )
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)
