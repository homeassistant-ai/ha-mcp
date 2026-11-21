"""The in-process server's "Local network" listener serves IPv6 too (issue #2915).

The worker tests start the server through ``EmbeddedServerManager.async_start``
on a real ``hass`` with a real server config entry and an administrator's
token. The bundled ha-mcp server and uvicorn are replaced; the package check,
the module purge, the listener and the readiness check are real.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import socket
import sys
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_listen
from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    DATA_ADMIN_TOKEN,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
)

from ._core_client import free_port
from ._embedded_server_core import issue_admin_token
from ._embedded_server_helpers import _clear_worker_env as _clear_worker_env
from ._embedded_server_helpers import _load as _load
from ._embedded_server_helpers import _require_ipv6_loopback as _require_ipv6_loopback
from ._embedded_server_helpers import _stub_ha_mcp_surface as _stub_ha_mcp_surface
from ._server_entry import add_server_entry

# These tests run their own listener on the loopback interface; Core's test
# plugin blocks sockets unless a test asks for them, as its own HTTP clients do.
pytestmark = pytest.mark.usefixtures("socket_enabled")

_SRC = Path(__file__).parents[3] / "src" / "ha_mcp"


def _install_helpers(monkeypatch: pytest.MonkeyPatch) -> dict[str, ModuleType]:
    """Make the bundled ha-mcp ship the listener helpers."""
    modules = {}
    for name in ("listen_sockets", "ipv6_lan_guard"):
        module = _load(_SRC / f"{name}.py", f"ha_mcp.{name}")
        monkeypatch.setitem(sys.modules, f"ha_mcp.{name}", module)
        modules[f"ha_mcp.{name}"] = module
    return modules


@pytest.fixture
def module_cache() -> Iterator[None]:
    """Put back the ``ha_mcp`` modules a real start purges from ``sys.modules``."""
    saved = {n: m for n, m in sys.modules.items() if n.split(".")[0] == "ha_mcp"}
    yield
    for name in [n for n in sys.modules if n.split(".")[0] == "ha_mcp"]:
        del sys.modules[name]
    sys.modules.update(saved)


@pytest.fixture
def helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_ipv6_loopback()
    _install_helpers(monkeypatch)
    config = ModuleType("ha_mcp.config")
    monkeypatch.setattr(
        config,
        "get_global_settings",
        lambda: SimpleNamespace(ipv6_allowed_networks=""),
        raising=False,
    )
    monkeypatch.setitem(sys.modules, "ha_mcp.config", config)


def test_loopback_choice_is_unchanged(helpers: None) -> None:
    listener = embedded_listen.DirectListener(BIND_HOST_LOOPBACK, free_port())
    app = object()
    assert listener.wrap(app) == (app, True)
    assert listener.bind() == {}


def test_older_bundled_server_keeps_ipv4_bind_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "ha_mcp.listen_sockets", None)
    monkeypatch.setitem(sys.modules, "ha_mcp.ipv6_lan_guard", None)
    caplog.set_level(logging.INFO, logger=embedded_listen.__name__)
    listener = embedded_listen.DirectListener(BIND_HOST_ALL, free_port())
    app = object()
    assert listener.wrap(app) == (app, True)
    assert listener.bind() == {}
    assert "IPv4 clients only" in caplog.text


@asynccontextmanager
async def _lifespan() -> Any:
    yield


class _FakeMcp:
    _lifespan_manager = staticmethod(_lifespan)

    def http_app(self, path: str, stateless_http: bool) -> object:
        return object()


class _Uvicorn:
    """Stub uvicorn recording what the worker serves.

    ``serve`` listens on the sockets it is handed, as uvicorn does, reports
    itself started when ``starts`` is set, and runs until told to exit.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, starts: bool) -> None:
        self.app: Any = None
        self.config: dict[str, Any] = {}
        self.served: list[socket.socket] = []
        self.starts = starts
        self.server = SimpleNamespace(
            should_exit=False, started=False, serve=self.serve
        )
        module = ModuleType("uvicorn")
        monkeypatch.setattr(module, "Config", self.make_config, raising=False)
        monkeypatch.setattr(module, "Server", lambda config: self.server, raising=False)
        monkeypatch.setitem(sys.modules, "uvicorn", module)

    def make_config(self, app: Any, **config: Any) -> SimpleNamespace:
        self.app = app
        self.config = config
        return SimpleNamespace()

    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        for sock in sockets or []:
            sock.listen()
            self.served.append(sock)
        self.server.started = self.starts
        # uvicorn's own main loop polls should_exit the same way.
        while not self.server.should_exit:  # noqa: ASYNC110
            await asyncio.sleep(0.01)


