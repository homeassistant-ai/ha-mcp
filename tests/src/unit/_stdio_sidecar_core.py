"""The world of the stdio settings sidecar in a unit test.

The sidecar is this repository's own process; nothing here stands in for Home
Assistant. A test gets an empty data dir, the sidecar a previous startup left
running (a loopback listener that answers the retire POST), the child
``maybe_spawn`` starts (``subprocess.Popen`` replaced, no process starts) and
the app ``run_main`` serves (uvicorn's ``Server.run`` replaced, nothing serves).
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp.config import reset_global_settings
from ha_mcp.utils.data_paths import get_data_dir

DISABLE_ENV = "HA_MCP_DISABLE_SETTINGS_UI"
SENTINEL = "settings_ui_disabled"
# Where a freshly spawned child publishes itself; any loopback port will do.
NEW_URL = "http://127.0.0.1:54321/private_new/settings"

_REAL_POPEN = subprocess.Popen


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """ha-mcp's data dir, empty, with the settings UI enabled and no pinned port."""
    monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
    for name in (DISABLE_ENV, "HA_MCP_SIDECAR_PORT", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    get_data_dir.cache_clear()
    reset_global_settings()
    try:
        yield tmp_path
    finally:
        get_data_dir.cache_clear()
        reset_global_settings()


@dataclass
class OldSidecar:
    """The sidecar a previous stdio startup left running, on a loopback port.

    Every request is recorded as ``(method, path)``. A POST runs ``on_post``
    first, as the old process acts before it answers, then gets ``status``,
    ``headers`` and ``body``.
    """

    port: int = 0
    requests: list[tuple[str, str]] = field(default_factory=list)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(
        default_factory=lambda: {"success": True, "sentinel_created": False}
    )
    on_post: Callable[[], object] | None = None
    released: threading.Event = field(default_factory=threading.Event)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/private_old/settings"


def _handler(old: OldSidecar) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            old.requests.append(("GET", self.path))
            self.send_error(404)

        def do_POST(self) -> None:
            old.requests.append(("POST", self.path))
            if old.on_post is not None:
                old.on_post()
            payload = json.dumps(old.body).encode()
            self.send_response(old.status)
            for name, value in old.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


@pytest.fixture
def old_sidecar(data_dir: Path, socket_enabled: None) -> Iterator[OldSidecar]:
    """A running old sidecar whose URL is recorded in ``ui.url``."""
    old = OldSidecar()
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _handler(old))
    old.port = httpd.server_address[1]
    (data_dir / "ui.url").write_text(old.url + "\n")
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, name="old-sidecar"
    )
    thread.start()
    try:
        yield old
    finally:
        old.released.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@dataclass
class Child:
    """The process ``maybe_spawn`` started, as ``Popen`` hands it back."""

    pid: int
    returncode: int | None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self) -> int | None:
        return self.returncode


@dataclass
class Spawn:
    """One child start: its command and the data-dir files present then."""

    cmd: list[str]
    files: set[str]


