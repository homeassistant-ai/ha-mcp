"""Run the app's ``start.py`` against the test's filesystem, Supervisor and Core.

``start.py`` is the app's own process. It reaches three things outside it:

- the filesystem under ``/config``, ``/data`` and ``/opt``, redirected under
  the test's ``fs_root`` (the same ``/config`` the component reads);
- the Supervisor, which has no test double, so its answers are typed in
  (``Supervisor``);
- Home Assistant Core's REST API, reached through the Supervisor's
  ``/core/api`` proxy. ``connect_core`` routes those requests to the real
  ``hass`` over real HTTP, so every Core reply is Core's own.

``main()`` runs in an executor thread while Core serves the test's event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import ClientSessionGenerator, WebSocketGenerator

from ._proxy import state_path
from ._variants import REPO_ROOT, Variant, load_start

START_ROOTS = ("/config", "/data", "/opt")
CORE_API = "http://supervisor/core/api"
# The two ha-mcp server apps the proxy discovers, stable first.
SERVER_APP_DIRS = (
    REPO_ROOT / "homeassistant-addon",
    REPO_ROOT / "homeassistant-addon-dev",
)


def redirect(fs_root: Path, path: str | PurePath) -> Path:
    """Where ``start.py`` finds an absolute path in this test's world."""
    text = str(path)
    if any(text == root or text.startswith(f"{root}/") for root in START_ROOTS):
        return state_path(fs_root, text)
    return Path(text)


@dataclass
class Supervisor:
    """The Supervisor's answers, typed in: the Supervisor is not Core and has
    no test double. An unknown path is a request the Supervisor refuses."""

    addons: list[dict[str, Any]] = field(default_factory=list)
    info: dict[str, dict[str, Any]] = field(default_factory=dict)
    logs: dict[str, str] = field(default_factory=dict)
    reachable: bool = True
    lists_addons: bool = True
    accepts_options: bool = True
    gets: list[str] = field(default_factory=list)
    posts: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def get(self, path: str) -> dict[str, Any] | None:
        self.gets.append(path)
        if not self.reachable:
            return None
        if path == "/addons":
            return {"addons": self.addons} if self.lists_addons else None
        slug = path.removeprefix("/addons/").removesuffix("/info")
        return self.info.get(slug) if path.endswith("/info") else None

    def get_text(self, path: str) -> str | None:
        self.gets.append(path)
        return self.logs.get(path)

    def post(self, path: str, data: dict[str, Any]) -> bool:
        self.posts.append((path, data))
        return self.accepts_options


@dataclass
class Signals:
    """``start.py``'s ``signal`` module: a handler is recorded, not installed.

    Python installs signal handlers only from the main thread, and ``main()``
    runs in an executor thread here.
    """

    handlers: dict[int, Any] = field(default_factory=dict)

    def install(self, signum: int, handler: Any) -> None:
        self.handlers[signum] = handler

    def module(self) -> SimpleNamespace:
        return SimpleNamespace(
            SIGTERM=signal.SIGTERM,
            SIGINT=signal.SIGINT,
            SIG_DFL=signal.SIG_DFL,
            Signals=signal.Signals,
            signal=self.install,
        )


@dataclass
class Clock:
    """``start.py``'s ``time`` module. A sleep returns at once and is recorded.

    Once ``main()`` has installed its shutdown handlers, a sleep is the
    keep-alive loop's. Each of the first ``len(polls)`` such sleeps runs the
    next of ``polls`` (what happens between two keep-alive iterations) and
    lets the loop go round again. The one after runs ``while_running`` (the
    test's look at the running app), then delivers ``stop_signal``: SIGTERM is
    what a Supervisor stop sends. Before the handlers are installed,
    ``during_wait`` stands in for what happens while a retry waits.
    """

    signals: Signals
    sleeps: list[float] = field(default_factory=list)
    polls: list[Callable[[], None]] = field(default_factory=list)
    while_running: Callable[[], None] | None = None
    during_wait: Callable[[float], None] | None = None
    stop_signal: signal.Signals = signal.SIGTERM

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        handler = self.signals.handlers.get(self.stop_signal)
        if not callable(handler):
            if self.during_wait is not None:
                self.during_wait(seconds)
            return
        if self.polls:
            self.polls.pop(0)()
            return
        look, self.while_running = self.while_running, None
        if look is not None:
            look()
        handler(self.stop_signal, None)

    def module(self) -> SimpleNamespace:
        return SimpleNamespace(sleep=self.sleep, monotonic=time.monotonic)


