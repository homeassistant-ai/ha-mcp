"""Unit tests for the settings UI config persistence and tool visibility."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

from ha_mcp.settings_ui import (
    FEATURE_GATED_TOOLS,
    MANDATORY_TOOLS,
    _get_config_path,
    apply_tool_visibility,
    load_tool_config,
    register_settings_routes,
    save_tool_config,
)

SaveHandler = Callable[[Request], Awaitable[JSONResponse]]


class TestConfigPersistence:
    """Test load/save of tool_config.json."""

    def test_save_and_load(self, tmp_path: Path):
        config = {"tools": {"ha_hacs_info": "disabled", "ha_restart": "pinned"}}
        config_path = tmp_path / "tool_config.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            save_tool_config(config)
            loaded = load_tool_config()
        assert loaded == config

    def test_load_missing_file(self, tmp_path: Path):
        config_path = tmp_path / "nonexistent.json"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            assert load_tool_config() == {}

    def test_load_corrupt_file(self, tmp_path: Path):
        config_path = tmp_path / "corrupt.json"
        config_path.write_text("not json {{{")
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            assert load_tool_config() == {}

    def test_seed_from_env_vars(self, tmp_path: Path):
        config_path = tmp_path / "tool_config.json"
        settings = MagicMock()
        settings.disabled_tools = "ha_hacs_info,ha_hacs_download"
        settings.pinned_tools = "ha_restart"
        with patch("ha_mcp.settings_ui._get_config_path", return_value=config_path):
            config = load_tool_config(settings)
        assert config["tools"]["ha_hacs_info"] == "disabled"
        assert config["tools"]["ha_hacs_download"] == "disabled"
        assert config["tools"]["ha_restart"] == "pinned"
        assert config_path.exists()


class TestApplyToolVisibility:
    """Test apply_tool_visibility logic."""

    def test_disables_tools(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_hacs_info": "disabled", "ha_restart": "enabled"}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_hacs_info" in disabled_names
        assert "ha_restart" not in disabled_names

    def test_mandatory_tools_not_disabled(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": dict.fromkeys(MANDATORY_TOOLS, "disabled")}
        apply_tool_visibility(mcp, config, settings)
        if mcp.disable.called:
            disabled_names = mcp.disable.call_args[1]["names"]
            for name in MANDATORY_TOOLS:
                assert name not in disabled_names

    def test_yaml_editing_off_disables_tool(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = False
        config = {"tools": {}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_config_set_yaml" in disabled_names

    def test_yaml_editing_on_does_not_disable_tool(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {}}
        apply_tool_visibility(mcp, config, settings)
        if mcp.disable.called:
            disabled_names = mcp.disable.call_args[1]["names"]
            assert "ha_config_set_yaml" not in disabled_names

    def test_yaml_editing_on_but_ui_disabled_keeps_tool_disabled(self):
        # AND semantics: even when the safety toggle is on, a UI-saved
        # "disabled" state must be respected. (Regression guard for
        # Patch76 G9.2 — the previous behavior force-enabled the tool
        # whenever the safety toggle was on, overriding the UI choice.)
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_config_set_yaml": "disabled"}}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_called_once()
        disabled_names = mcp.disable.call_args[1]["names"]
        assert "ha_config_set_yaml" in disabled_names

    def test_returns_pinned_names(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {"tools": {"ha_restart": "pinned", "ha_hacs_info": "enabled"}}
        pinned = apply_tool_visibility(mcp, config, settings)
        assert "ha_restart" in pinned
        assert "ha_hacs_info" not in pinned

    def test_empty_config_no_disable(self):
        mcp = MagicMock()
        settings = MagicMock()
        settings.enable_yaml_config_editing = True
        config = {}
        apply_tool_visibility(mcp, config, settings)
        mcp.disable.assert_not_called()


class TestConfigPath:
    """Test _get_config_path uses SUPERVISOR_TOKEN, not /data heuristic."""

    def test_addon_path_when_supervisor_token_set(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        assert _get_config_path() == Path("/data/tool_config.json")

    def test_home_path_when_no_supervisor_token(self, monkeypatch, tmp_path):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        result = _get_config_path()
        assert result == tmp_path / ".ha-mcp" / "tool_config.json"
        assert (tmp_path / ".ha-mcp").is_dir()


class TestFeatureGatedTools:
    """Test the FEATURE_GATED_TOOLS dict aligns with the beta tag system."""

    def test_install_mcp_tools_is_gated(self):
        # Patch76 G7: ha_install_mcp_tools must appear as a stub when its
        # feature flag is off; otherwise users have no way to discover the
        # tool exists.
        assert "ha_install_mcp_tools" in FEATURE_GATED_TOOLS
        assert FEATURE_GATED_TOOLS["ha_install_mcp_tools"]["disabled_by"] == (
            "enable_custom_component_integration"
        )

    def test_filesystem_tools_use_addon_option_name(self):
        # disabled_by should reference the dev addon option name (matches
        # how the JS renders "set <code>{disabled_by}</code> in the dev
        # add-on config or the matching env var (see docs/beta.md)").
        for name in ("ha_list_files", "ha_read_file", "ha_write_file", "ha_delete_file"):
            assert FEATURE_GATED_TOOLS[name]["disabled_by"] == "enable_filesystem_tools"


class TestRouteRegistration:
    """Test register_settings_routes mounting under secret_path (Patch76 G1)."""

    def _collect_paths(self, mcp):
        return [call.args[0] for call in mcp.custom_route.call_args_list]

    def test_registers_root_in_addon_mode(self, monkeypatch):
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="/private_x")
        paths = self._collect_paths(mcp)
        # Root for ingress + secret-prefixed for direct port access
        assert "/" in paths
        assert "/settings" in paths
        assert "/private_x/settings" in paths
        assert "/private_x/api/settings/tools" in paths

    def test_secret_path_only_when_not_addon(self, monkeypatch):
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="/mcp")
        paths = self._collect_paths(mcp)
        # No root mount in Docker/standalone — only the secret-prefixed routes
        assert "/" not in paths
        assert "/settings" not in paths
        assert "/mcp/settings" in paths
        assert "/mcp/api/settings/tools" in paths

    def test_no_routes_when_no_addon_and_no_secret(self, monkeypatch):
        # Refuse to mount publicly: no auth → no routes.
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        mcp = MagicMock()
        mcp.custom_route = MagicMock(return_value=lambda fn: fn)
        register_settings_routes(mcp, MagicMock(), secret_path="")
        assert mcp.custom_route.call_count == 0


class TestSaveToolsValidation:
    """Test POST /api/settings/tools handler validation (Patch76 G3)."""

    def _make_request(self, body):
        request = MagicMock()
        request.json = AsyncMock(return_value=body)
        return request

    def _capture_handler(self, monkeypatch) -> SaveHandler:
        # Capture the _save_tools handler that register_settings_routes
        # mounts so we can call it directly instead of going through HTTP.
        monkeypatch.setenv("SUPERVISOR_TOKEN", "fake")
        captured: dict[str, Any] = {}

        def custom_route_factory(path, methods):
            def decorator(fn):
                if path == "/api/settings/tools" and "POST" in methods:
                    captured["save"] = fn
                return fn
            return decorator

        mcp = MagicMock()
        mcp.custom_route = MagicMock(side_effect=custom_route_factory)
        register_settings_routes(mcp, MagicMock(), secret_path="/x")
        return captured["save"]

    @pytest.mark.asyncio
    async def test_rejects_non_dict_body_array(self, monkeypatch, tmp_path):
        # Patch76 G3: a JSON array body would AttributeError on body.get
        # → 500. Must be a structured 400 instead.
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request([1, 2, 3]))
        assert resp.status_code == 400
        body = json.loads(resp.body)
        assert body["success"] is False

    @pytest.mark.asyncio
    async def test_rejects_non_dict_body_null(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request(None))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_rejects_non_dict_states(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "ha_mcp.settings_ui._get_config_path",
            lambda: tmp_path / "tool_config.json",
        )
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request({"states": "not-a-dict"}))
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_drops_garbage_state_values(self, monkeypatch, tmp_path):
        config_path = tmp_path / "tool_config.json"
        monkeypatch.setattr("ha_mcp.settings_ui._get_config_path", lambda: config_path)
        save = self._capture_handler(monkeypatch)
        resp = await save(self._make_request({
            "states": {
                "ha_good_tool": "disabled",
                "ha_bad_value": "not_a_real_state",
                42: "disabled",  # non-string key
            },
        }))
        assert resp.status_code == 200
        saved = json.loads(config_path.read_text())
        assert saved["tools"] == {"ha_good_tool": "disabled"}
