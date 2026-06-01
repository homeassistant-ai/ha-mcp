"""Unit tests for ``_log_settings_url`` — the HTTP-startup settings-UI log
line that makes the page discoverable for non-add-on installs (issue #1458).

Pure unit tests (no Docker): they exercise the host-display branches and path
normalization directly via ``caplog``.
"""

import logging

from ha_mcp.__main__ import _log_settings_url

_LOGGER = "ha_mcp.__main__"


def test_wildcard_host_uses_placeholder_with_note(caplog):
    """0.0.0.0 (the default bind) is not externally reachable → <host> + note."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _log_settings_url("0.0.0.0", 8086, "/mcp")
    msg = caplog.text
    assert "http://<host>:8086/mcp/settings" in msg
    assert "substitute this server's address for <host>" in msg


def test_ipv6_wildcard_uses_placeholder(caplog):
    """The IPv6 wildcard :: is treated the same as 0.0.0.0."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _log_settings_url("::", 8086, "/mcp")
    assert "http://<host>:8086/mcp/settings" in caplog.text


def test_concrete_host_emitted_without_note(caplog):
    """An explicit bind host is shown verbatim, with no placeholder note."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _log_settings_url("127.0.0.1", 8086, "/mcp")
    msg = caplog.text
    assert "http://127.0.0.1:8086/mcp/settings" in msg
    assert "<host>" not in msg


def test_ipv6_literal_host_is_bracketed(caplog):
    """A concrete IPv6 literal must be bracketed to form a valid URL."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _log_settings_url("::1", 8086, "/mcp")
    assert "http://[::1]:8086/mcp/settings" in caplog.text


def test_trailing_slash_in_path_collapsed(caplog):
    """A trailing slash on the secret path must not yield ``//settings``."""
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        _log_settings_url("0.0.0.0", 8086, "/mcp/")
    msg = caplog.text
    assert "/mcp/settings" in msg
    assert "/mcp//settings" not in msg