@dataclass
class App:
    """The app's process, set up in the test's world."""

    start: ModuleType
    variant: Variant
    fs_root: Path
    supervisor: Supervisor
    signals: Signals
    clock: Clock
    restarts_awaited: int = 0
    on_restart: Callable[[], None] | None = None
    # (level, message) of every line start.py logged. start.py prints from
    # main()'s thread while Core's runs, and pytest's capture buffer is not
    # thread-safe: it drops lines, so tests read this record instead.
    logged: list[tuple[str, str]] = field(default_factory=list)

    def path(self, absolute: str) -> Path:
        return redirect(self.fs_root, absolute)

    def info_lines(self) -> list[str]:
        """What start.py logged at INFO, in order."""
        return [message for level, message in self.logged if level == "INFO"]

    def out(self) -> str:
        """start.py's INFO log, as its stdout reads."""
        return "\n".join(self.info_lines())

    def err(self) -> str:
        """start.py's WARNING and ERROR log, as its stderr reads."""
        return "\n".join(message for level, message in self.logged if level != "INFO")

    def set_options(self, **options: Any) -> None:
        """The add-on options the Supervisor writes to /data/options.json."""
        self.path("/data/options.json").write_text(json.dumps(options))

    def proxy_config(self) -> dict[str, Any]:
        config: dict[str, Any] = json.loads(
            self.path(self.variant.config_file).read_text()
        )
        return config

    @property
    def deployed_component(self) -> Path:
        return self.path(f"/config/custom_components/{self.variant.component}")

    def deploy_component(self, version: str | None = None) -> None:
        """Put the component in /config the way an earlier start installed it,
        optionally with another version in its manifest."""
        shutil.copytree(self.variant.component_dir, self.deployed_component)
        if version is not None:
            manifest_file = self.deployed_component / "manifest.json"
            manifest = json.loads(manifest_file.read_text())
            manifest["version"] = version
            manifest_file.write_text(json.dumps(manifest))

    def wait_for_ha_restart(self) -> None:
        """Stands in for the user restarting Home Assistant, which a unit
        test cannot do."""
        self.restarts_awaited += 1
        if self.on_restart is not None:
            self.on_restart()


def _recording(
    app: App, level: str, log: Callable[[str], None]
) -> Callable[[str], None]:
    def record(message: str) -> None:
        app.logged.append((level, message))
        log(message)

    return record


def make_app(
    variant: Variant,
    fs_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ships_component: bool = True,
    installed: bool = False,
) -> App:
    """Load the flavor's ``start.py`` into the test's world.

    ``ships_component`` puts the image's ``/opt/<component>`` source in place
    (the Dockerfile copies it there). ``installed`` puts the current component
    in ``/config`` as an earlier start left it, so ``main()`` takes the
    ordinary restart path rather than the first install's.
    """
    start = load_start(variant)
    signals = Signals()
    app = App(
        start=start,
        variant=variant,
        fs_root=fs_root,
        supervisor=Supervisor(),
        signals=signals,
        clock=Clock(signals),
    )
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(start, "Path", lambda path: redirect(fs_root, path))
    for name, value in list(vars(start).items()):
        if isinstance(value, Path) and str(value).startswith(START_ROOTS):
            monkeypatch.setattr(start, name, redirect(fs_root, value))
    monkeypatch.setattr(start, "_supervisor_get", app.supervisor.get)
    monkeypatch.setattr(start, "_supervisor_get_text", app.supervisor.get_text)
    monkeypatch.setattr(start, "_supervisor_post", app.supervisor.post)
    monkeypatch.setattr(start, "_wait_for_ha_restart", app.wait_for_ha_restart)
    monkeypatch.setattr(start, "time", app.clock.module())
    monkeypatch.setattr(start, "signal", signals.module())
    for level, name in (
        ("INFO", "log_info"),
        ("WARNING", "log_warning"),
        ("ERROR", "log_error"),
    ):
        monkeypatch.setattr(start, name, _recording(app, level, getattr(start, name)))
    if ships_component:
        source = app.path(f"/opt/{variant.component}")
        source.parent.mkdir(parents=True, exist_ok=True)
        source.symlink_to(variant.component_dir, target_is_directory=True)
    if installed:
        app.deploy_component()
    return app


