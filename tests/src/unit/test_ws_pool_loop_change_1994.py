"""Regression tests for issue #1994: pooled clients after an event-loop change.

``WebSocketManager`` is a process-wide singleton, but its pooled connections
belong to the event loop they were created on. The embedded custom component
runs the server in a worker thread with its own loop and closes that loop on
teardown, so an integration reload hands the surviving manager a pool of
clients whose transports and futures belong to a loop that is already gone.

Awaiting ``client.disconnect()`` for such a client from the new loop raises
``RuntimeError: Task ... got Future ... attached to a different loop``. That is
neither ``OSError`` nor ``CancelledError``, so before the fix it escaped the
best-effort catch, the pool was never cleared, the manager's loop reference
was never updated, and every subsequent WebSocket-backed call re-entered the
same branch and failed identically until Home Assistant was restarted.

The tests below pin the two halves of the fix: the pool is detached before any
cleanup runs, and cleanup for a still-running owning loop happens on that loop
instead of being awaited from the new one.
"""

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator

import pytest

from ha_mcp.client import websocket_client
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    WebSocketManager,
)

CROSS_LOOP_MESSAGE = (
    "Task <Task pending name='Task-1'> got Future <Future pending> "
    "attached to a different loop"
)
URL = "http://ha.local"


class StubWebSocketClient:
    """Minimal pooled-client stand-in that records its disconnect calls."""

    def __init__(
        self, *, disconnect_error: BaseException | None = None, hang: bool = False
    ) -> None:
        self.is_connected = True
        self.disconnect_error = disconnect_error
        # Whether disconnect() never returns once it has started.
        self.hang = hang
        self.disconnect_calls = 0
        self.disconnected = threading.Event()
        self.disconnect_loop: asyncio.AbstractEventLoop | None = None
        self.last_connect_error: str | None = None
        # Set by a test that needs to observe manager state from inside the
        # disconnect call, i.e. mid-cleanup.
        self.observe_pool: Callable[[], object] | None = None
        self.pool_at_disconnect: list[object] = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.disconnect_loop = asyncio.get_running_loop()
        if self.observe_pool is not None:
            self.pool_at_disconnect.append(self.observe_pool())
        self.disconnected.set()
        if self.hang:
            await asyncio.Event().wait()
        if self.disconnect_error is not None:
            raise self.disconnect_error


def empty_the_pool(mgr: WebSocketManager) -> None:
    """Disconnect every pooled client on a loop of its own, leaving the
    thread's current loop as it was."""
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(mgr.disconnect())
    finally:
        loop.close()


@pytest.fixture
def manager() -> Iterator[WebSocketManager]:
    """Yield the singleton manager with an empty pool, and empty it again."""
    mgr = WebSocketManager()
    empty_the_pool(mgr)
    try:
        yield mgr
    finally:
        empty_the_pool(mgr)
        mgr.configure(client_factory=HomeAssistantWebSocketClient)


@pytest.fixture
def owning_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """A second event loop, running in a thread of its own."""
    loop = asyncio.new_event_loop()
    worker = threading.Thread(target=loop.run_forever, daemon=True)
    worker.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        worker.join(timeout=10)
        loop.close()


def pooled(manager: WebSocketManager, token: str = "t") -> object:
    return manager.pooled_client(url=URL, token=token)


def get_client_on(
    manager: WebSocketManager, loop: asyncio.AbstractEventLoop, token: str = "t"
) -> object:
    """``get_client`` called from a coroutine running on ``loop``."""
    return asyncio.run_coroutine_threadsafe(
        manager.get_client(url=URL, token=token), loop
    ).result(timeout=10)


def settle(loop: asyncio.AbstractEventLoop) -> None:
    """Let ``loop`` run what it already has queued, callbacks included."""

    async def two_steps() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run_coroutine_threadsafe(two_steps(), loop).result(timeout=10)


def test_get_client_recovers_after_the_owning_loop_was_closed(manager):
    """A second loop gets a fresh client instead of the cross-loop failure.

    Both halves matter: ``get_client`` must return normally, and the stale
    client must never be awaited, because its loop is closed by then.
    """
    stale = StubWebSocketClient(disconnect_error=RuntimeError(CROSS_LOOP_MESSAGE))
    fresh = StubWebSocketClient()
    handed_out = iter((stale, fresh))
    manager.configure(client_factory=lambda url, token: next(handed_out))

    first = asyncio.run(manager.get_client(url=URL, token="t"))
    assert first is stale

    # A different loop: this is the call that used to raise and leave the pool
    # permanently stale.
    second = asyncio.run(manager.get_client(url=URL, token="t"))

    assert second is fresh
    assert stale.disconnect_calls == 0
    assert pooled(manager) is fresh


async def test_stale_disconnect_runs_on_a_still_running_owning_loop(
    manager, owning_loop
):
    """Two live loops: cleanup runs on the loop that owns the client.

    The discriminating assertion is which loop executed the disconnect. Simply
    awaiting it from the new loop also sets the call counter, so the counter
    alone would pass against the unfixed code.
    """
    stale = StubWebSocketClient()
    fresh = StubWebSocketClient()
    handed_out = iter((stale, fresh))
    manager.configure(client_factory=lambda url, token: next(handed_out))

    assert get_client_on(manager, owning_loop) is stale

    second = await manager.get_client(url=URL, token="t")

    assert second is fresh
    assert stale.disconnected.wait(timeout=10)
    assert stale.disconnect_calls == 1
    assert stale.disconnect_loop is owning_loop
    assert pooled(manager) is fresh


