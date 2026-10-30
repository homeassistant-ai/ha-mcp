"""The in-process server's "Local network" listener serves IPv6 too (issue #2915)."""

from __future__ import annotations

import logging
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from ._embedded_stubs import install
from .test_listen_sockets import _load, _require_ipv6_loopback
from .test_sidecar_pin_port import _a_free_port

install()

from custom_components.ha_mcp_tools import embedded_listen  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
)

from .test_embedded_server import (  # noqa: E402
    _manager,
    _no_manifest_pin,  # noqa: F401 - autouse fixture
    _stub_ha_mcp_surface,
)
from .test_embedded_server_stop import _clear_worker_env  # noqa: E402

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
    config.get_global_settings = lambda: SimpleNamespace(ipv6_allowed_networks="")
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


def _serve_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: int, serve: Any
) -> tuple[Any, dict[str, Any]]:
    """Run the worker against stub uvicorn with the helpers in the bundle."""
    _require_ipv6_loopback()
    _clear_worker_env(monkeypatch)
    _install_helpers(monkeypatch)  # before the stub uvicorn: the guard imports it
    _stub_ha_mcp_surface(monkeypatch, mcp=_FakeMcp())
    sys.modules["ha_mcp.config"].get_global_settings().ipv6_allowed_networks = ""
    seen: dict[str, Any] = {}
    uvicorn_mod = ModuleType("uvicorn")
    uvicorn_mod.Config = lambda app, **kw: seen.update(app=app, config=kw)
    uvicorn_mod.Server = lambda config: SimpleNamespace(should_exit=False, serve=serve)
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn_mod)
    mgr, _hass, _entry = _manager(
        tmp_path, options={OPT_BIND_HOST: BIND_HOST_ALL, OPT_SERVER_PORT: port}
    )
    mgr._thread_main("tok")
    return mgr, seen


def test_worker_serves_guarded_dual_stack_and_releases_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served: list[Any] = []

    async def serve(sockets: Any = None) -> None:
        served.extend(sockets)

    mgr, seen = _serve_worker(tmp_path, monkeypatch, _a_free_port(), serve)
    assert mgr._thread_exc is None
    assert type(seen["app"]).__name__ == "IPv6LanGuard"
    assert seen["config"]["proxy_headers"] is False
    assert {sock.family for sock in served} == {socket.AF_INET, socket.AF_INET6}
    assert all(sock.fileno() == -1 for sock in served)


def test_busy_port_reads_as_a_startup_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def serve(sockets: Any = None) -> None:
        raise AssertionError("served on a busy port")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        mgr, _seen = _serve_worker(tmp_path, monkeypatch, taken.getsockname()[1], serve)
    message = str(mgr._thread_exc)
    assert "exited during startup" in message
    assert "Address already in use" in message


async def test_readiness_ignores_another_process_on_the_port(tmp_path: Path) -> None:
    # Another listener on the port answered the loopback probe, so a server
    # that never started was reported as running.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        mgr, _hass, _entry = _manager(
            tmp_path, options={OPT_SERVER_PORT: other.getsockname()[1]}
        )
        mgr._uv_server = None
        assert await mgr._async_server_ready() is False
        mgr._uv_server = SimpleNamespace(started=False)
        assert await mgr._async_server_ready() is False
        mgr._uv_server = SimpleNamespace(started=True)
        assert await mgr._async_server_ready() is True
