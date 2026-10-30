"""The IPv4 wildcard bind host also accepts IPv6 clients (issue #2915)."""

from __future__ import annotations

import errno
import importlib.util
import logging
import socket
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from .test_sidecar_pin_port import _a_free_port

_REPO = Path(__file__).parents[3]


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


listen_sockets = _load(_REPO / "src/ha_mcp/listen_sockets.py", "listen_sockets_ut")


def _require_ipv6_loopback() -> None:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as sock:
            sock.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback unavailable on this machine")


def _connects(address: str, port: int) -> bool:
    try:
        socket.create_connection((address, port), timeout=2).close()
    except OSError:
        return False
    return True


def test_wildcard_host_accepts_ipv4_and_ipv6_clients() -> None:
    _require_ipv6_loopback()
    port = _a_free_port()
    sockets = listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    assert sockets is not None
    try:
        for sock in sockets:
            sock.listen()
        assert _connects("127.0.0.1", port)
        assert _connects("::1", port)
    finally:
        for sock in sockets:
            sock.close()


def test_port_in_use_is_reported_not_skipped() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        with pytest.raises(OSError):
            listen_sockets.wildcard_listen_sockets("0.0.0.0", taken.getsockname()[1])


@pytest.mark.parametrize(
    "failure",
    ["socket", "bind"],
    ids=["ipv6-unsupported", "ipv6-address-unavailable"],
)
def test_machine_without_ipv6_still_serves_ipv4(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    class _NoIPv6Socket(listen_sockets._ListenSocket):
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            if failure == "socket" and family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported")
            super().__init__(family, *args, **kwargs)

        def bind(self, address: Any) -> None:
            if failure == "bind" and self.family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
            super().bind(address)

    monkeypatch.setattr(listen_sockets, "_ListenSocket", _NoIPv6Socket)
    port = _a_free_port()
    sockets = listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    assert sockets is not None
    try:
        assert [sock.family for sock in sockets] == [socket.AF_INET]
        sockets[0].listen()
        assert _connects("127.0.0.1", port)
    finally:
        for sock in sockets:
            sock.close()


def test_startup_line_logged_when_every_socket_serves(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _require_ipv6_loopback()
    port = _a_free_port()
    sockets = listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    assert sockets is not None and len(sockets) == 2
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    line = f"Uvicorn running on http://0.0.0.0:{port} (Press CTRL+C to quit)"
    try:
        listen_sockets.announce_when_listening(sockets, "0.0.0.0", port)
        sockets[0].listen()
        assert line not in caplog.text, "logged before every socket served"
        sockets[1].listen()
        assert caplog.text.count(line) == 1
    finally:
        for sock in sockets:
            sock.close()


def test_specific_host_is_left_to_uvicorn() -> None:
    assert listen_sockets.wildcard_listen_sockets("127.0.0.1", _a_free_port()) is None


def test_app_hands_dual_stack_sockets_to_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_ipv6_loopback()
    monkeypatch.setitem(sys.modules, "ha_mcp.listen_sockets", listen_sockets)
    start = _load(_REPO / "homeassistant-addon/start.py", "addon_start_ut")
    seen: dict[str, Any] = {}

    class _Mcp:
        def run(self, **kwargs: Any) -> None:
            seen.update(kwargs)
            for sock in kwargs["sockets"] or []:
                sock.close()

    assert start._run_mcp_server(_Mcp(), "0.0.0.0", _a_free_port(), "/p", {}) == 0
    assert seen["host"] == "0.0.0.0"
    # IPv6 is only ever bound behind the local-network guard (#2915).
    assert [m.cls.__name__ for m in seen["middleware"]] == ["IPv6LanGuard"]
    assert seen["uvicorn_config"]["proxy_headers"] is False
    families = {sock.family for sock in seen["sockets"]}
    assert families == {socket.AF_INET, socket.AF_INET6}


def test_app_with_a_specific_host_keeps_uvicorns_own_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An MCP_HOST override binds exactly that host, unguarded, with uvicorn's
    # proxy-header handling intact for a reverse proxy in front of it.
    monkeypatch.setitem(sys.modules, "ha_mcp.listen_sockets", listen_sockets)
    start = _load(_REPO / "homeassistant-addon/start.py", "addon_start_ut2")
    seen: dict[str, Any] = {}

    class _Mcp:
        def run(self, **kwargs: Any) -> None:
            seen.update(kwargs)

    assert start._run_mcp_server(_Mcp(), "127.0.0.1", _a_free_port(), "/p", {}) == 0
    assert seen["sockets"] is None
    assert "middleware" not in seen
    assert "proxy_headers" not in seen["uvicorn_config"]


def test_a_failed_family_releases_the_one_already_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_ipv6_loopback()

    class _IPv6Refused(listen_sockets._ListenSocket):
        def bind(self, address: Any) -> None:
            if self.family == socket.AF_INET6:
                raise OSError(errno.EACCES, "Permission denied")
            super().bind(address)

    monkeypatch.setattr(listen_sockets, "_ListenSocket", _IPv6Refused)
    port = _a_free_port()
    with pytest.raises(OSError):
        listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    # The IPv4 socket bound before IPv6 failed must be closed again: a plain
    # bind on its loopback succeeds only then.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_port_held_on_ipv6_only_keeps_the_ipv4_listener(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Before IPv6 was added the server bound IPv4 alone; another process
    # holding the port on IPv6 must not stop it from starting.
    _require_ipv6_loopback()
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as taken:
        taken.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        taken.bind(("::1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        sockets = listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    assert sockets is not None
    try:
        assert [sock.family for sock in sockets] == [socket.AF_INET]
        # The family is set at creation; serving proves the socket really bound.
        sockets[0].listen()
        assert _connects("127.0.0.1", port)
        assert "already in use on IPv6" in caplog.text
    finally:
        for sock in sockets:
            sock.close()
