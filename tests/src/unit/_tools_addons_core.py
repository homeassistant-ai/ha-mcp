"""A Supervisor and a target app for the app (add-on) tools, over real HTTP.

A unit test has no Supervisor. ``Supervisor`` answers its REST API the way
Supervisor does (``{"result": "ok", "data": ...}``), mints ingress sessions and
serves ``/ingress/<token>/...`` for the target app only to a request carrying
one. ``TargetApp`` is the app's own web server, over HTTP and WebSocket, at the
container address Supervisor reports for it (``127.0.0.1``).

Outside the app the server's client reaches Supervisor through Core: Core's own
``hassio`` integration, set up against the stand-in (``_hassio_core``). Inside the app it asks
the stand-in directly at ``SUPERVISOR_BASE_URL``, as the app's token allows.
``_supervisor_logs_core.route`` runs a test on ``app_route`` and ``core_route``
in turn.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_socket
from aiohasupervisor.models import (
    AddonBoot,
    AddonStage,
    AddonStartup,
    AddonState,
    InstalledAddonComplete,
)
from aiohasupervisor.models.addons import (
    AddonBootConfig,
    AddonsList,
    AppArmor,
    CpuArch,
    InstalledAddon,
    SupervisorRole,
)
from aiohasupervisor.models.ingress import Session
from aiohttp import WSMsgType, web
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.async_ import get_scheduled_timer_handles
from pytest_homeassistant.common import async_fire_time_changed

from ha_mcp import redaction
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.supervisor_client import make_supervisor_httpx_client
from ha_mcp.tools import tools_addons
from ha_mcp.tools.tools_addons import AddOnTools

from ._hassio_core import set_up_hassio, supervisor_error, supervisor_ok

SLUG = "local_target"
# The token in the app's ingress URL, as Supervisor names it.
INGRESS_TOKEN = "ingress-token-1"
APP_TOKEN = "app-supervisor-token"
# The bound the tools document for Supervisor's text and an app's response.
TEXT_LIMIT = 50 * 1024
TRUNCATED = "[Supervisor response truncated"

# ha_manage_app's defaults: every call names the few it changes.
MANAGE_DEFAULTS: dict[str, Any] = {
    "slug": "",
    "path": None,
    "method": "GET",
    "body": None,
    "debug": False,
    "port": None,
    "offset": 0,
    "limit": None,
    "websocket": False,
    "wait_for_close": True,
    "message_limit": None,
    "message_offset": 0,
    "summarize": True,
    "python_transform": None,
    "options": None,
    "network": None,
    "boot": None,
    "auto_update": None,
    "watchdog": None,
    "array_patch": None,
    "request_headers": None,
    "action": None,
    "repository": None,
}


@dataclass(frozen=True)
class Seen:
    """One request that reached Supervisor or the app."""

    method: str
    path: str
    headers: dict[str, str]
    body: Any


async def _seen(request: web.Request, path: str) -> Seen:
    text = await request.text() if request.can_read_body else ""
    try:
        body: Any = json.loads(text) if text else None
    except ValueError:
        body = text
    return Seen(request.method, path, dict(request.headers), body)


async def _forever() -> None:
    await asyncio.Event().wait()


@dataclass
class TargetApp:
    """The target app's web server: what it answers and what reached it."""

    port: int = 0
    status: int = 200
    body: Any = field(default_factory=lambda: {"ok": True})
    # A WebSocket peer: the frames it sends (once ``hold`` is set, if given),
    # then it closes unless told to stay open; a status refuses the handshake.
    frames: list[str | bytes] = field(default_factory=list)
    hold: asyncio.Event | None = None
    stay_open: bool = False
    ws_status: int | None = None
    # Never answer the request.
    hang: bool = False
    requests: list[Seen] = field(default_factory=list)
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def handle(self, request: web.Request, path: str) -> web.StreamResponse:
        self.requests.append(await _seen(request, path))
        self.arrived.set()
        if self.hang:
            await _forever()
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._websocket(request)
        if isinstance(self.body, str):
            return web.Response(status=self.status, text=self.body)
        return web.json_response(self.body, status=self.status)

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        if self.ws_status is not None:
            return web.Response(status=self.ws_status)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if self.hold is not None:
            await self.hold.wait()
        for frame in self.frames:
            if isinstance(frame, bytes):
                await ws.send_bytes(frame)
            else:
                await ws.send_str(frame)
        if self.stay_open:
            async for message in ws:
                if message.type is WSMsgType.CLOSE:
                    break
        await ws.close()
        return ws

    async def serve(self, request: web.Request) -> web.StreamResponse:
        return await self.handle(request, "/" + request.match_info["path"])


