"""Unit tests for the HAOS bake's HTTP-port discovery.

HA 2026.8 changed the Supervisor-managed default HTTP port from 8123 to 80
("Supervisor fronts Core on the standard HTTP port"). The bake boots a
FRESH HAOS before any seeded config exists, so it hits that new default and
must not assume either port — a bake that waits on 8123 alone burns its
whole budget and fails before discovery can look at 80, which is exactly
how every HAOS lane broke on release day.

Both helpers run only on an image-cache MISS, so without unit cover a
regression stays invisible until the cache key rotates — long after the
change that caused it. Mirrors the existing helper-level tests in
test_haos_supervisor_wait.py.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "tests" / "haos_image_build"))

import build_image  # noqa: E402


class _FakeSocket:
    """Stand-in for socket.socket that accepts only ``open_ports``."""

    def __init__(self, open_ports: set[int], attempts: list[int]) -> None:
        self._open = open_ports
        self._attempts = attempts

    def __call__(self, *_args, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def settimeout(self, _timeout):
        return None

    def connect(self, address):
        _host, port = address
        self._attempts.append(port)
        if port not in self._open:
            raise OSError("connection refused")


class TestWaitAnyPort:
    def test_returns_the_alt_forward_when_only_port_80_answers(self, monkeypatch):
        """The 2026.8 shape: Core serves 80, the 8123 forward never opens."""
        attempts: list[int] = []
        monkeypatch.setattr(
            build_image.socket,
            "socket",
            _FakeSocket({build_image.HA_ALT_HOST_PORT}, attempts),
        )

        opened = build_image._wait_any_port(
            (build_image.HA_HOST_PORT, build_image.HA_ALT_HOST_PORT), timeout=5
        )

        assert opened == build_image.HA_ALT_HOST_PORT
        assert build_image.HA_HOST_PORT in attempts, "both forwards must be probed"

    def test_returns_the_primary_forward_when_it_answers(self, monkeypatch):
        attempts: list[int] = []
        monkeypatch.setattr(
            build_image.socket,
            "socket",
            _FakeSocket({build_image.HA_HOST_PORT}, attempts),
        )

        opened = build_image._wait_any_port(
            (build_image.HA_HOST_PORT, build_image.HA_ALT_HOST_PORT), timeout=5
        )

        assert opened == build_image.HA_HOST_PORT

    def test_names_both_forwards_when_neither_opens(self, monkeypatch):
        attempts: list[int] = []
        monkeypatch.setattr(build_image.socket, "socket", _FakeSocket(set(), attempts))
        monkeypatch.setattr(build_image.time, "sleep", lambda _s: None)

        with pytest.raises(TimeoutError) as excinfo:
            build_image._wait_any_port(
                (build_image.HA_HOST_PORT, build_image.HA_ALT_HOST_PORT),
                timeout=0.1,
            )

        message = str(excinfo.value)
        assert str(build_image.HA_HOST_PORT) in message
        assert str(build_image.HA_ALT_HOST_PORT) in message


class TestDiscoverHaBaseUrl:
    def _patch_urlopen(self, monkeypatch, serving_url: str | None):
        class _Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        def fake_urlopen(url, timeout=None):
            if serving_url is not None and url.startswith(serving_url):
                return _Response()
            raise OSError("connection refused")

        monkeypatch.setattr(build_image.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(build_image.time, "sleep", lambda _s: None)

    def test_locks_onto_the_port_80_forward(self, monkeypatch):
        alt = f"http://127.0.0.1:{build_image.HA_ALT_HOST_PORT}"
        self._patch_urlopen(monkeypatch, alt)

        assert build_image._discover_ha_base_url(timeout=5) == alt

    def test_locks_onto_the_8123_forward(self, monkeypatch):
        primary = f"http://127.0.0.1:{build_image.HA_HOST_PORT}"
        self._patch_urlopen(monkeypatch, primary)

        assert build_image._discover_ha_base_url(timeout=5) == primary

    def test_raises_naming_both_candidates_when_nothing_answers(self, monkeypatch):
        self._patch_urlopen(monkeypatch, None)

        with pytest.raises(TimeoutError) as excinfo:
            build_image._discover_ha_base_url(timeout=0.1)

        message = str(excinfo.value)
        assert str(build_image.HA_HOST_PORT) in message
        assert str(build_image.HA_ALT_HOST_PORT) in message


def test_socket_module_is_still_imported_by_the_bake():
    """Guard the monkeypatch targets above against an import refactor."""
    assert build_image.socket is socket
