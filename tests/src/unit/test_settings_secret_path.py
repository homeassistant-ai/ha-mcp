"""Unit tests for the OAuth/OIDC dedicated settings-UI secret path.

GHSA-mx64-982r-65vg: in OAuth and OIDC modes (internet-facing by design) the
settings UI must never be mounted at the guessable default MCP path, where
FastMCP custom routes bypass the OAuth/OIDC auth middleware. Instead it gets a
dedicated secret path, and that path is never advertised back to MCP clients.

These tests pin the resolution helper (`_resolve_settings_secret_path`) and the
registration helper (`_register_settings_ui_secret_path`) in `ha_mcp.__main__`.
"""

from unittest.mock import MagicMock

import pytest

from ha_mcp.__main__ import (
    _register_settings_ui_secret_path,
    _resolve_settings_secret_path,
)
from ha_mcp.settings_ui import get_http_settings_prefix


@pytest.fixture(autouse=True)
def _reset_http_settings_prefix():
    # _http_settings_prefix is process-global; isolate each test.
    from ha_mcp import settings_ui as _su

    saved = _su._http_settings_prefix
    _su._http_settings_prefix = None
    yield
    _su._http_settings_prefix = saved


class TestResolveSettingsSecretPath:
    def test_disabled_returns_none(self, monkeypatch):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        assert _resolve_settings_secret_path() is None

    def test_explicit_path_used(self, monkeypatch):
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/private_custom")
        assert _resolve_settings_secret_path() == "/private_custom"

    def test_autogen_when_unset(self, monkeypatch):
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        path = _resolve_settings_secret_path()
        assert path is not None
        assert path.startswith("/private_")
        # token_urlsafe(16) → ~22 chars of entropy, unguessable.
        assert len(path) > len("/private_") + 15

    def test_autogen_paths_are_unique(self, monkeypatch):
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        assert _resolve_settings_secret_path() != _resolve_settings_secret_path()

    def test_disable_beats_explicit(self, monkeypatch):
        # Hard off wins even if a secret path is also provided.
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "true")
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/private_custom")
        assert _resolve_settings_secret_path() is None


class TestRegisterSettingsUiSecretPath:
    def _paths(self, mcp):
        return [call.args[0] for call in mcp.custom_route.call_args_list]

    def test_autogen_mounts_but_never_advertises(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086)

        paths = self._paths(mcp)
        # Mounted under an auto-generated secret path...
        assert any(p.startswith("/private_") and p.endswith("/settings") for p in paths)
        # ...never at the guessable default MCP path (the vulnerable location)...
        assert "/mcp/api/settings/advanced" not in paths
        # ...and no bare-root mount either.
        assert "/settings" not in paths
        # ...and the secret is never advertised to MCP clients via ha_get_overview.
        assert get_http_settings_prefix() is None

    def test_explicit_path_mounted_not_advertised(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/private_fixed")
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086)

        paths = self._paths(mcp)
        assert "/private_fixed/settings" in paths
        assert "/private_fixed/api/settings/tools" in paths
        assert get_http_settings_prefix() is None

    def test_disabled_registers_nothing(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086)

        assert mcp.custom_route.call_count == 0
        assert get_http_settings_prefix() is None