@dataclass(frozen=True)
class _Answer:
    status: int = 200
    payload: Any = None
    text: str | None = None
    drop: bool = False
    hang: bool = False


class Supervisor:
    """What Supervisor answers, what reached it, and the sessions it minted.

    Answers queued for one request are given in order; the last one is kept
    for every later request. A request with none queued gets its ``default``.
    """

    def __init__(self, target: TargetApp) -> None:
        self.target = target
        self.url = ""
        self.sessions: list[str] = []
        self._answers: dict[tuple[str, str], list[_Answer]] = {}
        self._defaults: dict[tuple[str, str], _Answer] = {}
        self._seen: list[Seen] = []

    @property
    def requests(self) -> list[tuple[str, str]]:
        """``(METHOD, path)`` of every REST request, in order."""
        return [(seen.method, seen.path) for seen in self._seen]

    def sent(self, method: str, path: str) -> list[Any]:
        """The bodies of the requests made to ``method path``."""
        return [s.body for s in self._seen if (s.method, s.path) == (method, path)]

    def headers(self, method: str, path: str) -> dict[str, str]:
        """The headers of the last request made to ``method path``."""
        found = [s for s in self._seen if (s.method, s.path) == (method, path)]
        return found[-1].headers

    def forget_requests(self) -> None:
        """Start ``requests`` afresh, as after Core has set ``hassio`` up."""
        self._seen.clear()

    def default_refusal(
        self, method: str, path: str, message: str, status: int = 400
    ) -> None:
        """Supervisor refuses ``method path`` with ``message`` whenever no
        answer is queued for it."""
        self._defaults[method, path] = _Answer(status, supervisor_error(message))

    def forget_defaults(self) -> None:
        """From now on answer only what is queued."""
        self._defaults.clear()

    def _queue(self, method: str, path: str, answer: _Answer) -> None:
        self._answers.setdefault((method, path), []).append(answer)

    def answer(self, method: str, path: str, data: Any = None) -> None:
        """Supervisor accepts the request and answers ``data``; with none it
        answers ``{}``, as its ``api_return_ok`` does."""
        self._queue(method, path, _Answer(payload=supervisor_ok(data)))

    def default(self, method: str, path: str, data: Any = None) -> None:
        """Supervisor answers ``data`` to ``method path`` whenever no answer
        is queued for it."""
        self._defaults[method, path] = _Answer(payload=supervisor_ok(data))

    def refuse(
        self,
        method: str,
        path: str,
        message: str,
        status: int = 400,
        error_key: str | None = None,
    ) -> None:
        """Supervisor refuses the request with its own message, and with the
        ``error_key`` its keyed errors add (``api_return_error``)."""
        payload = supervisor_error(message)
        if error_key is not None:
            payload["error_key"] = error_key
        self._queue(method, path, _Answer(status, payload))

    def reply(
        self,
        method: str,
        path: str,
        *,
        status: int = 200,
        payload: Any = None,
        text: str | None = None,
    ) -> None:
        """Supervisor answers with this exact body."""
        self._queue(method, path, _Answer(status, payload, text))

    def drop(self, method: str, path: str) -> None:
        """Supervisor reads the request and closes the connection unanswered."""
        self._queue(method, path, _Answer(drop=True))

    def hang(self, method: str, path: str) -> None:
        """Supervisor reads the request and never answers."""
        self._queue(method, path, _Answer(hang=True))

    def running(self, slug: str = SLUG, **fields: Any) -> None:
        """Answer ``slug``'s info as a running app with ingress; ``fields`` win."""
        info = app_info(slug, **{"ingress_port": self.target.port, **fields})
        self.answer("GET", f"/addons/{slug}/info", info)

    def _next_answer(self, method: str, path: str) -> _Answer | None:
        if queue := self._answers.get((method, path)):
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return self._defaults.get((method, path))

    async def serve(self, request: web.Request) -> web.StreamResponse:
        path = "/" + request.match_info["path"]
        if path.startswith("/ingress/") and path.count("/") >= 3:
            return await self._ingress(request, path)
        self._seen.append(await _seen(request, path))
        answer = self._next_answer(request.method, path)
        if answer is None:
            if (request.method, path) == ("POST", "/ingress/session"):
                session = Session(session=f"session-{len(self.sessions) + 1}")
                self.sessions.append(session.session)
                return web.json_response(supervisor_ok(session.to_dict()))
            return web.json_response(
                supervisor_error(f"No answer for {path}"), status=404
            )
        if answer.hang:
            await _forever()
        if answer.drop:
            assert request.transport is not None
            request.transport.close()
            raise ConnectionResetError
        if answer.text is not None:
            return web.Response(status=answer.status, text=answer.text)
        return web.json_response(answer.payload, status=answer.status)

    async def _ingress(self, request: web.Request, path: str) -> web.StreamResponse:
        if request.cookies.get("ingress_session") not in self.sessions:
            return web.Response(status=401, text="401: Unauthorized")
        _, _, token, rest = path.split("/", 3)
        assert token == INGRESS_TOKEN
        return await self.target.handle(request, "/" + rest)


