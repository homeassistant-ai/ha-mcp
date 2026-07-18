"""Unit tests for the OAuth/OIDC dedicated settings-UI secret path.

GHSA-mx64-982r-65vg: in OAuth and OIDC modes (internet-facing by design) the
settings UI must never be mounted at the guessable default MCP path, where
FastMCP custom routes bypass the OAuth/OIDC auth middleware. Instead it gets a
dedicated secret path, and that path is never advertised back to MCP clients.

These tests pin the resolution helper (`_resolve_settings_secret_path`) and the
registration helper (`_register_settings_ui_secret_path`) in `ha_mcp.__main__`.
"""

import logging
from unittest.mock import MagicMock

import pytest

from ha_mcp.__main__ import (
    _register_settings_ui_secret_path,
    _resolve_settings_secret_path,
    _settings_ui_disabled,
)
from ha_mcp.settings_ui import get_http_settings_prefix, is_http_settings_mounted


@pytest.fixture(autouse=True)
def _reset_settings_globals():
    # _http_settings_prefix / _http_settings_mounted are process-global; isolate.
    from ha_mcp import settings_ui as _su

    saved_prefix = _su._http_settings_prefix
    saved_mounted = _su._http_settings_mounted
    _su._http_settings_prefix = None
    _su._http_settings_mounted = False
    yield
    _su._http_settings_prefix = saved_prefix
    _su._http_settings_mounted = saved_mounted


class TestSettingsUiDisabled:
    """The kill switch is honored uniformly across HTTP transports (standard
    ha-mcp-web/ha-mcp-sse, OAuth, OIDC) via this shared predicate."""

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_truthy_disables(self, monkeypatch, val):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", val)
        assert _settings_ui_disabled() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  "])
    def test_falsy_or_unset_keeps_enabled(self, monkeypatch, val):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", val)
        assert _settings_ui_disabled() is False

    def test_unset_keeps_enabled(self, monkeypatch):
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        assert _settings_ui_disabled() is False

    def test_unrecognized_keeps_enabled_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "disable")
        with caplog.at_level(logging.WARNING):
            assert _settings_ui_disabled() is False
        assert any("not a recognized" in r.getMessage() for r in caplog.records)


class TestResolveSettingsSecretPath:
    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", " on ", "True"])
    def test_truthy_values_disable(self, monkeypatch, val):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", val)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        assert _resolve_settings_secret_path() is None

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "  "])
    def test_falsy_values_keep_enabled(self, monkeypatch, val):
        # The security-relevant direction: a falsy value must NOT disable the UI.
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", val)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        path = _resolve_settings_secret_path()
        assert path is not None and path.startswith("/private_")

    def test_unrecognized_value_keeps_enabled_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "disabled")
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        with caplog.at_level(logging.WARNING):
            path = _resolve_settings_secret_path()
        assert path is not None and path.startswith("/private_")
        assert any("not a recognized" in r.getMessage() for r in caplog.records)

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
        assert len(path) > len("/private_") + 15

    def test_autogen_paths_are_unique(self, monkeypatch):
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        assert _resolve_settings_secret_path() != _resolve_settings_secret_path()

    def test_disable_beats_explicit(self, monkeypatch):
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "true")
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/private_custom")
        assert _resolve_settings_secret_path() is None


class TestRegisterSettingsUiSecretPath:
    def _mk(self):
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        return mcp

    def _paths(self, mcp):
        return [c.args[0] for c in mcp.custom_route.call_args_list]

    def test_autogen_mounts_but_never_advertises(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.delenv("MCP_SETTINGS_SECRET_PATH", raising=False)
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        paths = self._paths(mcp)
        assert any(p.startswith("/private_") and p.endswith("/settings") for p in paths)
        assert "/mcp/api/settings/advanced" not in paths
        assert "/settings" not in paths
        # Path hidden from MCP clients, but the HTTP-mounted flag is still set so
        # the settings page does not mistake itself for the stdio sidecar.
        assert get_http_settings_prefix() is None
        assert is_http_settings_mounted() is True

    def test_explicit_path_mounted_not_advertised(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/private_fixed")
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        paths = self._paths(mcp)
        assert "/private_fixed/settings" in paths
        assert "/private_fixed/api/settings/tools" in paths
        assert get_http_settings_prefix() is None
        assert is_http_settings_mounted() is True

    def test_disabled_registers_nothing(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setenv("HA_MCP_DISABLE_SETTINGS_UI", "1")
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        assert mcp.custom_route.call_count == 0
        assert get_http_settings_prefix() is None
        assert is_http_settings_mounted() is False

    def test_collision_with_mcp_path_is_rejected(self, monkeypatch):
        # GHSA-mx64: reusing the client-known MCP path must not re-expose settings.
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/mcp")
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        assert mcp.custom_route.call_count == 0
        assert is_http_settings_mounted() is False

    def test_collision_ignores_trailing_slash(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/mcp/")
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        assert mcp.custom_route.call_count == 0

    def test_missing_leading_slash_is_normalized(self, monkeypatch):
        # Starlette asserts routes start with "/", so a slash-less explicit value
        # must be normalized, not mounted verbatim (which would crash startup).
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "private_fixed")
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        paths = self._paths(mcp)
        assert "/private_fixed/settings" in paths
        assert not any(p.startswith("private_fixed") for p in paths)

    def test_empty_after_normalization_is_rejected(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_DISABLE_SETTINGS_UI", raising=False)
        monkeypatch.setenv("MCP_SETTINGS_SECRET_PATH", "/")
        mcp = self._mk()

        _register_settings_ui_secret_path(mcp, MagicMock(), "0.0.0.0", 8086, "/mcp")

        assert mcp.custom_route.call_count == 0
        assert is_http_settings_mounted() is False
