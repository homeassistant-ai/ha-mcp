"""Regression tests for issue #2127: replacing a stale pooled client.

When ``get_client`` finds a pooled client whose connection has dropped, it
used to pop the pool entry and simply drop the reference. A dead connection
can still own a parked reader task and a half-open socket, so the drop
abandoned both to garbage collection — the GC's asyncgen finalizer then
acloses the reader's ``Connection.__aiter__`` mid-``__anext__`` and the loop
logs ``RuntimeError: aclose(): asynchronous generator is already running``.
The replacement path must disconnect the stale client on the loop that owns
it (the current one — a loop change already detached the pool earlier in the
same call).
"""

import asyncio
import threading
from collections.abc import Callable

import pytest

from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    WebSocketManager,
)


class StubWebSocketClient:
    """Minimal pooled-client stand-in that records its disconnect calls."""

    def __init__(self, *, disconnect_error: BaseException | None = None) -> None:
        self.is_connected = True
        self.disconnect_error = disconnect_error
        self.disconnect_calls = 0
        self.disconnected = threading.Event()
        self.disconnect_loop: asyncio.AbstractEventLoop | None = None
        self.last_connect_error: str | None = None

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_loop = asyncio.get_running_loop()
        self.disconnected.set()
        if self.disconnect_error is not None:
            raise self.disconnect_error


@pytest.fixture
def manager():
    """Yield the singleton manager with isolated, restored pool state."""
    mgr = WebSocketManager()
    saved = (
        dict(mgr._clients),
        dict(mgr._last_used),
        mgr._current_loop,
        mgr._lock,
        mgr._lock_loop,
        mgr._client_factory,
    )
    mgr._clients.clear()
    mgr._last_used.clear()
    mgr._current_loop = None
    mgr._lock = None
    mgr._lock_loop = None
    try:
        yield mgr
    finally:
        mgr._clients.clear()
        mgr._clients.update(saved[0])
        mgr._last_used.clear()
        mgr._last_used.update(saved[1])
        mgr._current_loop = saved[2]
        mgr._lock = saved[3]
        mgr._lock_loop = saved[4]
        mgr.configure(client_factory=saved[5] or HomeAssistantWebSocketClient)


def _hand_out(*clients: StubWebSocketClient) -> Callable[[str, str], object]:
    handed = iter(clients)
    return lambda url, token: next(handed)


async def test_get_client_disconnects_the_replaced_stale_client(manager):
    """The dropped client is disconnected, on the current loop, not abandoned.

    The loop assertion is the discriminating half: the stale client is
    same-loop by construction, so its reader task and socket can and must be
    torn down right here rather than left to garbage collection.
    """
    stale = StubWebSocketClient()
    fresh = StubWebSocketClient()
    manager.configure(client_factory=_hand_out(stale, fresh))

    first = await manager.get_client(url="http://ha.local", token="t")
    assert first is stale

    stale.is_connected = False

    second = await manager.get_client(url="http://ha.local", token="t")

    assert second is fresh
    assert stale.disconnect_calls == 1
    assert stale.disconnect_loop is asyncio.get_running_loop()
    assert list(manager._clients.values()) == [fresh]
    assert len(manager._last_used) == 1


async def test_stale_client_disconnect_failure_does_not_block_replacement(
    manager,
):
    """A raising disconnect is best-effort: the caller still gets a client.

    ``RuntimeError`` is the realistic failure shape (a dead transport mid
    ``websocket.close()``); it must be swallowed by the replacement path, and
    the raising client must still be gone from the pool.
    """
    stale = StubWebSocketClient(disconnect_error=RuntimeError("Event loop is closed"))
    fresh = StubWebSocketClient()
    manager.configure(client_factory=_hand_out(stale, fresh))

    first = await manager.get_client(url="http://ha.local", token="t")
    assert first is stale

    stale.is_connected = False

    second = await manager.get_client(url="http://ha.local", token="t")

    assert second is fresh
    assert stale.disconnect_calls == 1
    assert list(manager._clients.values()) == [fresh]
