"""Tests for the default-path startup warning of ``ha-mcp-web``.

Predicate: warn iff the run is **not** containerized AND ``MCP_SECRET_PATH``
is the default ``/mcp`` AND the bind host is non-loopback. The container
exclusion is what keeps the warning from firing on every Docker / add-on
deployment, where an in-container ``0.0.0.0`` bind says nothing about real
exposure (that is set by the ``docker -p`` mapping). Operators on a direct
run silence it by following either documented hardening lever (bind loopback
OR set a custom high-entropy ``MCP_SECRET_PATH``).
"""

from __future__ import annotations

import logging

import pytest

from ._main_web import start_main_web

_WARNING_LOGGER = "ha_mcp.__main__"


def _default_path_warnings(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    host: str,
    path: str | None = None,
) -> list[logging.LogRecord]:
    """The default-path warnings of an ``ha-mcp-web`` start on ``host``/``path``."""
    with caplog.at_level(logging.WARNING, logger=_WARNING_LOGGER):
        start_main_web(
            monkeypatch, MCP_HOST=host, MCP_PORT="8086", MCP_SECRET_PATH=path
        )
    return [r for r in caplog.records if "default MCP_SECRET_PATH" in r.getMessage()]


@pytest.fixture(autouse=True)
def _not_in_container(monkeypatch) -> None:
    """Pin the container check to False for the host/path predicate tests.

    These tests run wherever pytest runs — including inside a container on
    CI — so the container check must be neutralized to keep the host-based
    cases deterministic. Container behavior is exercised against the real
    container signals in ``test_is_running_in_container.py``.
    """
    monkeypatch.setattr("ha_mcp.__main__._is_running_in_container", lambda: False)


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "[::]",
        "192.168.1.50",
        "10.0.0.1",
        "fe80::1",
        "[fe80::1]",
        "::ffff:192.168.1.1",
        "example.invalid",
        "",
        "*",
    ],
)
def test_warns_on_default_path_non_loopback_host(
    host: str, monkeypatch, caplog
) -> None:
    records = _default_path_warnings(monkeypatch, caplog, host)
    assert len(records) >= 1, (
        f"expected warning for non-loopback host {host!r}, got none"
    )
    assert all(r.levelno == logging.WARNING for r in records)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",
        "::1",
        "[::1]",
        "::1%eth0",
        "::ffff:127.0.0.1",
        "localhost",
        "LOCALHOST",
        "ip6-localhost",
    ],
)
def test_no_warn_on_default_path_loopback_host(host: str, monkeypatch, caplog) -> None:
    assert not _default_path_warnings(monkeypatch, caplog, host), (
        f"unexpected warning for loopback host {host!r}"
    )


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "127.0.0.1", "192.168.1.50", "localhost"],
)
def test_no_warn_on_custom_path_any_host(host: str, monkeypatch, caplog) -> None:
    assert not _default_path_warnings(monkeypatch, caplog, host, "/private_abc123"), (
        f"unexpected warning for custom path on host {host!r}"
    )


def test_warning_text_names_both_hardening_levers(monkeypatch, caplog) -> None:
    [record] = _default_path_warnings(monkeypatch, caplog, "0.0.0.0")
    msg = record.getMessage()
    assert "MCP_HOST=127.0.0.1" in msg
    assert "MCP_SECRET_PATH" in msg
    assert "SECURITY.md" in msg
