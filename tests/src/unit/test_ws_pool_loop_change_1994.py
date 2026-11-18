"""Issue #1994: actual Core connections remain isolated across event loops."""

import asyncio
import logging
import threading
from collections.abc import AsyncIterator, Coroutine
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)

CROSS_LOOP_MESSAGE = "Future attached to a different loop"


class LoopWorker:
    """Run real socket operations on another loop while Core keeps serving."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread: threading.Thread | None = None
        self.clients: list[HomeAssistantWebSocketClient] = []
        self.start()

    def start(self) -> None:
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    async def run[T](self, operation: Coroutine[Any, Any, T]) -> T:
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        return await asyncio.wait_for(asyncio.wrap_future(future), timeout=10)

    async def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        assert self.thread is not None
        await asyncio.to_thread(self.thread.join, 10)
        assert not self.thread.is_alive()

    async def close(self) -> None:
        if self.loop.is_closed():
            return
        if not self.loop.is_running():
            self.start()
        for client in self.clients:
            # Use the real cleanup even when a test injected a transport error.
            await self.run(HomeAssistantWebSocketClient.disconnect(client))
        await self.run(self.loop.shutdown_asyncgens())
        await self.stop()
        self.loop.close()


@pytest.fixture
async def owning_loop(core_client: HomeAssistantClient) -> AsyncIterator[LoopWorker]:
    worker = LoopWorker()
    try:
        yield worker
    finally:
        await worker.close()


async def get_client(core_client: HomeAssistantClient, token: str | None = None):
    return await websocket_manager.get_client(
        url=core_client.base_url, token=token or core_client.token, verify_ssl=False
    )


async def get_client_on(
    worker: LoopWorker, core_client: HomeAssistantClient, token: str | None = None
):
    client = await worker.run(get_client(core_client, token))
    worker.clients.append(client)
    assert await worker.run(client.ping())
    return client


def pooled(core_client: HomeAssistantClient):
    return websocket_manager.pooled_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=False
    )


async def settled(worker: LoopWorker) -> None:
    async def two_steps() -> None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    await worker.run(two_steps())


async def test_get_client_recovers_after_the_owning_loop_was_closed(
    core_client, owning_loop, monkeypatch
):
    stale = await get_client_on(owning_loop, core_client)
    # Close the socket on its owner before closing the loop, without clearing
    # the manager's pool. This reproduces a reload without leaking a transport.
    await owning_loop.close()
    cleanup = AsyncMock(side_effect=RuntimeError(CROSS_LOOP_MESSAGE))
    monkeypatch.setattr(stale, "disconnect", cleanup)

    fresh = await get_client(core_client)

    assert fresh is not stale
    cleanup.assert_not_awaited()
    assert pooled(core_client) is fresh
    assert await fresh.ping()


async def test_stale_disconnect_runs_on_a_still_running_owning_loop(
    core_client, owning_loop, monkeypatch
):
    stale = await get_client_on(owning_loop, core_client)
    original_disconnect = stale.disconnect
    finished = threading.Event()
    cleanup_loops = []

    async def disconnect():
        cleanup_loops.append(asyncio.get_running_loop())
        await original_disconnect()
        finished.set()

    monkeypatch.setattr(stale, "disconnect", disconnect)
    fresh = await get_client(core_client)

    assert fresh is not stale
    assert await asyncio.to_thread(finished.wait, 10)
    assert cleanup_loops == [owning_loop.loop]
    assert not stale.is_connected
    assert pooled(core_client) is fresh
    assert await fresh.ping()


async def test_stale_disconnect_is_deferred_to_a_stopped_owning_loop(
    core_client, owning_loop, monkeypatch
):
    stale = await get_client_on(owning_loop, core_client)
    original_disconnect = stale.disconnect
    finished = threading.Event()
    cleanup_loops = []

    async def disconnect():
        cleanup_loops.append(asyncio.get_running_loop())
        await original_disconnect()
        finished.set()

    monkeypatch.setattr(stale, "disconnect", disconnect)
    await owning_loop.stop()
    fresh = await get_client(core_client)

    assert fresh is not stale
    assert cleanup_loops == []
    assert stale.is_connected
    owning_loop.start()
    assert await asyncio.to_thread(finished.wait, 10)
    assert cleanup_loops == [owning_loop.loop]
    assert not stale.is_connected
    assert pooled(core_client) is fresh
    assert await fresh.ping()


async def test_disconnect_clears_the_pool_even_when_a_client_raises(
    core_client, monkeypatch
):
    failing = await get_client(core_client)
    original_disconnect = failing.disconnect
    pool_at_disconnect = []

    async def disconnect():
        pool_at_disconnect.append(pooled(core_client))
        await original_disconnect()
        raise RuntimeError(CROSS_LOOP_MESSAGE)

    monkeypatch.setattr(failing, "disconnect", disconnect)
    await websocket_manager.disconnect()

    assert pool_at_disconnect == [None]
    assert pooled(core_client) is None
    fresh = await get_client(core_client)
    assert fresh is not failing
    assert await fresh.ping()


async def test_a_stale_disconnect_cancelled_with_its_loop_is_logged(
    core_client, owning_loop, monkeypatch, caplog
):
    stale = await get_client_on(owning_loop, core_client)
    started = threading.Event()
    cleanup_tasks = []

    async def disconnect():
        task = asyncio.current_task()
        assert task is not None
        cleanup_tasks.append(task)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(stale, "disconnect", disconnect)
    caplog.set_level(logging.DEBUG, logger=websocket_client.logger.name)
    fresh = await get_client(core_client)
    assert await asyncio.to_thread(started.wait, 10)
    owning_loop.loop.call_soon_threadsafe(cleanup_tasks[0].cancel)
    await settled(owning_loop)

    assert "cancelled with its loop" in caplog.text
    assert fresh is not stale
    assert await fresh.ping()


async def test_a_failed_stale_disconnect_is_reported(
    core_client, owning_loop, monkeypatch, caplog
):
    stale = await get_client_on(owning_loop, core_client)
    cleanup = AsyncMock(side_effect=RuntimeError(CROSS_LOOP_MESSAGE))
    monkeypatch.setattr(stale, "disconnect", cleanup)
    caplog.set_level(logging.DEBUG, logger=websocket_client.logger.name)

    fresh = await get_client(core_client)
    await settled(owning_loop)

    cleanup.assert_awaited_once()
    assert "Stale WebSocket disconnect failed" in caplog.text
    assert CROSS_LOOP_MESSAGE in caplog.text
    assert fresh is not stale
    assert await fresh.ping()


async def test_a_loop_closing_mid_schedule_does_not_stop_the_cleanup(
    core_client, hass_read_only_access_token, owning_loop, monkeypatch, caplog
):
    first = await get_client_on(owning_loop, core_client)
    second = await get_client_on(owning_loop, core_client, hass_read_only_access_token)
    calls = 0

    def raise_runtime(_coro, _loop):
        nonlocal calls
        calls += 1
        raise RuntimeError("Event loop is closed")

    caplog.set_level(logging.DEBUG, logger=websocket_client.logger.name)
    with monkeypatch.context() as patched:
        patched.setattr(websocket_client.asyncio, "run_coroutine_threadsafe", raise_runtime)
        fresh = await get_client(core_client)

    assert fresh is not first
    assert fresh is not second
    assert calls == 2
    assert caplog.text.count("Could not schedule disconnect") == 2
    assert await owning_loop.run(first.ping())
    assert await owning_loop.run(second.ping())
    assert pooled(core_client) is fresh
    assert await fresh.ping()
