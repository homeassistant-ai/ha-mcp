"""Unit tests for the pinned sidecar-port feature (#1587).

Covers ``_pick_free_port(pinned)`` (ephemeral default, pin success, busy
fallback) and the lenient ``sidecar_pin_port`` Settings validator.
"""

import socket

import pytest

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.config import Settings


def _a_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestPickFreePort:
    def test_ephemeral_when_unpinned(self) -> None:
        port = sidecar._pick_free_port()
        assert 0 < port <= 65535

    def test_ephemeral_when_pinned_zero(self) -> None:
        port = sidecar._pick_free_port(0)
        assert 0 < port <= 65535

    def test_returns_pinned_port_when_available(self) -> None:
        pinned = _a_free_port()
        assert sidecar._pick_free_port(pinned) == pinned

    def test_falls_back_to_ephemeral_when_pinned_busy(self, caplog) -> None:
        # Hold a listening socket on the target port so the pinned bind
        # fails (SO_REUSEADDR does not let a second socket join an active
        # listener) and the helper must fall back.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            busy_port = busy.getsockname()[1]

            with caplog.at_level("WARNING"):
                got = sidecar._pick_free_port(busy_port)

        assert got != busy_port
        assert 0 < got <= 65535
        assert any("falling back" in r.message.lower() for r in caplog.records)


class TestSidecarPinPortValidator:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", 0),  # explicit off
            ("8099", 8099),  # valid pinned
            ("1024", 1024),  # lower bound
            ("65535", 65535),  # upper bound
            ("80", 0),  # privileged -> off
            ("1023", 0),  # just below range -> off
            ("70000", 0),  # above range -> off
            ("-1", 0),  # negative -> off
            ("abc", 0),  # unparseable -> off
            ("", 0),  # empty -> off
        ],
    )
    def test_env_values_clamp_to_zero_or_valid(
        self, monkeypatch, raw: str, expected: int
    ) -> None:
        monkeypatch.setenv("HA_MCP_SIDECAR_PORT", raw)
        assert Settings().sidecar_pin_port == expected

    def test_default_is_zero(self, monkeypatch) -> None:
        monkeypatch.delenv("HA_MCP_SIDECAR_PORT", raising=False)
        assert Settings().sidecar_pin_port == 0