def test_stale_disconnect_is_deferred_to_a_stopped_owning_loop(manager):
    """A stopped, not closed loop still gets the cleanup when it resumes.

    Such a loop owns live transports and can be restarted, so abandoning its
    clients would leak a connection and its background task on every loop
    swap. The discriminating assertion is that the disconnect runs on the
    owning loop only after that loop is resumed, not that it runs at all.
    """
    stale = StubWebSocketClient()
    fresh = StubWebSocketClient()
    handed_out = iter((stale, fresh))
    manager.configure(client_factory=lambda url, token: next(handed_out))

    owning_loop = asyncio.new_event_loop()
    try:
        first = owning_loop.run_until_complete(manager.get_client(url=URL, token="t"))
        assert first is stale
        assert not owning_loop.is_running()
        assert not owning_loop.is_closed()

        # A second loop takes over while the first one is merely stopped.
        second = asyncio.run(manager.get_client(url=URL, token="t"))

        assert second is fresh
        # Still queued: a stopped loop runs nothing until it is resumed.
        assert stale.disconnect_calls == 0

        async def drain() -> None:
            for _ in range(100):
                if stale.disconnected.is_set():
                    return
                await asyncio.sleep(0.01)

        owning_loop.run_until_complete(drain())

        assert stale.disconnect_calls == 1
        assert stale.disconnect_loop is owning_loop
        assert pooled(manager) is fresh
    finally:
        owning_loop.close()


async def test_disconnect_clears_the_pool_even_when_a_client_raises(manager):
    """``WebSocketManager.disconnect`` leaves no pooled client behind.

    Two independent properties, so both are asserted: the pool is already
    detached while a client is being disconnected (``pool_at_disconnect``),
    and a raising client does not abort the shutdown. Asserting only the final
    state would pass against the old order, where the raise was merely caught
    and the trailing ``clear()`` still ran.
    """
    failing = StubWebSocketClient(disconnect_error=RuntimeError(CROSS_LOOP_MESSAGE))
    manager.configure(client_factory=lambda url, token: failing)

    await manager.get_client(url=URL, token="t")
    assert pooled(manager) is failing
    failing.observe_pool = lambda: pooled(manager)

    await manager.disconnect()

    assert failing.disconnect_calls == 1
    assert failing.pool_at_disconnect == [None]
    assert pooled(manager) is None


def test_a_stale_disconnect_cancelled_with_its_loop_is_logged(
    manager, owning_loop, caplog
):
    """A fire-and-forget disconnect cancelled by its loop's shutdown logs that,
    and nothing raises: on a cancelled ``concurrent.futures.Future`` asking
    for its exception raises ``CancelledError`` itself."""
    stale = StubWebSocketClient(hang=True)
    handed_out = iter((stale, StubWebSocketClient()))
    manager.configure(client_factory=lambda url, token: next(handed_out))
    get_client_on(manager, owning_loop)
    caplog.set_level(logging.DEBUG, logger=websocket_client.logger.name)

    asyncio.run(manager.get_client(url=URL, token="t"))
    assert stale.disconnected.wait(timeout=10)

    async def cancel_the_rest() -> None:
        # What a loop's shutdown does to the tasks it still runs.
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task():
                task.cancel()

    asyncio.run_coroutine_threadsafe(cancel_the_rest(), owning_loop).result(10)
    settle(owning_loop)

    assert "cancelled with its loop" in caplog.text


def test_a_failed_stale_disconnect_is_reported(manager, owning_loop, caplog):
    """A scheduled disconnect that raised is surfaced at debug, not swallowed.

    Nobody awaits it, and a ``concurrent.futures.Future`` stays silent about
    an exception no one retrieved.
    """
    stale = StubWebSocketClient(disconnect_error=RuntimeError(CROSS_LOOP_MESSAGE))
    handed_out = iter((stale, StubWebSocketClient()))
    manager.configure(client_factory=lambda url, token: next(handed_out))
    get_client_on(manager, owning_loop)
    caplog.set_level(logging.DEBUG, logger=websocket_client.logger.name)

    asyncio.run(manager.get_client(url=URL, token="t"))
    assert stale.disconnected.wait(timeout=10)
    settle(owning_loop)

    assert "Stale WebSocket disconnect failed" in caplog.text
    assert CROSS_LOOP_MESSAGE in caplog.text


def test_a_loop_closing_mid_schedule_does_not_stop_the_cleanup(
    manager, owning_loop, monkeypatch, caplog
):
    """The race where the owning loop closes between the is_closed() check and
    ``run_coroutine_threadsafe`` is caught, and cleanup moves to the next
    client instead of propagating.

    ``run_coroutine_threadsafe`` is forced to raise ``RuntimeError`` (what a
    loop closing in that window actually raises) for the call that changes
    loop. Two stale clients pin the ``continue``: both are attempted, and the
    caller still gets its fresh client.
    """
    first = StubWebSocketClient()
    second = StubWebSocketClient()
    fresh = StubWebSocketClient()
    handed_out = iter((first, second, fresh))
    manager.configure(client_factory=lambda url, token: next(handed_out))
    get_client_on(manager, owning_loop, token="t1")
    get_client_on(manager, owning_loop, token="t2")

    calls = 0

    def raise_runtime(_coro, _loop):
        # Model the loop closing in the check->schedule window; the real
        # except branch is what must close the orphaned coroutine, not this.
        nonlocal calls
        calls += 1
        raise RuntimeError("Event loop is closed")

    caplog.set_level(logging.DEBUG, logger=websocket_client.logger.name)
    with monkeypatch.context() as patched:
        patched.setattr(
            websocket_client.asyncio, "run_coroutine_threadsafe", raise_runtime
        )
        handed = asyncio.run(manager.get_client(url=URL, token="t3"))

    assert handed is fresh
    assert calls == 2
    assert caplog.text.count("Could not schedule disconnect") == 2
    assert first.disconnect_calls == 0
    assert second.disconnect_calls == 0
