"""Unit tests for the settings UI config persistence and tool visibility."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ha_mcp.settings_ui import (
    MANDATORY_TOOLS,
    apply_tool_visibility,
    load_tool_config,
    save_tool_config,
)


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
        config = {"tools": {name: "disabled" for name in MANDATORY_TOOLS}}
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