class Spawner:
    """``subprocess.Popen`` for ``maybe_spawn``: records the start, plays the child.

    The child publishes ``NEW_URL`` at once unless ``publishes`` is False;
    ``exit_code`` is what it has exited with (None: still running).
    ``on_spawn`` runs at the moment the child is started.
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.spawns: list[Spawn] = []
        self.publishes = True
        self.exit_code: int | None = None
        self.on_spawn: Callable[[], None] | None = None

    def __call__(self, cmd: list[str], **kwargs: Any) -> Child:
        self.spawns.append(
            Spawn(list(cmd), {path.name for path in self.data_dir.iterdir()})
        )
        if self.on_spawn is not None:
            self.on_spawn()
        log = kwargs.get("stdout")
        if isinstance(log, io.IOBase):
            log.close()  # the child's copy of sidecar.log
        if self.publishes:
            (self.data_dir / "ui.url").write_text(NEW_URL + "\n")
        return Child(pid=4242, returncode=self.exit_code)


@pytest.fixture
def spawned(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> Spawner:
    """Every child ``maybe_spawn`` starts, without starting a process."""
    spawner = Spawner(data_dir)
    monkeypatch.setattr(subprocess, "Popen", spawner)
    return spawner


@contextlib.contextmanager
def another_parent_spawning(data_dir: Path) -> Iterator[None]:
    """Hold the spawn lock as a second stdio parent in mid-spawn does."""
    fd = os.open(data_dir / "spawn.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def live_process() -> Iterator[subprocess.Popen[bytes]]:
    """A real process that runs until the test ends it."""
    proc = _REAL_POPEN([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def exits_on_first_poll(
    monkeypatch: pytest.MonkeyPatch, proc: subprocess.Popen[bytes]
) -> list[float]:
    """``proc`` exits (and is reaped) the first time the caller sleeps to poll it.

    Returns the sleeps, so a test sees whether the caller waited at all.
    """
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    monkeypatch.setattr(time, "sleep", sleep)
    return sleeps


@dataclass
class Serving:
    """What ``run_main`` set up by the time it handed its socket to uvicorn."""

    app: Starlette
    server: uvicorn.Server
    url: str
    sockets: list[socket.socket]

    @property
    def host(self) -> str:
        """The authority of the published URL, as a browser sends it in Host."""
        return urlsplit(self.url).netloc

    @property
    def prefix(self) -> str:
        """The secret path every route sits under."""
        return urlsplit(self.url).path.removesuffix("/settings")

    def client(self) -> TestClient:
        return TestClient(self.app, base_url=f"http://{self.host}")


def run_sidecar(
    monkeypatch: pytest.MonkeyPatch,
    on_serve: Callable[[Serving], None] = lambda _serving: None,
) -> int:
    """Run ``run_main`` up to where uvicorn would serve; ``on_serve`` sees it then.

    The caller takes ``data_dir`` and ``socket_enabled``: the listener is bound
    for real on 127.0.0.1.
    """

    def run(server: uvicorn.Server, sockets: list[socket.socket] | None = None) -> None:
        handed = list(sockets or [])
        try:
            app = server.config.app
            assert isinstance(app, Starlette)
            url = sidecar.read_sidecar_url()
            assert url is not None, "the sidecar serves before publishing its URL"
            on_serve(Serving(app=app, server=server, url=url, sockets=handed))
        finally:
            for sock in handed:
                sock.close()

    monkeypatch.setattr(uvicorn.Server, "run", run)
    quiet_uvicorn_logging(monkeypatch)
    return sidecar.run_main()


def quiet_uvicorn_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep uvicorn's process-wide logging setup, which would outlive the test, off."""
    monkeypatch.setattr(uvicorn.Config, "configure_logging", lambda _config: None)


@pytest.fixture
def served(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, socket_enabled: None
) -> Serving:
    """The app a freshly started sidecar serves, with the URL it published."""
    seen: list[Serving] = []
    assert run_sidecar(monkeypatch, seen.append) == 0
    return seen[0]


def run_stdio_main(monkeypatch: pytest.MonkeyPatch, mcp: FastMCP) -> None:
    """Run the stdio entry point ``main`` up to where it would serve on stdin.

    ``mcp`` is the in-process server's FastMCP, so the tool-metadata dump lists
    its tools. The stdin loop is not run, logging is left as pytest set it up,
    and the process is not force-exited.
    """
    monkeypatch.setattr(sys, "argv", ["ha-mcp"])
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://homeassistant.invalid:8123")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "test-token")
    monkeypatch.setattr("ha_mcp.__main__._get_server", lambda: SimpleNamespace(mcp=mcp))
    monkeypatch.setattr("ha_mcp.__main__._check_stdin_available", lambda: True)
    monkeypatch.setattr("ha_mcp.__main__._setup_logging", lambda *a, **kw: None)
    monkeypatch.setattr(
        "ha_mcp.__main__._run_entrypoint", lambda coro, label: coro.close()
    )
    exits: list[int] = []
    monkeypatch.setattr("ha_mcp.__main__._force_exit", exits.append)
    monkeypatch.setattr("ha_mcp.__main__._force_exit_on_shutdown", False)

    from ha_mcp.__main__ import main

    main()
    assert exits == [0]
