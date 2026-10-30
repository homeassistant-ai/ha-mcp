"""The IPv4 wildcard bind host also accepts IPv6 clients (issue #2915)."""

from __future__ import annotations

import errno
import importlib.util
import logging
import socket
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _connects(address: str, port: int) -> bool:
    try:
        socket.create_connection((address, port), timeout=2).close()
    except OSError:
        return False
    return True


def test_wildcard_host_accepts_ipv4_and_ipv6_clients() -> None:
    _require_ipv6_loopback()
    port = _free_port()
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
        taken.bind(("0.0.0.0", 0))
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
    real_socket = socket.socket

    class _NoIPv6Socket(real_socket):
        def __init__(self, family: int = -1, *args: Any, **kwargs: Any) -> None:
            if failure == "socket" and family == socket.AF_INET6:
                raise OSError(errno.EAFNOSUPPORT, "Address family not supported")
            super().__init__(family, *args, **kwargs)

        def bind(self, address: Any) -> None:
            if failure == "bind" and self.family == socket.AF_INET6:
                raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
            super().bind(address)

    monkeypatch.setattr(listen_sockets.socket, "socket", _NoIPv6Socket)
    port = _free_port()
    sockets = listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    assert sockets is not None
    try:
        assert [sock.family for sock in sockets] == [socket.AF_INET]
        sockets[0].listen()
        assert _connects("127.0.0.1", port)
    finally:
        for sock in sockets:
            sock.close()


def test_startup_line_logged_once_serving(caplog: pytest.LogCaptureFixture) -> None:
    port = _free_port()
    sockets = listen_sockets.wildcard_listen_sockets("0.0.0.0", port)
    assert sockets is not None
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    try:
        listen_sockets.announce_when_listening(sockets, "0.0.0.0", port)
        time.sleep(0.3)
        assert "Uvicorn running" not in caplog.text, "logged before serving"
        for sock in sockets:
            sock.listen()
        deadline = time.monotonic() + 5
        while "Uvicorn running" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.05)
        assert f"Uvicorn running on http://0.0.0.0:{port} (Press CTRL+C to quit)" in (
            caplog.text
        )
    finally:
        for sock in sockets:
            sock.close()


def test_specific_host_is_left_to_uvicorn() -> None:
    assert listen_sockets.wildcard_listen_sockets("127.0.0.1", _free_port()) is None


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
            for sock in kwargs["sockets"]:
                sock.close()

    assert start._run_mcp_server(_Mcp(), "0.0.0.0", _free_port(), "/p", {}) == 0
    assert seen["host"] == "0.0.0.0"
    # IPv6 is only ever bound behind the local-network guard (#2915).
    assert [m.cls.__name__ for m in seen["middleware"]] == ["IPv6LanGuard"]
    assert seen["uvicorn_config"]["proxy_headers"] is False
    families = {sock.family for sock in seen["sockets"]}
    assert families == {socket.AF_INET, socket.AF_INET6}