def app_info(slug: str = SLUG, **changes: Any) -> dict[str, Any]:
    """``GET /addons/<slug>/info`` as Supervisor answers it: a started app with
    ingress, built from aiohasupervisor's model; ``changes`` replace its fields."""
    # The aiohasupervisor release Core 2026.12 pins requires log_format; the
    # one 2026.11 pins has no such field. Drop this check once the stable Core
    # pin moves off 2026.11.
    log_fields: dict[str, Any] = {}
    if any(spec.name == "log_format" for spec in fields(InstalledAddonComplete)):
        log_fields["log_format"] = "plain"
    info = InstalledAddonComplete(
        **log_fields,
        apparmor=AppArmor.DEFAULT,
        auth_api=False,
        docker_api=False,
        full_access=False,
        homeassistant_api=True,
        host_network=False,
        host_pid=False,
        ingress=True,
        long_description=None,
        rating=6,
        signed=False,
        supervisor_api=True,
        supervisor_role=SupervisorRole.DEFAULT,
        arch=[CpuArch.AMD64, CpuArch.AARCH64],
        documentation=False,
        detached=False,
        advanced=False,
        available=True,
        build=True,
        description="An app the tests reach",
        homeassistant=None,
        icon=False,
        logo=False,
        name="Target App",
        repository="local",
        slug=slug,
        stage=AddonStage.STABLE,
        update_available=False,
        url=None,
        version_latest="1.0.0",
        version="1.0.0",
        state=AddonState.STARTED,
        hostname=slug.replace("_", "-"),
        dns=[f"{slug.replace('_', '-')}.local.hass.io"],
        protected=True,
        boot=AddonBoot.AUTO,
        boot_config=AddonBootConfig.AUTO,
        options={},
        schema=[],
        machine=[],
        network=None,
        network_description=None,
        host_ipc=False,
        host_uts=False,
        host_dbus=False,
        privileged=[],
        changelog=False,
        stdin=False,
        gpio=False,
        usb=False,
        uart=False,
        kernel_modules=False,
        devicetree=False,
        udev=False,
        video=False,
        audio=False,
        startup=AddonStartup.APPLICATION,
        services=[],
        discovery=[],
        translations={},
        webui=None,
        ingress_entry=f"/api/hassio_ingress/{INGRESS_TOKEN}",
        ingress_url=f"/api/hassio_ingress/{INGRESS_TOKEN}/",
        ingress_port=8099,
        ingress_panel=False,
        audio_input=None,
        audio_output=None,
        auto_update=False,
        ip_address=IPv4Address("127.0.0.1"),
        watchdog=False,
        devices=[],
        system_managed=False,
        system_managed_config_entry=None,
    )
    return replace(info, **changes).to_dict(by_alias=True)


