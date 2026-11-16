"""How ha-mcp stops when told to, and when it refuses to start.

Each test runs a real entry point (``ha-mcp`` over stdio, ``ha-mcp-web``) to
its end: the handlers it registers receive a real signal, the shutdown sequence
and its cleanup run, and the exit code is checked. Only the server's transport
is replaced (see ``_graceful_shutdown_core``).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import signal
from typing import IO
from unittest.mock import AsyncMock

import pytest

from ha_mcp.__main__ import SHUTDOWN_TIMEOUT_SECONDS
from ha_mcp.client import websocket_listener
from ha_mcp.client.websocket_client import websocket_manager

from ._graceful_shutdown_core import EntryPoints, InProcessServer, StartedThreads
from ._graceful_shutdown_core import data_dir as data_dir
from ._graceful_shutdown_core import entry_points as entry_points
from ._graceful_shutdown_core import started_threads as started_threads

MAIN_LOGGER = "ha_mcp.__main__"


class SignalledServer(InProcessServer):
    """A server that receives ``signum`` once it runs, then serves until cancelled."""

    def __init__(self, signum: int) -> None:
        super().__init__(serve=self.serve_until_cancelled)
        self.signum = signum
        self.cancelled = False

    async def serve_until_cancelled(self) -> None:
        signal.raise_signal(self.signum)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.parametrize(
    "signum", [signal.SIGTERM, signal.SIGINT], ids=["SIGTERM", "SIGINT"]
)
def test_stop_signal_stops_the_server_and_releases_its_connections(
    signum: signal.Signals,
    entry_points: EntryPoints,
    started_threads: StartedThreads,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SIGTERM (a client or Docker stopping it) and SIGINT (Ctrl-C) shut down cleanly.

    The server is cancelled, the WebSocket listener and connections and the
    server's own client are closed, and the process exits 0 with no error
    logged (asyncio's own Ctrl-C handling would log a hard stop).
    """
    stop_listener = AsyncMock(wraps=websocket_listener.stop_websocket_listener)
    monkeypatch.setattr(websocket_listener, "stop_websocket_listener", stop_listener)
    disconnect = AsyncMock(wraps=websocket_manager.disconnect)
    monkeypatch.setattr(websocket_manager, "disconnect", disconnect)
    server = SignalledServer(signum)

    entry_points.stdio(server)

    assert server.cancelled
    stop_listener.assert_awaited_once()
    disconnect.assert_awaited_once()
    assert server.closes == 1
    assert entry_points.exits == [0]
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_second_stop_signal_during_shutdown_exits_at_once_with_failure(
    entry_points: EntryPoints, started_threads: StartedThreads
) -> None:
    """Ctrl-C pressed again while shutdown is under way exits with code 1."""

    async def serve() -> None:
        signal.raise_signal(signal.SIGINT)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            signal.raise_signal(signal.SIGINT)
            raise

    entry_points.stdio(InProcessServer(serve))

    assert entry_points.exits == [1]


def test_stop_signal_after_the_server_loop_ended_still_exits_cleanly(
    entry_points: EntryPoints, started_threads: StartedThreads
) -> None:
    """A SIGTERM landing during the final exit, when no loop runs, exits 0."""
    entry_points.after_exit = lambda: signal.raise_signal(signal.SIGTERM)

    with pytest.raises(SystemExit) as exited:
        entry_points.stdio(InProcessServer())

    assert exited.value.code == 0
    assert entry_points.exits == [0]


