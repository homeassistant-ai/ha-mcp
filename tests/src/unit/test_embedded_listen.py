"""The in-process server's "Local network" listener serves IPv6 too (issue #2915)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from ._embedded_stubs import install
from .test_sidecar_pin_port import _a_free_port

install()

from custom_components.ha_mcp_tools import embedded_listen  # noqa: E402
from custom_components.ha_mcp_tools.const import (  # noqa: E402
    BIND_HOST_ALL,
    BIND_HOST_LOOPBACK,
)

_SRC = Path(__file__).parents[3] / "src" / "ha_mcp"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(f"{name}_el", _SRC / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundled ha-mcp that ships the listener helpers."""
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback unavailable on this machine")
    for name in ("listen_sockets", "ipv6_lan_guard"):
        monkeypatch.setitem(sys.modules, f"ha_mcp.{name}", _load(name))
    config = ModuleType("ha_mcp.config")
    config.get_global_settings = lambda: SimpleNamespace(ipv6_allowed_networks="")
    monkeypatch.setitem(sys.modules, "ha_mcp.config", config)
    transport = ModuleType("ha_mcp.transport_security")
    transport.ensure_host_origin_guard_default_off = lambda: None
    monkeypatch.setitem(sys.modules, "ha_mcp.transport_security", transport)


def test_local_network_binds_both_families_behind_the_guard(helpers: None) -> None:
    listener = embedded_listen.DirectListener(BIND_HOST_ALL, _a_free_port())
    app, proxy_headers = listener.wrap(object())
    assert type(app).__name__ == "IPv6LanGuard"
    # The guard re-applies proxy headers after its own check.
    assert proxy_headers is False
    try:
        families = {sock.family for sock in listener.bind()["sockets"]}
        assert families == {socket.AF_INET, socket.AF_INET6}
    finally:
        listener.close()


def test_close_releases_the_port_for_the_next_bring_up(helpers: None) -> None:
    port = _a_free_port()
    first = embedded_listen.DirectListener(BIND_HOST_ALL, port)
    first.bind()
    first.close()
    second = embedded_listen.DirectListener(BIND_HOST_ALL, port)
    try:
        assert second.bind()["sockets"]
    finally:
        second.close()


def test_loopback_choice_is_unchanged(helpers: None) -> None:
    listener = embedded_listen.DirectListener(BIND_HOST_LOOPBACK, _a_free_port())
    app = object()
    assert listener.wrap(app) == (app, True)
    assert listener.bind() == {}


def test_older_bundled_server_keeps_ipv4_bind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ha_mcp.listen_sockets", None)
    monkeypatch.setitem(sys.modules, "ha_mcp.ipv6_lan_guard", None)
    listener = embedded_listen.DirectListener(BIND_HOST_ALL, _a_free_port())
    app = object()
    assert listener.wrap(app) == (app, True)
    assert listener.bind() == {}
