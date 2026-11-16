"""What the WebSocket client keeps and sends, seen from outside, on a real Core.

The client under test is the server's own ``HomeAssistantWebSocketClient``
connected to Core's ``websocket_api`` (``_ws_transport.connected_ws``). These
helpers watch what it registers, stall its transport, or hold Core's answer to
its login; Core still answers everything it is sent.
"""

from __future__ import annotations

import asyncio
import gc
import weakref
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.websocket_api.auth import AuthPhase
from homeassistant.core import HomeAssistant

from ha_mcp.client import websocket_client
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.client.websocket_state import WebSocketConnectionState

from ._held_command import HeldCommand
from ._ws_transport import SpawnedTasks

WS_LOGGER = "ha_mcp.client.websocket_client"
EVENT = "b11_ws_client"


def subscribed(hass: HomeAssistant, event_type: str = EVENT) -> int:
    """How many listeners Core's bus has for ``event_type``."""
    return hass.bus.async_listeners().get(event_type, 0)


async def collect() -> None:
    """Collect what nothing holds any more."""
    # In debug mode the loop keeps the handle that last resumed a task, and
    # through it that task's frame; one step lets it go.
    await asyncio.sleep(0)
    gc.collect()


async def released(ref: weakref.ref[Any]) -> None:
    """Nothing holds ``ref``'s object once the loop has let go of what it last ran.

    The loop's own handles go within a few steps; an object the client kept
    stays, and the failure names what holds it.
    """
    for _ in range(50):
        await collect()
        if ref() is None:
            return
        await asyncio.sleep(0.01)
    holders = [
        f"{type(holder).__name__}: {holder!r:.200}"
        for holder in gc.get_referrers(ref())
    ]
    raise AssertionError(f"still held by {holders}")


class Registered:
    """Weak references to what the client registered while recording."""

    def __init__(self) -> None:
        self.pending: list[weakref.ref[Any]] = []
        self.events: list[weakref.ref[Any]] = []
        self.queues: list[weakref.ref[Any]] = []

    async def alive(self) -> list[str]:
        """The kinds whose registrations something still holds on to."""
        await collect()
        return [
            kind
            for kind, refs in (
                ("pending", self.pending),
                ("event", self.events),
                ("queue", self.queues),
            )
            if any(ref() is not None for ref in refs)
        ]


@contextmanager
def registered() -> Iterator[Registered]:
    """Record every pending future, event future and queue registered meanwhile."""
    seen = Registered()

    def recording(
        name: str, into: list[weakref.ref[Any]]
    ) -> Callable[[WebSocketConnectionState, int], Any]:
        original = getattr(WebSocketConnectionState, name)

        def record(state: WebSocketConnectionState, message_id: int) -> Any:
            item = original(state, message_id)
            into.append(weakref.ref(item))
            return item

        return record

    with (
        patch.object(
            WebSocketConnectionState,
            "register_pending_request",
            recording("register_pending_request", seen.pending),
        ),
        patch.object(
            WebSocketConnectionState,
            "register_event_response",
            recording("register_event_response", seen.events),
        ),
        patch.object(
            WebSocketConnectionState,
            "register_subscription_queue",
            recording("register_subscription_queue", seen.queues),
        ),
    ):
        yield seen


@contextmanager
def stalled_send(client: HomeAssistantWebSocketClient) -> Iterator[asyncio.Event]:
    """The client's next frame is written, then its send never returns.

    As a send stuck in flow-control drainage: Core has the frame, the caller
    is still inside the send. Later frames go out as usual. The event is set
    once the stalled frame is written.
    """
    websocket = client.websocket
    assert websocket is not None
    send = websocket.send
    written = asyncio.Event()

    async def write_then_stall(message: Any, *args: Any, **kwargs: Any) -> None:
        await send(message, *args, **kwargs)
        if written.is_set():
            return
        written.set()
        await asyncio.Event().wait()

    with patch.object(websocket, "send", write_then_stall):
        yield written


def short_cleanup(monkeypatch: pytest.MonkeyPatch, seconds: float = 0.05) -> None:
    """Cut the budget a cancelled call gives its own cleanup."""
    monkeypatch.setattr(websocket_client, "CLEANUP_TIMEOUT_SECONDS", seconds)


def client_log(caplog: pytest.LogCaptureFixture, text: str) -> list[int]:
    """The levels of the client's log records that mention ``text``."""
    return [
        record.levelno
        for record in caplog.records
        if record.name == WS_LOGGER and text in record.getMessage()
    ]


async def settled(client: HomeAssistantWebSocketClient, spawned: SpawnedTasks) -> None:
    """Let every background release the client started finish.

    Core answers in order, so once the ping is back every late acknowledgement
    has been read and its release started.
    """
    assert await client.ping() is True
    await asyncio.gather(*spawned.tasks, return_exceptions=True)


@asynccontextmanager
async def held_auth(
    monkeypatch: pytest.MonkeyPatch, *, first_only: bool = False
) -> AsyncIterator[HeldCommand]:
    """Core holds its answer to a login until released, then answers it."""
    held = HeldCommand()
    core_handle = AuthPhase.async_handle

    async def hold_then_answer(auth: AuthPhase, msg: Any) -> Any:
        held.calls += 1
        if not first_only or held.calls == 1:
            held.arrived.set()
            await held.release.wait()
        return await core_handle(auth, msg)

    monkeypatch.setattr(AuthPhase, "async_handle", hold_then_answer)
    try:
        yield held
    finally:
        held.release.set()