@dataclass
class CoreLink:
    """The real ``hass`` HTTP server ``start.py`` talks to.

    ``requests`` records each Core API request ``start.py`` makes, as
    ``(method, path)`` with the path below ``/api``. ``on_request`` runs in
    ``main()``'s thread just before Core receives one: what else happens at
    that moment.
    """

    base_url: str
    requests: list[tuple[str, str]] = field(default_factory=list)
    on_request: Callable[[str, str], None] | None = None

    def send(self, request: urllib.request.Request, timeout: float) -> Any:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(request, timeout=timeout)

    def status(self, request: urllib.request.Request) -> int:
        try:
            with self.send(request, timeout=30) as response:
                status: int = response.status
                return status
        except urllib.error.HTTPError as err:
            return err.code

    def get(self, path: str) -> int:
        """GET from Core as an anonymous remote client."""
        return self.status(urllib.request.Request(f"{self.base_url}{path}"))

    def get_json(self, path: str) -> tuple[int, Any]:
        """GET a JSON document from Core as an anonymous remote client."""
        try:
            with self.send(
                urllib.request.Request(f"{self.base_url}{path}"), timeout=30
            ) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, None

    def post(self, path: str, body: bytes) -> int:
        """POST to Core as a remote client would (no Home Assistant login)."""
        return self.status(
            urllib.request.Request(
                f"{self.base_url}{path}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
        )


async def connect_core(
    app: App,
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    hass_access_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> CoreLink:
    """Route ``start.py``'s Core API requests to the real ``hass``.

    The Supervisor proxies ``/core/api`` to Core's REST API with the app's
    Supervisor token. Here the token is an admin's, and a request goes to
    Core's own HTTP server on loopback, so ``_ha_core_api`` runs unchanged and
    every reply is Core's.
    """
    for domain in ("websocket_api", "api", "config", "persistent_notification"):
        assert await async_setup_component(hass, domain, {})
    # In production HomeAssistantHTTP.start() stops aiohttp freezing the
    # router, so an integration set up after boot (as the app's config flow
    # does) can still bind views. The test server does not run start().
    monkeypatch.setattr(hass.http.app.router, "freeze", lambda: None)
    server = await hass_client()
    link = CoreLink(base_url=str(server.make_url("")).rstrip("/"))

    def urlopen(request: urllib.request.Request, timeout: float) -> Any:
        url = request.full_url
        assert url.startswith(CORE_API), f"unexpected Supervisor request {url}"
        path = url.removeprefix(CORE_API)
        link.requests.append((request.get_method(), path))
        if link.on_request is not None:
            link.on_request(request.get_method(), path)
        routed = urllib.request.Request(
            f"{link.base_url}/api{path}",
            data=request.data,
            headers=dict(request.header_items()),
            method=request.get_method(),
        )
        return link.send(routed, timeout)

    monkeypatch.setattr(
        app.start,
        "urllib",
        SimpleNamespace(
            request=SimpleNamespace(Request=urllib.request.Request, urlopen=urlopen),
            error=urllib.error,
        ),
    )
    monkeypatch.setenv("SUPERVISOR_TOKEN", hass_access_token)
    # Core scans custom_components again, so it finds the proxy component.
    hass.data.pop(loader.DATA_CUSTOM_COMPONENTS, None)
    return link


async def run_main(hass: HomeAssistant, app: App) -> int:
    """Run the app's ``main()`` to its exit, as the Supervisor starts it."""
    rc: int = await hass.async_add_executor_job(app.start.main)
    return rc


def await_in_core[T](hass: HomeAssistant, awaitable: Coroutine[Any, Any, T]) -> T:
    """Await ``awaitable`` on Core's event loop from ``main()``'s thread: Core's
    state is only consistent on its own loop."""
    return asyncio.run_coroutine_threadsafe(awaitable, hass.loop).result(timeout=30)


def in_core[T](hass: HomeAssistant, look: Callable[[], T]) -> T:
    """Run ``look`` on Core's event loop from ``main()``'s thread."""

    async def on_loop() -> T:
        return look()

    return await_in_core(hass, on_loop())


async def notifications(hass_ws_client: WebSocketGenerator) -> dict[str, Any]:
    """Core's persistent notifications, keyed by notification id."""
    client = await hass_ws_client()
    await client.send_json_auto_id({"type": "persistent_notification/get"})
    reply = await client.receive_json()
    assert reply["success"], reply
    return {item["notification_id"]: item for item in reply["result"]}


def entry_states(hass: HomeAssistant, variant: Variant) -> list[str]:
    return [
        entry.state.value for entry in hass.config_entries.async_entries(variant.domain)
    ]


def file_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@contextlib.contextmanager
def no_restricted_create_under(root: Path) -> Iterator[None]:
    """Fail ``os.open`` (the 0600 create) for paths under ``root`` only, as on
    a filesystem that cannot honour restricted modes.

    ``os.open`` is process-wide; failing it everywhere would also break
    whatever else calls it meanwhile (a weakref finalizer during garbage
    collection, another xdist test's files).
    """
    real_open = os.open

    def guarded_open(path: Any, *args: Any, **kwargs: Any) -> int:
        try:
            candidate = Path(os.fsdecode(path))
        except TypeError, ValueError:
            return real_open(path, *args, **kwargs)
        if candidate == root or candidate.is_relative_to(root):
            raise OSError("no mode bits")
        return real_open(path, *args, **kwargs)

    with patch.object(os, "open", side_effect=guarded_open):
        yield


def server_app_slugs() -> list[str]:
    """The ha-mcp server apps' slugs, stable first, from their config.yaml."""
    return [
        str(yaml.safe_load((d / "config.yaml").read_text())["slug"])
        for d in SERVER_APP_DIRS
    ]
