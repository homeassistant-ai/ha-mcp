"""Real Core for the HAOS image builder's Supervisor client.

``tests/haos_image_build/build_image.py`` talks to a booted HAOS: Core's REST
onboarding and auth, Core's WebSocket, and the Supervisor behind Core's
``hassio`` ``supervisor/api`` command. Here all of that is real Core, with
Core's own ``hassio`` integration (``_hassio_core``) in front of the Supervisor
stand-in of ``_tools_addons_core`` (a unit test has no Supervisor).

The builder is synchronous, so a test runs it in an executor thread while
Core's loop serves it. Between the builder and Core sits a TCP relay the test
can cut, as a Core or Supervisor restart cuts the builder's connection. The
builder's clock is ``BuildClock``, set as the builder's ``time``: sleeps advance
it instead of waiting, and a test can run code at a given sleep. Core's own
clock is untouched.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from functools import partial
from ipaddress import IPv4Address
from typing import Any, TypeVar

import pytest
from aiohasupervisor.models import (
    HomeAssistantInfo,
    LogLevel,
    RootInfo,
    SupervisorInfo,
    SupervisorState,
    UpdateChannel,
)
from aiohttp import web
from homeassistant.auth.providers.homeassistant import HassAuthProvider
from homeassistant.components.websocket_api.const import DATA_CONNECTIONS
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from tests.haos_image_build import build_image
from tests.haos_image_build.build_image import HAWebSocket, OAuthCredentials, onboard

from ._hassio_core import set_up_hassio
from ._tools_addons_core import Supervisor, TargetApp
from ._tools_addons_core import target_app as target_app
from ._ws_transport import until

T = TypeVar("T")

# Stands for Supervisor's answer while it restarts (``BuildSupervisor.restarting``).
RESTARTING = object()


@dataclass
class BuildClock:
    """The builder's ``time``: ``monotonic`` reads it, ``sleep`` advances it."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)
    # Run once, on the sleep with this index (0 is the first sleep).
    on_sleep: dict[int, Callable[[], None]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def monotonic(self) -> float:
        with self._lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            index = len(self.sleeps)
            self.sleeps.append(seconds)
            self.now += seconds
        if (hook := self.on_sleep.pop(index, None)) is not None:
            hook()

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds


class Relay:
    """A TCP relay to Core whose open connections the test can cut."""

    def __init__(self, target_port: int) -> None:
        self._target_port = target_port
        self._server: asyncio.Server | None = None
        self._pairs: list[tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []
        self.port = 0
        # The method and path of each connection's first request, in order.
        self.requests: list[str] = []

    @property
    def websockets(self) -> int:
        """WebSocket connections the builder opened through the relay."""
        return self.requests.count("GET /api/websocket")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        up_reader, up_writer = await asyncio.open_connection(
            "127.0.0.1", self._target_port
        )
        self._pairs.append((writer, up_writer))
        first = await reader.read(65536)
        self.requests.append(" ".join(first.split(b"\r\n", 1)[0].decode().split()[:2]))
        up_writer.write(first)
        await asyncio.gather(
            self._pipe(reader, up_writer),
            self._pipe(up_reader, writer),
            return_exceptions=True,
        )

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    def cut(self) -> None:
        """Drop every open connection, as a restart drops the builder's socket.

        Callable from a builder thread too: the sockets are closed on the loop
        that owns them."""
        if _on_loop(self._loop):
            for client, upstream in self._pairs:
                client.transport.abort()
                upstream.transport.abort()
            self._pairs.clear()
        else:
            asyncio.run_coroutine_threadsafe(_call(self.cut), self._loop).result(5)

    async def close(self) -> None:
        self.cut()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


class BuildSupervisor(Supervisor):
    """The Supervisor stand-in, able to run code when a request reaches it."""

    def __init__(self, target: TargetApp) -> None:
        super().__init__(target)
        self._hooks: dict[tuple[str, str], list[Callable[[], Any]]] = {}

    def when(self, method: str, path: str, hook: Callable[[], Any]) -> None:
        """Run ``hook`` once, as the next ``method path`` request arrives; a
        coroutine it returns is awaited before Supervisor answers."""
        self._hooks.setdefault((method, path), []).append(hook)

    def restarting(self, method: str, path: str) -> None:
        """Answer the next ``method path`` request as a restarting Supervisor:
        its proxy has no handler yet and answers a plain-text error, which Core
        passes on as a blank ``unknown_error``."""
        self.reply(method, path, status=502, text="")

    async def serve(self, request: web.Request) -> web.StreamResponse:
        hooks = self._hooks.get((request.method, "/" + request.match_info["path"]))
        if hooks and inspect.isawaitable(result := hooks.pop(0)()):
            await asyncio.gather(result)
        return await super().serve(request)


@dataclass
class Haos:
    """Core, its Supervisor stand-in, and the builder's view of them."""

    hass: HomeAssistant
    supervisor: BuildSupervisor
    relay: Relay
    clock: BuildClock
    base_url: str
    credentials: OAuthCredentials
    monkeypatch: pytest.MonkeyPatch
    # Every client the test logged in, closed when the test ends.
    clients: list[HAWebSocket] = field(default_factory=list)

    async def run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run builder code in a worker thread while Core serves it."""
        return await self.hass.async_add_executor_job(partial(func, *args, **kwargs))

    async def connected(
        self, credentials: OAuthCredentials | None = None
    ) -> HAWebSocket:
        """A builder client logged in to Core over the relay."""
        ws = HAWebSocket(self.base_url, credentials or self.credentials)
        self.clients.append(ws)
        await self.run(ws.connect)
        return ws

    async def member_credentials(self) -> OAuthCredentials:
        """Real tokens of a Core user who is not an administrator."""
        user = await self.hass.auth.async_create_user(
            "Member", group_ids=["system-users"]
        )
        refresh = await self.hass.auth.async_create_refresh_token(
            user, client_id=self.base_url
        )
        access = self.hass.auth.async_create_access_token(refresh)
        return OAuthCredentials(access_token=access, refresh_token=refresh.token)

    def revoke_login(self) -> None:
        """Revoke the builder's refresh token, as a user signing it out does.

        Callable from a builder thread too."""
        if not _on_loop(self.hass.loop):
            self.in_core(self.revoke_login)
            return
        refresh = self.hass.auth.async_get_refresh_token_by_token(
            self.credentials.refresh_token
        )
        assert refresh is not None
        self.hass.auth.async_remove_refresh_token(refresh)

    async def load_supervisor_api(self) -> None:
        """Load Core's ``hassio``, which registers ``supervisor/api``. From
        then on Supervisor answers only what the test queues."""
        await set_up_hassio(self.hass, self.supervisor, self.monkeypatch)
        self.supervisor.forget_defaults()

    def in_core(self, func: Callable[[], Any]) -> None:
        """Run ``func`` on Core's loop from a builder thread, and wait for it
        (and for a coroutine it returns)."""
        asyncio.run_coroutine_threadsafe(_call(func), self.hass.loop).result(5)


async def _call(func: Callable[[], Any]) -> None:
    if inspect.isawaitable(result := func()):
        await result


def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
    try:
        return asyncio.get_running_loop() is loop
    except RuntimeError:
        return False


@pytest.fixture
async def build_supervisor(
    aiohttp_client: Any, target_app: TargetApp
) -> BuildSupervisor:
    found = BuildSupervisor(target_app)
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", found.serve)
    server = await aiohttp_client(app)
    found.url = f"127.0.0.1:{server.port}"
    return found


async def _haos(
    hass: HomeAssistant,
    supervisor: BuildSupervisor,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    supervisor_api: bool,
) -> AsyncIterator[Haos]:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    clock = BuildClock()
    monkeypatch.setattr(build_image, "time", clock)
    for domain in ("http", "api", "websocket_api", "onboarding"):
        assert await async_setup_component(hass, domain, {})
    # Core's frontend serves the /manifest.json the builder waits on after a
    # Core update.
    assert await async_setup_component(hass, "frontend", {})
    server = await aiohttp_client(hass.http.app)
    relay = Relay(server.port)
    await relay.start()
    base_url = f"http://127.0.0.1:{relay.port}"
    credentials = await hass.async_add_executor_job(onboard, base_url)
    haos = Haos(hass, supervisor, relay, clock, base_url, credentials, monkeypatch)
    if supervisor_api:
        await haos.load_supervisor_api()
    try:
        yield haos
    finally:
        for client in haos.clients:
            await haos.run(client.__exit__)
        await relay.close()
        # Core closes a WebSocket a moment after its client is gone.
        await until(lambda: not hass.data.get(DATA_CONNECTIONS))


@pytest.fixture
async def haos(
    hass: HomeAssistant,
    local_auth: HassAuthProvider,
    build_supervisor: BuildSupervisor,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Haos]:
    """A booted HAOS as the builder sees it: onboarded, ``supervisor/api`` ready.

    ``local_auth`` is Core's own login provider, the one onboarding creates the
    owner in."""
    async for found in _haos(
        hass, build_supervisor, aiohttp_client, monkeypatch, supervisor_api=True
    ):
        yield found


@pytest.fixture
async def haos_booting(
    hass: HomeAssistant,
    local_auth: HassAuthProvider,
    build_supervisor: BuildSupervisor,
    aiohttp_client: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Haos]:
    """Core up but ``hassio`` not loaded yet: ``supervisor/api`` is unknown."""
    async for found in _haos(
        hass, build_supervisor, aiohttp_client, monkeypatch, supervisor_api=False
    ):
        yield found


# Supervisor's answers are built with the models of aiohasupervisor, the
# Supervisor client Core uses, so each one has every field Supervisor sends,
# with its type.


def supervisor_info(
    *,
    version: str = "2026.06.1",
    version_latest: str = "2026.06.1",
    update_available: bool = False,
    channel: str = "stable",
) -> dict[str, Any]:
    """Supervisor's ``/supervisor/info`` data."""
    return SupervisorInfo(
        version=version,
        version_latest=version_latest,
        update_available=update_available,
        channel=UpdateChannel(channel),
        arch="amd64",
        supported=True,
        healthy=True,
        ip_address=IPv4Address("172.30.32.2"),
        timezone="UTC",
        logging=LogLevel.INFO,
        debug=False,
        debug_block=False,
        diagnostics=None,
        auto_update=True,
        country=None,
        detect_blocking_io=False,
        feature_flags={},
    ).to_dict()


def root_info(state: str) -> dict[str, Any]:
    """Supervisor's root ``/info`` data, which carries its own state."""
    return RootInfo(
        supervisor="2026.06.1",
        homeassistant="2026.8.2",
        hassos="17.0",
        docker="28.1.1",
        hostname="homeassistant",
        operating_system="Home Assistant OS 17.0",
        features=[],
        machine="qemux86-64",
        machine_id=None,
        arch="amd64",
        state=SupervisorState(state),
        supported_arch=["amd64"],
        supported=True,
        channel=UpdateChannel.STABLE,
        logging=LogLevel.INFO,
        timezone="UTC",
    ).to_dict()


def core_info(
    version: str, *, version_latest: str, update_available: bool
) -> dict[str, Any]:
    """Supervisor's ``/core/info`` data."""
    return HomeAssistantInfo(
        version=version,
        version_latest=version_latest,
        update_available=update_available,
        machine="qemux86-64",
        ip_address=IPv4Address("172.30.32.1"),
        arch="amd64",
        image="ghcr.io/home-assistant/qemux86-64-homeassistant",
        boot=True,
        port=8123,
        ssl=False,
        watchdog=True,
        audio_input=None,
        audio_output=None,
        backups_exclude_database=False,
        duplicate_log_file=False,
    ).to_dict()
