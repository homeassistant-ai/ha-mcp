"""Unit tests for the settings-UI URL ``ha-mcp-web`` logs at startup, which
makes the page discoverable for non-add-on installs (issue #1458).

Pure unit tests (no Docker): they start ``ha-mcp-web`` on each kind of bind
host and read the startup line via ``caplog``.
"""

import logging

import pytest

from ._main_web import start_main_web

_LOGGER = "ha_mcp.__main__"


def _startup_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    host: str,
    path: str = "/mcp",
) -> str:
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        start_main_web(
            monkeypatch,
            MCP_HOST=host,
            MCP_PORT="8086",
            MCP_SECRET_PATH=path,
            HA_MCP_DISABLE_SETTINGS_UI=None,
            SUPERVISOR_TOKEN=None,
        )
    return caplog.text


def test_wildcard_host_uses_placeholder_with_note(monkeypatch, caplog):
    """0.0.0.0 (the default bind) is not externally reachable → <host> + note."""
    msg = _startup_log(monkeypatch, caplog, "0.0.0.0")
    assert "http://<host>:8086/mcp/settings" in msg
    assert "substitute this server's address for <host>" in msg


def test_ipv6_wildcard_uses_placeholder(monkeypatch, caplog):
    """The IPv6 wildcard :: is treated the same as 0.0.0.0."""
    assert "http://<host>:8086/mcp/settings" in _startup_log(monkeypatch, caplog, "::")


def test_concrete_host_emitted_without_note(monkeypatch, caplog):
    """An explicit bind host is shown verbatim, with no placeholder note."""
    msg = _startup_log(monkeypatch, caplog, "127.0.0.1")
    assert "http://127.0.0.1:8086/mcp/settings" in msg
    assert "<host>" not in msg


def test_ipv6_literal_host_is_bracketed(monkeypatch, caplog):
    """A concrete IPv6 literal must be bracketed to form a valid URL."""
    assert "http://[::1]:8086/mcp/settings" in _startup_log(monkeypatch, caplog, "::1")


def test_trailing_slash_in_path_collapsed(monkeypatch, caplog):
    """A trailing slash on the secret path must not yield ``//settings``."""
    msg = _startup_log(monkeypatch, caplog, "0.0.0.0", "/mcp/")
    assert "/mcp/settings" in msg
    assert "/mcp//settings" not in msg