def installed_apps(*slugs: str) -> dict[str, Any]:
    """Supervisor's installed app list, using the list-view model for each row."""
    apps = [InstalledAddon.from_dict(app_info(slug)) for slug in slugs]
    return AddonsList(addons=apps).to_dict()


@pytest.fixture
async def target_app(aiohttp_client: Any, socket_enabled: None) -> TargetApp:
    """The target app, listening on its container address."""
    target = TargetApp()
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", target.serve)
    server = await aiohttp_client(app)
    target.port = server.port
    return target


async def serve_supervisor(aiohttp_client: Any, target: TargetApp) -> Supervisor:
    """A stand-in Supervisor in front of ``target``, listening on 127.0.0.1."""
    found = Supervisor(target)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", found.serve)
    server = await aiohttp_client(app)
    found.url = f"127.0.0.1:{server.port}"
    return found


@pytest.fixture
async def supervisor(aiohttp_client: Any, target_app: TargetApp) -> Supervisor:
    """The stand-in Supervisor, in front of ``target_app``."""
    return await serve_supervisor(aiohttp_client, target_app)


@pytest.fixture
async def socket_supervisor(
    target_app: TargetApp, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Supervisor]:
    """The stand-in Supervisor on the app's Unix socket, mounted as Supervisor
    2026.11 mounts it (``SUPERVISOR_SOCKET``); ``http://supervisor`` leads
    nowhere, so only a request over the socket is answered."""
    # The plugin's allow list refuses Unix sockets; Core's hassio tests reset it.
    pytest_socket.socket_allow_hosts(["127.0.0.1"], allow_unix_socket=True)
    found = Supervisor(target_app)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", found.serve)
    runner = web.AppRunner(app)
    await runner.setup()
    # Not tmp_path: it can exceed the 104-byte sun_path limit on macOS.
    socket_dir = tempfile.mkdtemp(dir="/tmp")
    socket_path = Path(socket_dir, "supervisor.sock")
    await web.UnixSite(runner, str(socket_path)).start()
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.delenv("SUPERVISOR_BASE_URL", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)
    monkeypatch.setenv("SUPERVISOR_SOCKET", str(socket_path))
    try:
        yield found
    finally:
        await runner.cleanup()
        shutil.rmtree(socket_dir)


