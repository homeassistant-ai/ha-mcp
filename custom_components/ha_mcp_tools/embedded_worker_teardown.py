"""Worker-loop teardown for the in-process server (#2127).

The embedded worker hand-rolls its event loop instead of using asyncio.run, so
it does the runner's teardown itself: cancel what is left pending, finalize
async generators, join the default executor and close the loop.
shutdown_server_resources(_bounded) mirrors the CLI runner's connection cleanup
and runs from EmbeddedServerManager._serve while the loop is still live.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Budget for each bounded teardown phase (_serve's resource cleanup, the
# pending-task sweep, the default-executor join). Mirrors the CLI runner's
# SHUTDOWN_TIMEOUT_SECONDS: the phases together must finish inside
# embedded_server's _STOP_JOIN_TIMEOUT_SECONDS, or
# EmbeddedServerManager.async_stop declares the worker orphaned while the old
# thread is still executing shared ha_mcp modules. The budget bounds only the
# phases that accept one — asyncgen finalization and uvicorn's post-drain
# lifespan shutdown remain unbounded — so it buys headroom, not a hard ceiling
# on the join.
TEARDOWN_TIMEOUT_SECONDS = 2.0


async def shutdown_server_resources_bounded(server: Any) -> None:
    """Run :func:`shutdown_server_resources` inside the teardown budget.

    An unresponsive peer's close handshake (websockets' 10s default
    close_timeout) must not eat embedded_server's whole
    ``_STOP_JOIN_TIMEOUT_SECONDS`` join budget. Cancel-and-abandon, not
    ``wait_for``: ``wait_for`` awaits the cancelled coroutine before raising,
    and the cleanup stack swallows ``CancelledError`` at several layers
    (per-client in ``WebSocketManager.disconnect``, in ``client.disconnect``'s
    own task-cancel guard), so a straggler must be left to
    :func:`_cancel_pending_tasks` instead of being joined here.
    """
    task = asyncio.ensure_future(shutdown_server_resources(server))
    _done, pending = await asyncio.wait({task}, timeout=TEARDOWN_TIMEOUT_SECONDS)
    if pending:
        task.cancel()
        _LOGGER.warning("Embedded resource cleanup timed out")


async def shutdown_server_resources(server: Any) -> None:
    """Release the served stack's Home Assistant connections on its own loop.

    Mirrors the CLI runner's ``_cleanup_resources`` (``ha_mcp.__main__``)
    without importing it: stop the WebSocket listener service, disconnect the
    pooled WebSocket clients, and close the server's HTTP client. Every step
    guards independently — a failing step must not keep the next one from
    running, and no failure here may mask the serve outcome.
    """
    try:
        from ha_mcp.client.websocket_listener import stop_websocket_listener

        await stop_websocket_listener()
    except ImportError:
        _LOGGER.debug("WebSocket listener module not available")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("WebSocket listener cleanup failed: %s", err)

    try:
        from ha_mcp.client.websocket_client import websocket_manager

        await websocket_manager.disconnect()
    except ImportError:
        _LOGGER.debug("WebSocket manager module not available")
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("WebSocket manager cleanup failed: %s", err)

    try:
        await server.close()
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Server cleanup failed: %s", err)


def _cancel_pending_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel every task still pending on ``loop`` and wait them out.

    Mirrors ``asyncio.runners._cancel_all_tasks`` — the step ``asyncio.run``
    performs between the main coroutine returning and asyncgen finalization,
    which the embedded worker's hand-rolled loop lifecycle skipped (issue
    #2127). Without it, tasks the served stack leaves behind — WebSocket reader
    tasks parked in ``Connection.__aiter__``, sse_starlette's
    ``_shutdown_watcher`` poll (unreachable by its uvicorn signal hooks on a
    non-main thread) — are still pending at teardown: ``shutdown_asyncgens()``
    then acloses generators mid-``__anext__`` (``RuntimeError: aclose():
    asynchronous generator is already running``) and ``loop.close()`` destroys
    the survivors ("Task was destroyed but it is pending!"), one error pair per
    entry reload.

    Abandoning is inherently partial: a task that ignores cancellation past
    the budget and still drives an async generator leaves that generator
    running, and ``shutdown_asyncgens()`` then reports the same ``aclose()``
    error this sweep exists to remove. The ignored-cancellation warning
    below is the tell when that residual fires.
    """
    pending = asyncio.all_tasks(loop)
    if not pending:
        return
    for task in pending:
        task.cancel()
    # Bounded, unlike asyncio.runners: EmbeddedServerManager.async_stop joins
    # the worker for only embedded_server's _STOP_JOIN_TIMEOUT_SECONDS, so a
    # task that ignores cancellation gets logged and abandoned rather than
    # hanging the join (the CLI's _cancel_tasks does the same, issue #2027
    # precedent).
    done, still_pending = loop.run_until_complete(
        asyncio.wait(pending, timeout=TEARDOWN_TIMEOUT_SECONDS)
    )
    if still_pending:
        _LOGGER.warning(
            "%d task(s) ignored cancellation during worker-loop teardown",
            len(still_pending),
        )
    for task in done:
        if task.cancelled():
            continue
        exc = task.exception()
        if exc is not None:
            _LOGGER.warning(
                "Task %r raised during worker-loop teardown: %r",
                task.get_name(),
                exc,
            )


def teardown_worker_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Drain and close the worker loop with ``asyncio.run`` teardown parity.

    Teardown is best-effort but never SILENT: a raise here must not mask the
    primary outcome, yet a recurring cleanup failure (leaking executor threads
    across reloads) has to be visible in the logs.
    Each step gets its own guard so one failure cannot skip the others.
    """
    try:
        _cancel_pending_tasks(loop)
    except Exception:
        _LOGGER.warning(
            "Worker-loop task cancellation failed during teardown",
            exc_info=True,
        )
    for _label, _coro_factory in (
        ("asyncgen", loop.shutdown_asyncgens),
        # The executor join is bounded too: a stuck executor thread must not
        # keep the worker alive past the join deadline (abandoning it emits
        # a RuntimeWarning instead of hanging). The runtime has accepted
        # timeout= since Python 3.12; typeshed's AbstractEventLoop signature
        # lags behind, hence the scoped ignore.
        (
            "executor",
            partial(
                loop.shutdown_default_executor,
                timeout=TEARDOWN_TIMEOUT_SECONDS,  # type: ignore[call-arg]
            ),
        ),
    ):
        try:
            loop.run_until_complete(_coro_factory())
        except Exception:
            _LOGGER.warning(
                "Worker-loop %s shutdown failed during teardown",
                _label,
                exc_info=True,
            )
    loop.close()
