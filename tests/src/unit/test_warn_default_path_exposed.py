"""Tests for ``_warn_if_default_path_exposed`` startup warning.

Predicate: warn iff ``MCP_SECRET_PATH`` is the default ``/mcp`` AND the
bind host is non-loopback. Operators silence the warning by following
either of the two documented hardening levers (bind loopback OR set a
custom high-entropy ``MCP_SECRET_PATH``).
"""

from __future__ import annotations

import logging

import pytest

from ha_mcp.__main__ import _warn_if_default_path_exposed

_WARNING_LOGGER = "ha_mcp.__main__"


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
def test_warns_on_default_path_non_loopback_host(host: str, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_WARNING_LOGGER):
        _warn_if_default_path_exposed(host, 8086, "/mcp")
    records = [r for r in caplog.records if "default MCP_SECRET_PATH" in r.getMessage()]
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
def test_no_warn_on_default_path_loopback_host(host: str, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_WARNING_LOGGER):
        _warn_if_default_path_exposed(host, 8086, "/mcp")
    assert not [
        r for r in caplog.records if "default MCP_SECRET_PATH" in r.getMessage()
    ], f"unexpected warning for loopback host {host!r}"


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "127.0.0.1", "192.168.1.50", "localhost"],
)
def test_no_warn_on_custom_path_any_host(host: str, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_WARNING_LOGGER):
        _warn_if_default_path_exposed(host, 8086, "/private_abc123")
    assert not [
        r for r in caplog.records if "default MCP_SECRET_PATH" in r.getMessage()
    ], f"unexpected warning for custom path on host {host!r}"


def test_warning_text_names_both_hardening_levers(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_WARNING_LOGGER):
        _warn_if_default_path_exposed("0.0.0.0", 8086, "/mcp")
    [record] = [
        r for r in caplog.records if "default MCP_SECRET_PATH" in r.getMessage()
    ]
    msg = record.getMessage()
    assert "MCP_HOST=127.0.0.1" in msg
    assert "MCP_SECRET_PATH" in msg
    assert "SECURITY.md" in msg