@pytest.fixture
async def core_supervisor(
    hass: HomeAssistant, supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> Supervisor:
    """Core's ``hassio`` integration in front of ``supervisor``, no app
    installed. Once Core is set up, Supervisor answers only what the test
    queues."""
    await set_up_hassio(hass, supervisor, monkeypatch)
    supervisor.forget_defaults()
    return supervisor


@dataclass(frozen=True)
class Route:
    """The server's client, and the Supervisor it reaches by one route."""

    client: HomeAssistantClient
    supervisor: Supervisor
    in_app: bool

    @property
    def target(self) -> TargetApp:
        return self.supervisor.target


@pytest.fixture
async def core_route(
    core_supervisor: Supervisor, core_client: HomeAssistantClient
) -> Route:
    """The client outside the app: Supervisor through Core."""
    return Route(core_client, core_supervisor, in_app=False)


@pytest.fixture
async def app_route(
    supervisor: Supervisor,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Route]:
    """The client as the app runs it: Supervisor asked directly."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    monkeypatch.setenv("SUPERVISOR_TOKEN", APP_TOKEN)
    monkeypatch.setenv("SUPERVISOR_BASE_URL", f"http://{supervisor.url}")
    yield Route(core_client, supervisor, in_app=True)


@dataclass
class SupervisorWait:
    """How long the app's own Supervisor requests may wait, cut short.

    ``asked`` keeps what the tool asked for; the request waits ``read`` and
    ``connect`` instead, so a Supervisor that never answers fails at once.
    """

    read: float = 0.1
    connect: float = 5.0
    asked: list[httpx.Timeout] = field(default_factory=list)


@pytest.fixture
def supervisor_wait(monkeypatch: pytest.MonkeyPatch) -> SupervisorWait:
    """Inside the app, Supervisor requests built by the real factory, waiting
    ``SupervisorWait`` long."""
    wait = SupervisorWait()

    def short(*, timeout: httpx.Timeout, verify: bool) -> httpx.AsyncClient:
        wait.asked.append(timeout)
        cut = httpx.Timeout(wait.read, connect=wait.connect)
        return make_supervisor_httpx_client(timeout=cut, verify=verify)

    monkeypatch.setattr(tools_addons, "make_supervisor_httpx_client", short)
    return wait


@pytest.fixture
def quick_app_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app proxy's HTTP client gives up after a tenth of a second."""
    build = tools_addons.build_addon_http_client

    def quick(
        client: HomeAssistantClient, timeout: int, slug: str
    ) -> httpx.AsyncClient:
        http = build(client, timeout, slug)
        http.timeout = httpx.Timeout(0.1)
        return http

    monkeypatch.setattr(tools_addons, "build_addon_http_client", quick)


@pytest.fixture
def quick_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A job-group collision is retried after milliseconds, not seconds."""
    monkeypatch.setattr(tools_addons, "JOB_COLLISION_RETRY_INITIAL_DELAY", 0.001)
    monkeypatch.setattr(tools_addons, "JOB_COLLISION_RETRY_MAX_DELAY", 0.002)


@pytest.fixture
def fresh_secrets() -> Iterator[None]:
    """Do not let remembered secret values escape the test that registered them."""
    redaction.clear_known_secret_values()
    yield
    redaction.clear_known_secret_values()


def timers(hass: HomeAssistant) -> set[asyncio.TimerHandle]:
    """The timers armed on Core's loop now."""
    return set(get_scheduled_timer_handles(hass.loop))


async def armed(
    hass: HomeAssistant, low: float, high: float, besides: set[asyncio.TimerHandle]
) -> None:
    """Return once a timer outside ``besides`` is due in ``low``-``high`` s."""
    loop = hass.loop
    for _ in range(10_000):
        due = [
            handle.when() - loop.time()
            for handle in timers(hass) - besides
            if not handle.cancelled()
        ]
        if any(low < seconds <= high for seconds in due):
            return
        await asyncio.sleep(0)
    raise AssertionError(f"no timer due in ({low}, {high}] seconds: {sorted(due)}")


async def outwait(
    hass: HomeAssistant, low: float, high: float, besides: set[asyncio.TimerHandle]
) -> None:
    """Once the call arms a timer due in ``low``-``high`` seconds, move Core's
    clock ``high`` seconds on so it fires."""
    await armed(hass, low, high, besides)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=high))


async def manage(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    """``ha_manage_app`` with ``overrides`` on its defaults."""
    return await AddOnTools(client).manage_addon(**(MANAGE_DEFAULTS | overrides))


async def read(client: HomeAssistantClient, **overrides: Any) -> dict[str, Any]:
    """``ha_get_app`` with ``overrides`` on its defaults."""
    kwargs: dict[str, Any] = {
        "source": None,
        "slug": None,
        "include_stats": False,
        "repository": None,
        "query": None,
    } | overrides
    return await AddOnTools(client).get_addon(**kwargs)
