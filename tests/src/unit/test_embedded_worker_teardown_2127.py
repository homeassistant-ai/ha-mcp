"""Regression tests for issue #2127: worker-loop teardown and serve cleanup.

The embedded worker thread hand-rolls its loop lifecycle instead of using
``asyncio.run``, and its teardown skipped the runner's cancel-pending-tasks
step. Tasks the served stack leaves behind — WebSocket reader tasks parked in
``Connection.__aiter__``, sse_starlette's ``_shutdown_watcher`` poll — were
still pending when ``shutdown_asyncgens()`` ran, so it aclosed generators
mid-``__anext__`` (``RuntimeError: aclose(): asynchronous generator is
already running``) and ``loop.close()`` destroyed the survivors ("Task was
destroyed but it is pending!"), one error pair per entry reload.

Home Assistant and aiohttp are stubbed via ``_embedded_stubs``; the
``_shutdown_server_resources`` tests use the real ``ha_mcp`` package (which
imports neither), monkeypatched at its own module seams.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

from ._embedded_stubs import install

# Install stubs before importing the component module (isort barrier).
install()

import custom_components.ha_mcp_tools.embedded_server as es  # noqa: E402


def _park_reader_and_watcher(
    loop: asyncio.AbstractEventLoop,
) -> tuple[dict[str, asyncio.Task[Any]], list[str]]:
    """Recreate the served stack's leftovers on ``loop`` and park them.

    ``reader`` drives an async generator and suspends mid-``__anext__`` —
    the shape of the WebSocket client's ``async for message in
    self.websocket`` over ``Connection.__aiter__``. ``watcher`` is a plain
    long sleep — the shape of sse_starlette's ``_shutdown_watcher`` poll.
    """
    tasks: dict[str, asyncio.Task[Any]] = {}
    events: list[str] = []

    async def stream() -> AsyncIterator[None]:
        try:
            while True:
                await asyncio.sleep(3600)
                yield None
        finally:
            events.append("generator finalized")

    async def reader() -> None:
        async for _ in stream():
            pass

    async def start() -> None:
        tasks["reader"] = asyncio.ensure_future(reader())
        tasks["watcher"] = asyncio.ensure_future(asyncio.sleep(3600))
        # Two scheduler passes: the reader enters the generator and parks at
        # its await, leaving the generator suspended inside __anext__.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    loop.run_until_complete(start())
    return tasks, events


def test_teardown_cancels_pending_tasks_and_finalizes_generators() -> None:
    """The full teardown drains parked tasks without a single loop error.

    Both halves of the reported pair are discriminating: without the
    cancel-pending step, ``shutdown_asyncgens()`` acloses the reader's
    generator mid-``__anext__`` and reports ``RuntimeError`` through the
    loop's exception handler, and the watcher survives to ``loop.close()``
    still pending.
    """
    loop = asyncio.new_event_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    tasks, events = _park_reader_and_watcher(loop)

    es._teardown_worker_loop(loop)

    assert loop.is_closed()
    assert tasks["reader"].cancelled()
    assert tasks["watcher"].cancelled()
    assert events == ["generator finalized"]
    assert errors == []


def test_teardown_with_nothing_pending_still_closes_the_loop() -> None:
    """An already-drained loop tears down without a task-cancellation pass."""
    loop = asyncio.new_event_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))

    es._teardown_worker_loop(loop)

    assert loop.is_closed()
    assert errors == []


def test_teardown_survives_a_task_that_raises_on_cancel() -> None:
    """A task that turns cancellation into its own error cannot stop teardown.

    The raise is reported (never silent) but the loop still finalizes and
    closes — the per-step guards in ``_teardown_worker_loop`` and the
    exception sweep in ``_cancel_pending_tasks`` own this.
    """
    loop = asyncio.new_event_loop()

    async def defiant() -> None:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            raise RuntimeError("cleanup went sideways") from None

    async def start() -> dict[str, asyncio.Task[Any]]:
        task = asyncio.ensure_future(defiant())
        await asyncio.sleep(0)
        return {"defiant": task}

    tasks = loop.run_until_complete(start())

    es._teardown_worker_loop(loop)

    assert loop.is_closed()
    assert tasks["defiant"].done()
    assert isinstance(tasks["defiant"].exception(), RuntimeError)


async def test_shutdown_server_resources_runs_every_step(monkeypatch) -> None:
    """Listener stop, pool disconnect, and server close are all awaited."""
    import ha_mcp.client.websocket_client as ws_client
    import ha_mcp.client.websocket_listener as ws_listener

    stop_listener = AsyncMock(name="stop_websocket_listener")
    pool_disconnect = AsyncMock(name="websocket_manager.disconnect")
    monkeypatch.setattr(ws_listener, "stop_websocket_listener", stop_listener)
    monkeypatch.setattr(ws_client.websocket_manager, "disconnect", pool_disconnect)

    server = AsyncMock(name="server")

    await es._shutdown_server_resources(server)

    stop_listener.assert_awaited_once()
    pool_disconnect.assert_awaited_once()
    server.close.assert_awaited_once()


async def test_shutdown_server_resources_isolates_step_failures(
    monkeypatch,
) -> None:
    """A raising step is logged and the later steps still run."""
    import ha_mcp.client.websocket_client as ws_client
    import ha_mcp.client.websocket_listener as ws_listener

    stop_listener = AsyncMock(side_effect=RuntimeError("listener boom"))
    pool_disconnect = AsyncMock(side_effect=OSError("socket boom"))
    monkeypatch.setattr(ws_listener, "stop_websocket_listener", stop_listener)
    monkeypatch.setattr(ws_client.websocket_manager, "disconnect", pool_disconnect)

    server = AsyncMock(name="server")

    await es._shutdown_server_resources(server)

    stop_listener.assert_awaited_once()
    pool_disconnect.assert_awaited_once()
    server.close.assert_awaited_once()
