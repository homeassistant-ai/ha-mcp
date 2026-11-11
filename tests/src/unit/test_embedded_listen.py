"""The in-process server's "Local network" listener serves IPv6 too (issue #2915).

The worker tests start the server through ``EmbeddedServerManager.async_start``
on a real ``hass`` with a real server config entry. The package install and
the access token (this component's own collaborators), the bundled ha-mcp
server and uvicorn are replaced; the listener and the readiness check are real.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import socket
import sys
from contextlib import asynccontextmanager
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import embedded_listen
from custom_components.ha_mcp_tools import embedded_server as es
from custom_components.ha_mcp_tools.const import (
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
)

from ._embedded_server_helpers import _a_free_port as _a_free_port
from ._embedded_server_helpers import _clear_worker_env as _clear_worker_env
from ._embedded_server_helpers import _load as _load
from ._embedded_server_helpers import _no_manifest_pin as _no_manifest_pin
from ._embedded_server_helpers import _require_ipv6_loopback as _require_ipv6_loopback
from ._embedded_server_helpers import _stub_ha_mcp_surface as _stub_ha_mcp_surface
from ._server_entry import add_server_entry

# These tests run their own listener on the loopback interface; Core's test
# plugin blocks sockets unless a test asks for them, as its own HTTP clients do.
pytestmark = pytest.mark.usefixtures("socket_enabled")

_SRC = Path(__file__).parents[3] / "src" / "ha_mcp"


def _install_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bundled ha-mcp ship the listener helpers."""
    for name in ("listen_sockets", "ipv6_lan_guard"):
        module = _load(_SRC / f"{name}.py", f"ha_mcp.{name}")
        monkeypatch.setitem(sys.modules, f"ha_mcp.{name}", module)


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
    listener = embedded_listen.DirectListener(BIND_HOST_LOOPBACK, _a_free_port())
    app = object()
    assert listener.wrap(app) == (app, True)
    assert listener.bind() == {}


def test_older_bundled_server_keeps_ipv4_bind_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setitem(sys.modules, "ha_mcp.listen_sockets", None)
    monkeypatch.setitem(sys.modules, "ha_mcp.ipv6_lan_guard", None)
    caplog.set_level(logging.INFO, logger=embedded_listen.__name__)
    listener = embedded_listen.DirectListener(BIND_HOST_ALL, _a_free_port())
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


def _worker(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    options: dict[str, Any],
    *,
    starts: bool = True,
) -> tuple[es.EmbeddedServerManager, _Uvicorn]:
    """A manager whose start runs the real worker on the stubbed server."""
    _clear_worker_env(monkeypatch)
    _install_helpers(monkeypatch)  # before the stub uvicorn: the guard imports it
    _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())
    # An imported package has a spec; the post-start version check looks it up.
    spec = ModuleSpec("ha_mcp", None, is_package=True)
    monkeypatch.setattr(sys.modules["ha_mcp"], "__spec__", spec)
    sys.modules["ha_mcp.config"].get_global_settings().ipv6_allowed_networks = ""
    uvicorn = _Uvicorn(monkeypatch, starts=starts)
    manager = es.EmbeddedServerManager(hass, add_server_entry(hass, options=options))
    monkeypatch.setattr(manager, "_async_ensure_package", AsyncMock(return_value=None))
    monkeypatch.setattr(manager, "_async_warn_on_dependency_conflicts", AsyncMock())
    monkeypatch.setattr(manager, "_async_access_token", AsyncMock(return_value="tok"))
    monkeypatch.setattr(manager, "_prepare_config_dir", lambda: None)
    monkeypatch.setattr(es, "_purge_ha_mcp_modules", lambda: None)
    return manager, uvicorn


async def test_worker_serves_guarded_dual_stack_and_releases_it(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _require_ipv6_loopback()
    options = {OPT_BIND_HOST: BIND_HOST_ALL, OPT_SERVER_PORT: _a_free_port()}
    manager, uvicorn = _worker(hass, monkeypatch, options)

    await manager.async_start()  # raises if the worker failed
    await manager.async_stop()

    assert type(uvicorn.app).__name__ == "IPv6LanGuard"
    assert uvicorn.config["proxy_headers"] is False
    assert {sock.family for sock in uvicorn.served} == {socket.AF_INET, socket.AF_INET6}
    assert all(sock.fileno() == -1 for sock in uvicorn.served)


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
        manager, uvicorn = _worker(hass, monkeypatch, options)
        with pytest.raises(es.EmbeddedServerError) as exc:
            await manager.async_start()
        await manager.async_stop()
    message = str(exc.value)
    assert "exited during startup" in message
    assert os.strerror(errno.EADDRINUSE) in message
    assert uvicorn.served == []  # never served on the busy port


async def test_readiness_ignores_another_process_on_the_port(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another listener on the port answered the loopback probe, so a server
    # that never started was reported as running.
    monkeypatch.setattr(es, "_READY_POLL_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(es, "_READY_STALL_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(es, "_READY_TOTAL_CAP_SECONDS", 10.0)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        options = {
            OPT_BIND_HOST: BIND_HOST_LOOPBACK,
            OPT_SERVER_PORT: other.getsockname()[1],
        }

        never_started, _ = _worker(hass, monkeypatch, options, starts=False)
        with pytest.raises(es.EmbeddedServerError, match="did not become reachable"):
            await never_started.async_start()
        assert never_started.is_running is False

        started, _ = _worker(hass, monkeypatch, options, starts=True)
        await started.async_start()  # its own server started, and the port answers
        assert started.is_running is True
        await started.async_stop()
