"""Hold one WebSocket command in Core until the test releases it.

The command's own handler is re-registered with its own schema; a held request
waits, then the handler answers it, so every reply is still Core's.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant


class HeldCommand:
    """Requests that have reached Core's handler and are waiting for release."""

    def __init__(self) -> None:
        self.arrived = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def reached_by(self, call: asyncio.Task[Any]) -> None:
        """Wait until ``call``'s request is held at Core.

        A call that ends first never sent it: its own error is raised, and a
        call that returned fails the test, instead of the wait hanging.
        """
        arrived = asyncio.ensure_future(self.arrived.wait())
        try:
            await asyncio.wait({call, arrived}, return_when=asyncio.FIRST_COMPLETED)
            if not arrived.done():
                pytest.fail(f"the call ended before Core had it: {call.result()!r}")
        finally:
            arrived.cancel()
            await asyncio.gather(arrived, return_exceptions=True)


@asynccontextmanager
async def held_command(
    hass: HomeAssistant, command: str, *, first_only: bool = False
) -> AsyncIterator[HeldCommand]:
    """Hold ``command`` (every request, or only the first) until released.

    Leaving the block releases what is still held and lets Core answer it.
    """
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]
    held = HeldCommand()

    async def hold_then_answer(
        core: HomeAssistant, connection: websocket_api.ActiveConnection, msg: Any
    ) -> None:
        held.calls += 1
        if not first_only or held.calls == 1:
            held.arrived.set()
            await held.release.wait()
        core_handler(core, connection, msg)

    websocket_api.async_register_command(
        hass, command, websocket_api.async_response(hold_then_answer), schema
    )
    try:
        yield held
    finally:
        held.release.set()
        await hass.async_block_till_done()
