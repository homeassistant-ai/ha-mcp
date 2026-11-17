"""Run ha-mcp's entry points to their end in a unit test, signals included.

Nothing here stands in for Home Assistant. The in-process server is this
repository's own, and only its transport is replaced: ``serve`` runs where it
would read stdin or bind a port. Signal registration, the real signal handler,
the shutdown sequence, cleanup and the exit code are the entry point's own; the
final ``os._exit`` is recorded instead of taken.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import threading
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

import pytest

from ha_mcp.__main__ import main, main_web
from ha_mcp._vendor.fastmcp import FastMCP

from ._stdio_sidecar_core import data_dir as data_dir


async def returns() -> None:
    """A server that ends on its own, as one does when its client goes away."""


@dataclass
class InProcessServer:
    """The server an entry point builds, reduced to what shutdown touches."""

    serve: Callable[[], Awaitable[None]] = returns
    on_close: Callable[[], Awaitable[None]] | None = None
    mcp: FastMCP = field(default_factory=lambda: FastMCP("test"))
    started_with: list[dict[str, Any]] = field(default_factory=list)
    closes: int = 0

    async def run_async(self, **kwargs: Any) -> None:
        self.started_with.append(kwargs)
        await self.serve()

    async def close(self) -> None:
        self.closes += 1
        if self.on_close is not None:
            await self.on_close()


@contextlib.contextmanager
def signal_handlers_restored() -> Iterator[None]:
    """Put pytest's SIGTERM / SIGINT handlers back after an entry point set its own."""
    saved = {
        signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        yield
    finally:
        for signum, handler in saved.items():
            signal.signal(signum, handler)


@dataclass
class EntryPoints:
    """Runs ``ha-mcp`` (stdio) and ``ha-mcp-web`` in this process.

    ``stdin`` is what the stdio entry point finds on fd 0 (a pipe, as an MCP
    client connects it, unless a test sets another); ``exits`` the codes it
    force-exits with, after which ``after_exit`` runs if set.
    """

    monkeypatch: pytest.MonkeyPatch
    stdin: IO[str] | None
    exits: list[int] = field(default_factory=list)
    after_exit: Callable[[], None] | None = None

    def force_exit(self, code: int) -> None:
        self.exits.append(code)
        if self.after_exit is not None:
            self.after_exit()

    def install(self, server: InProcessServer) -> None:
        patch = self.monkeypatch
        patch.setattr(server.mcp, "run_async", server.run_async)
        patch.setattr("ha_mcp.__main__._server", server)
        patch.setattr("ha_mcp.__main__._setup_logging", lambda *a, **kw: None)
        # Process-wide state a run leaves behind, put back after the test.
        patch.setattr("ha_mcp.__main__._shutdown_event", None)
        patch.setattr("ha_mcp.__main__._shutdown_in_progress", False)
        patch.setattr("ha_mcp.__main__._force_exit_on_shutdown", False)
        access_log = logging.getLogger("uvicorn.access")
        patch.setattr(access_log, "filters", list(access_log.filters))

    def stdio(self, server: InProcessServer, *args: str) -> None:
        """Run ``ha-mcp *args`` serving ``server``."""
        self.install(server)
        self.monkeypatch.setattr(sys, "argv", ["ha-mcp", *args])
        self.monkeypatch.setattr(sys, "stdin", self.stdin)
        self.monkeypatch.setattr("ha_mcp.__main__._force_exit", self.force_exit)
        with signal_handlers_restored():
            main()

    def web(self, server: InProcessServer) -> None:
        """Run ``ha-mcp-web`` serving ``server``."""
        self.install(server)
        with signal_handlers_restored():
            main_web()


@pytest.fixture
def entry_points(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[EntryPoints]:
    """The entry points, configured for a standard install with the settings UI off."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://homeassistant.invalid:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
    for name in ("MCP_HOST", "MCP_PORT", "MCP_SECRET_PATH"):
        monkeypatch.delenv(name, raising=False)
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd) as stdin:
        try:
            yield EntryPoints(monkeypatch, stdin)
        finally:
            os.close(write_fd)


class StartedThreads:
    """The threads started since the test began."""

    def __init__(self) -> None:
        self.before = set(threading.enumerate())

    def started(self) -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t not in self.before]

    def watchdogs(self) -> list[threading.Timer]:
        """The timers armed and not yet fired or cancelled."""
        return [t for t in self.started() if isinstance(t, threading.Timer)]

    def finish(self) -> None:
        """Cancel the armed timers and wait for every started thread to end."""
        for thread in self.started():
            if isinstance(thread, threading.Timer):
                thread.cancel()
            thread.join(timeout=5)


@pytest.fixture
def started_threads() -> Iterator[StartedThreads]:
    """The threads a test starts, ended before Core checks none is left."""
    threads = StartedThreads()
    try:
        yield threads
    finally:
        threads.finish()