async def _worker(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    *,
    starts: bool = True,
) -> tuple[es.EmbeddedServerManager, _Uvicorn]:
    """A manager whose real start runs the worker on the stubbed server.

    Core's test ``hass`` runs with ``skip_pip``, so the package step checks the
    ha-mcp installed here against the component's manifest pin.
    """
    _clear_worker_env(monkeypatch)
    helpers = _install_helpers(
        monkeypatch
    )  # before the stub uvicorn: the guard imports it
    _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp(), served=helpers)
    sys.modules["ha_mcp.config"].get_global_settings().ipv6_allowed_networks = ""
    uvicorn = _Uvicorn(monkeypatch, starts=starts)
    data = {DATA_ADMIN_TOKEN: await issue_admin_token(hass)}
    entry = add_server_entry(hass, data=data, options=options)
    return es.EmbeddedServerManager(hass, entry), uvicorn


@pytest.mark.usefixtures("module_cache")
async def test_worker_serves_guarded_dual_stack_and_releases_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_ipv6_loopback()
    options = {OPT_BIND_HOST: BIND_HOST_ALL, OPT_SERVER_PORT: free_port()}
    manager, uvicorn = await _worker(hass, monkeypatch, options)

    await manager.async_start()  # raises if the worker failed
    await manager.async_stop()

    assert type(uvicorn.app).__name__ == "IPv6LanGuard"
    assert uvicorn.config["proxy_headers"] is False
    assert {sock.family for sock in uvicorn.served} == {socket.AF_INET, socket.AF_INET6}
    assert all(sock.fileno() == -1 for sock in uvicorn.served)


@pytest.mark.usefixtures("module_cache")
async def test_busy_port_reads_as_a_startup_exit(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_ipv6_loopback()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        options = {
            OPT_BIND_HOST: BIND_HOST_ALL,
            OPT_SERVER_PORT: taken.getsockname()[1],
        }
        manager, uvicorn = await _worker(hass, monkeypatch, options)
        with pytest.raises(es.EmbeddedServerError) as exc:
            await manager.async_start()
        await manager.async_stop()
    message = str(exc.value)
    assert "exited during startup" in message
    assert os.strerror(errno.EADDRINUSE) in message
    assert uvicorn.served == []  # never served on the busy port


@pytest.mark.usefixtures("module_cache")
async def test_readiness_ignores_another_process_on_the_port(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another listener on the port answered the loopback probe, so a server
    # that never started was reported as running.
    monkeypatch.setattr(es, "READY_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(es, "READY_STALL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(es, "READY_TOTAL_CAP_SECONDS", 10.0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        options = {
            OPT_BIND_HOST: BIND_HOST_LOOPBACK,
            OPT_SERVER_PORT: other.getsockname()[1],
        }

        never_started, _ = await _worker(hass, monkeypatch, options, starts=False)
        with pytest.raises(es.EmbeddedServerError, match="did not become reachable"):
            await never_started.async_start()
        assert never_started.is_running is False

        started, _ = await _worker(hass, monkeypatch, options, starts=True)
        await started.async_start()  # its own server started, and the port answers
        assert started.is_running is True
        await started.async_stop()