def test_stdio_stop_signal_arms_a_watchdog_that_force_exits_a_stuck_shutdown(
    entry_points: EntryPoints,
    started_threads: StartedThreads,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stdio server stuck in shutdown (issue #2027) is still made to exit.

    The watchdog may not cut a graceful shutdown short: it outlasts the three
    graceful phases (server stop, cleanup, task cancellation).
    """
    entry_points.stdio(SignalledServer(signal.SIGTERM))
    [watchdog] = started_threads.watchdogs()
    assert watchdog.daemon
    assert watchdog.interval > 3 * SHUTDOWN_TIMEOUT_SECONDS

    watchdog.cancel()
    with caplog.at_level(logging.WARNING, logger=MAIN_LOGGER):
        watchdog.function(*watchdog.args, **watchdog.kwargs)
        started_threads.finish()

    assert entry_points.exits == [0, 0]
    assert "Graceful shutdown did not complete" in caplog.text


def test_http_stop_signal_shuts_down_without_a_watchdog(
    entry_points: EntryPoints, started_threads: StartedThreads
) -> None:
    """``ha-mcp-web`` stops gracefully on SIGTERM and arms no force-exit timer."""
    server = SignalledServer(signal.SIGTERM)

    with pytest.raises(SystemExit) as exited:
        entry_points.web(server)

    assert exited.value.code == 0
    assert server.cancelled
    assert started_threads.watchdogs() == []


def test_failing_cleanup_steps_are_logged_and_the_rest_still_run(
    entry_points: EntryPoints,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A cleanup step that fails does not skip the next ones or fail the exit."""
    monkeypatch.setattr(
        websocket_listener,
        "stop_websocket_listener",
        AsyncMock(side_effect=OSError("listener socket gone")),
    )
    monkeypatch.setattr(
        websocket_manager,
        "disconnect",
        AsyncMock(side_effect=ConnectionResetError("peer reset")),
    )

    async def close() -> None:
        raise OSError("http pool close failed")

    server = InProcessServer(on_close=close)

    with caplog.at_level(logging.WARNING, logger=MAIN_LOGGER):
        entry_points.stdio(server)

    assert server.closes == 1
    assert entry_points.exits == [0]
    for failure in ("listener socket gone", "peer reset", "http pool close failed"):
        assert failure in caplog.text


def test_cleanup_failure_escaping_its_guards_is_logged(
    entry_points: EntryPoints, caplog: pytest.LogCaptureFixture
) -> None:
    """A close that raises past ``except Exception`` is still reported.

    Shutdown never awaits the cleanup task's result, so without its done
    callback this failure would surface only as a GC-time "never retrieved".
    """

    async def close() -> None:
        raise BaseExceptionGroup("close aborted", [asyncio.CancelledError()])

    with caplog.at_level(logging.WARNING, logger=MAIN_LOGGER):
        entry_points.stdio(InProcessServer(on_close=close))

    assert "Resource cleanup raised" in caplog.text
    assert "close aborted" in caplog.text
    assert entry_points.exits == [0]


def test_cleanup_that_swallows_cancellation_cannot_hold_shutdown(
    entry_points: EntryPoints,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A hung close that eats one cancellation, as the real stack does, is abandoned.

    Shutdown cancels it at the timeout and moves on (issue #2127); the runner's
    task sweep ends it. Awaiting it instead (``wait_for``) would wait out the
    swallowed cancellation: the 5 s cap then ends the close without the
    timeout ever being logged.
    """
    monkeypatch.setattr("ha_mcp.__main__.SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    cancellations = 0

    async def close() -> None:
        nonlocal cancellations
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        cancellations += 1
        try:
            async with asyncio.timeout(5):
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations += 1
            raise

    with caplog.at_level(logging.WARNING, logger=MAIN_LOGGER):
        entry_points.stdio(InProcessServer(on_close=close))

    assert "Resource cleanup timed out" in caplog.text
    assert cancellations == 2
    assert entry_points.exits == [0]


@pytest.mark.parametrize(
    ("stop", "code"),
    [
        (SystemExit(3), 3),
        (SystemExit("config error"), 1),
        # A cancel with no stop signal is a hard stop (#1544).
        (asyncio.CancelledError(), 1),
    ],
    ids=["exit-code", "exit-message", "unrequested-cancel"],
)
def test_server_ending_abnormally_exits_with_a_failure_code(
    stop: BaseException, code: int, entry_points: EntryPoints
) -> None:
    """A server that stops abnormally never ends the process with exit 0."""

    async def serve() -> None:
        raise stop

    entry_points.stdio(InProcessServer(serve))

    assert entry_points.exits == [code]


def _stdin(kind: str, stack: contextlib.ExitStack) -> IO[str] | None:
    if kind == "dev-null":
        return stack.enter_context(open(os.devnull))
    if kind == "in-memory":
        return io.StringIO()
    return None


@pytest.mark.parametrize("kind", ["dev-null", "in-memory", "none"])
def test_stdio_refuses_to_start_without_a_readable_stdin(
    kind: str, entry_points: EntryPoints, capsys: pytest.CaptureFixture[str]
) -> None:
    """``docker run`` without ``-i`` (stdin on /dev/null), a stdin with no
    descriptor, or none at all: exit 1 with the fix, instead of serving EOF."""
    server = InProcessServer()

    with contextlib.ExitStack() as stack, pytest.raises(SystemExit) as exited:
        entry_points.stdin = _stdin(kind, stack)
        entry_points.stdio(server)

    assert exited.value.code == 1
    assert "-i flag" in capsys.readouterr().err
    assert server.started_with == []


def test_stdio_starts_on_a_terminal(entry_points: EntryPoints) -> None:
    """A terminal is a readable stdin, like the pipe every other test runs on."""
    server = InProcessServer()
    primary, secondary = os.openpty()
    try:
        with os.fdopen(secondary) as terminal:
            entry_points.stdin = terminal
            entry_points.stdio(server)
    finally:
        os.close(primary)

    assert len(server.started_with) == 1
    assert entry_points.exits == [0]


def test_smoke_test_flag_exits_with_its_result_without_serving(
    entry_points: EntryPoints, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ha-mcp --smoke-test`` reports the smoke test's result and starts nothing."""
    monkeypatch.setattr("ha_mcp.smoke_test.main", lambda: 3)
    server = InProcessServer()

    with pytest.raises(SystemExit) as exited:
        entry_points.stdio(server, "--smoke-test")

    assert exited.value.code == 3
    assert server.started_with == []


def test_ha_mcp_web_defaults_to_all_interfaces_port_8086_and_mcp_path(
    entry_points: EntryPoints,
) -> None:
    """With no MCP_* variables the documented defaults apply.

    0.0.0.0 keeps LAN clients working (SECURITY.md, "Local network is the
    trusted zone"); FastMCP's own default would be 127.0.0.1.
    """
    server = InProcessServer()

    with pytest.raises(SystemExit):
        entry_points.web(server)

    [started] = server.started_with
    assert (started["host"], started["port"], started["path"]) == (
        "0.0.0.0",
        8086,
        "/mcp",
    )


def test_ha_mcp_web_refuses_a_non_numeric_port(
    entry_points: EntryPoints, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mistyped MCP_PORT exits 1 instead of starting on some other port."""
    monkeypatch.setenv("MCP_PORT", "not-a-number")
    server = InProcessServer()

    with pytest.raises(SystemExit) as exited:
        entry_points.web(server)

    assert exited.value.code == 1
    assert server.started_with == []
