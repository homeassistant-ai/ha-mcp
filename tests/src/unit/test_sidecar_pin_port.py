"""Unit tests for sidecar port acquisition (#1587, #2131).

Covers ``_bind_listener(preferred, source)`` — ephemeral default,
preferred-port success, busy fallback with an accurate busy signal on
every platform — and the lenient ``sidecar_pin_port`` Settings validator.
"""

import contextlib
import socket
import sys

import pytest

from ha_mcp import stdio_settings_sidecar as sidecar
from ha_mcp.config import Settings


def _a_free_port() -> int:
    """Return an available localhost TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class TestBindListener:
    def test_ephemeral_when_no_preference(self) -> None:
        with contextlib.closing(sidecar._bind_listener(0, "Remembered")) as sock:
            assert 0 < sock.getsockname()[1] <= 65535

    def test_binds_preferred_port_when_available(self) -> None:
        preferred = _a_free_port()
        with contextlib.closing(sidecar._bind_listener(preferred, "Pinned")) as sock:
            assert sock.getsockname()[1] == preferred

    def test_falls_back_to_ephemeral_when_preferred_busy(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Busy preferred port → ephemeral fallback, on every platform.

        The serving socket is bound directly (no probe-then-rebind), so
        the busy signal is the bind itself. The busy listener here does
        NOT set SO_REUSEADDR — matching a real sidecar's asyncio
        listener, which never sets it on Windows.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            busy_port = busy.getsockname()[1]

            with (
                caplog.at_level("WARNING"),
                contextlib.closing(
                    sidecar._bind_listener(busy_port, "Remembered")
                ) as sock,
            ):
                got = sock.getsockname()[1]

        assert got != busy_port
        assert 0 < got <= 65535
        assert any(
            "falling back" in r.message.lower() and "Remembered" in r.message
            for r in caplog.records
        )

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows-only SO_REUSEADDR hijack semantics",
    )
    def test_windows_squatter_with_reuseaddr_is_still_detected(self) -> None:
        """A SO_REUSEADDR-setting squatter must not fool the bind.

        Plain SO_REUSEADDR on Windows lets a second socket bind over a
        live listener that also set it — reporting the busy port as
        free, then dying inside uvicorn's real bind after the discovery
        files were already written (#2134 review). SO_EXCLUSIVEADDRUSE
        gives the accurate busy signal.
        """
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
            busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            busy_port = busy.getsockname()[1]

            with contextlib.closing(
                sidecar._bind_listener(busy_port, "Remembered")
            ) as sock:
                assert sock.getsockname()[1] != busy_port


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
