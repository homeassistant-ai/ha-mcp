"""Regression tests for issue #1994: pooled clients after an event-loop change.

``WebSocketManager`` is a process-wide singleton, but its pooled connections
belong to the event loop they were created on. The embedded custom component
runs the server in a worker thread with its own loop and closes that loop on
teardown, so an integration reload hands the surviving manager a pool of
clients whose transports and futures belong to a loop that is already gone.

Awaiting ``client.disconnect()`` for such a client from the new loop raises
``RuntimeError: Task ... got Future ... attached to a different loop``. That is
neither ``OSError`` nor ``CancelledError``, so before the fix it escaped the
best-effort catch, the pool was never cleared, ``_current_loop`` was never
updated, and every subsequent WebSocket-backed call re-entered the same branch
and failed identically until Home Assistant was restarted.

The tests below pin the two halves of the fix: the pool is detached before any
cleanup runs, and cleanup for a still-running owning loop happens on that loop
instead of being awaited from the new one.
"""

import asyncio
import threading

import pytest

from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    WebSocketManager,
)

CROSS_LOOP_MESSAGE = (
    "Task <Task pending name='Task-1'> got Future <Future pending> "
    "attached to a different loop"
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


def test_get_client_recovers_after_the_owning_loop_was_closed(manager):
    """A second loop gets a fresh client instead of the cross-loop failure.

    Both halves matter: ``get_client`` must return normally, and the stale
    client must never be awaited, because its loop is closed by then.
    """
    stale = StubWebSocketClient(disconnect_error=RuntimeError(CROSS_LOOP_MESSAGE))
    fresh = StubWebSocketClient()
    handed_out = iter((stale, fresh))
    manager.configure(client_factory=lambda url, token: next(handed_out))

    first = asyncio.run(manager.get_client(url="http://ha.local", token="t"))
    assert first is stale

    # A different loop: this is the call that used to raise and leave the pool
    # permanently stale.
    second = asyncio.run(manager.get_client(url="http://ha.local", token="t"))

    assert second is fresh
    assert stale.disconnect_calls == 0
    assert list(manager._clients.values()) == [fresh]
    assert len(manager._last_used) == 1


async def test_stale_disconnect_runs_on_a_still_running_owning_loop(manager):
    """Two live loops: cleanup runs on the loop that owns the client.

    The discriminating assertion is which loop executed the disconnect. Simply
    awaiting it from the new loop also sets the call counter, so the counter
    alone would pass against the unfixed code.
    """
    stale = StubWebSocketClient()
    fresh = StubWebSocketClient()
    handed_out = iter((stale, fresh))
    manager.configure(client_factory=lambda url, token: next(handed_out))

    owning_loop = asyncio.new_event_loop()
    worker = threading.Thread(target=owning_loop.run_forever, daemon=True)
    worker.start()
    try:
        first = asyncio.run_coroutine_threadsafe(
            manager.get_client(url="http://ha.local", token="t"), owning_loop
        ).result(timeout=10)
        assert first is stale

        second = await manager.get_client(url="http://ha.local", token="t")

        assert second is fresh
        assert stale.disconnected.wait(timeout=10)
        assert stale.disconnect_calls == 1
        assert stale.disconnect_loop is owning_loop
        assert list(manager._clients.values()) == [fresh]
    finally:
        owning_loop.call_soon_threadsafe(owning_loop.stop)
        worker.join(timeout=10)
        owning_loop.close()


async def test_disconnect_clears_the_pool_even_when_a_client_raises(manager):
    """``WebSocketManager.disconnect`` leaves no pooled state behind.

    A raising client used to abort the loop before ``_clients.clear()``, which
    is the same permanently-stale-pool failure on the shutdown path.
    """
    failing = StubWebSocketClient(disconnect_error=RuntimeError(CROSS_LOOP_MESSAGE))
    manager.configure(client_factory=lambda url, token: failing)

    await manager.get_client(url="http://ha.local", token="t")
    assert manager._clients

    await manager.disconnect()

    assert failing.disconnect_calls == 1
    assert manager._clients == {}
    assert manager._last_used == {}
    assert manager._current_loop is None
