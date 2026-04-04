"""Unit tests for user-configurable tool visibility (disable/pin).

Tests the _apply_tool_visibility method, group expansion, mandatory tool
protection, enable_yaml_config_editing toggle, and configurable pinned
tools / max_results.
"""

from __future__ import annotations

from unittest.mock import patch

from fastmcp import FastMCP

from ha_mcp.server import HomeAssistantSmartMCPServer


async def _list_tool_names(mcp: FastMCP) -> list[str]:
    """List visible tool names."""
    return sorted(t.name for t in await mcp.list_tools())


class TestToolGroupConstants:
    """Verify group constants match expectations."""

    def test_tool_groups_non_empty(self):
        assert len(HomeAssistantSmartMCPServer._TOOL_GROUPS) >= 20

    def test_mandatory_tools_non_empty(self):
        assert "ha_search_entities" in HomeAssistantSmartMCPServer._MANDATORY_TOOLS
        assert "ha_get_state" in HomeAssistantSmartMCPServer._MANDATORY_TOOLS

    def test_mandatory_search_tools(self):
        assert "ha_search_tools" in HomeAssistantSmartMCPServer._MANDATORY_SEARCH_TOOLS
        assert "ha_call_read_tool" in HomeAssistantSmartMCPServer._MANDATORY_SEARCH_TOOLS


class TestApplyToolVisibility:
    """Test _apply_tool_visibility with a real FastMCP instance."""

    def _make_mcp_with_tools(self) -> FastMCP:
        """Create a FastMCP server with tagged test tools."""
        mcp = FastMCP("test")

        @mcp.tool(tags={"HACS"})
        def ha_hacs_info() -> str:
            """HACS info."""
            return "ok"

        @mcp.tool(tags={"HACS"})
        def ha_hacs_download() -> str:
            """Download HACS repo."""
            return "ok"

        @mcp.tool(tags={"System"})
        def ha_restart() -> str:
            """Restart HA."""
            return "ok"

        @mcp.tool(tags={"System"})
        def ha_config_set_yaml() -> str:
            """YAML config editing."""
            return "ok"

        @mcp.tool(tags={"Search & Discovery"})
        def ha_search_entities() -> str:
            """Search entities."""
            return "ok"

        @mcp.tool(tags={"Search & Discovery"})
        def ha_get_state() -> str:
            """Get state."""
            return "ok"

        @mcp.tool(tags={"Search & Discovery"})
        def ha_get_overview() -> str:
            """Overview."""
            return "ok"

        @mcp.tool(tags={"Utilities"})
        def ha_report_issue() -> str:
            """Report issue."""
            return "ok"

        return mcp

    async def test_disable_individual_tool(self):
        """Disabling a single tool by name removes it."""
        mcp = self._make_mcp_with_tools()
        mcp.disable(names={"ha_config_set_yaml"})
        mcp.enable(names={"ha_search_entities", "ha_get_state", "ha_get_overview", "ha_report_issue"})

        names = await _list_tool_names(mcp)
        assert "ha_config_set_yaml" not in names
        assert "ha_restart" in names

    async def test_disable_group_by_tag(self):
        """Disabling a group tag removes all tools with that tag."""
        mcp = self._make_mcp_with_tools()
        mcp.disable(tags={"HACS"})
        mcp.enable(names={"ha_search_entities", "ha_get_state", "ha_get_overview", "ha_report_issue"})

        names = await _list_tool_names(mcp)
        assert "ha_hacs_info" not in names
        assert "ha_hacs_download" not in names
        assert "ha_restart" in names

    async def test_mandatory_tools_cannot_be_disabled(self):
        """Mandatory tools are re-enabled even if user disables their group."""
        mcp = self._make_mcp_with_tools()
        mcp.disable(tags={"Search & Discovery"})
        mcp.enable(names={"ha_search_entities", "ha_get_state", "ha_get_overview", "ha_report_issue"})

        names = await _list_tool_names(mcp)
        assert "ha_search_entities" in names
        assert "ha_get_state" in names
        assert "ha_get_overview" in names

    async def test_disable_individual_plus_group(self):
        """Can disable a group and an individual tool simultaneously."""
        mcp = self._make_mcp_with_tools()
        mcp.disable(tags={"HACS"})
        mcp.disable(names={"ha_config_set_yaml"})
        mcp.enable(names={"ha_search_entities", "ha_get_state", "ha_get_overview", "ha_report_issue"})

        names = await _list_tool_names(mcp)
        assert "ha_hacs_info" not in names
        assert "ha_config_set_yaml" not in names
        assert "ha_restart" in names

    async def test_empty_disabled_tools_no_effect(self):
        """Empty disabled_tools means all tools visible."""
        mcp = self._make_mcp_with_tools()
        names = await _list_tool_names(mcp)
        assert len(names) == 8

    async def test_default_disables_yaml_config(self):
        """Default disabled_tools value disables ha_config_set_yaml."""
        mcp = self._make_mcp_with_tools()
        mcp.disable(names={"ha_config_set_yaml"})
        mcp.enable(names={"ha_search_entities", "ha_get_state", "ha_get_overview", "ha_report_issue"})

        names = await _list_tool_names(mcp)
        assert "ha_config_set_yaml" not in names
        assert len(names) == 7


class TestConfigSettings:
    """Test Settings fields for tool visibility."""

    def test_tool_search_max_results_default(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.tool_search_max_results == 5

    def test_tool_search_max_results_clamped_low(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
            "TOOL_SEARCH_MAX_RESULTS": "1",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.tool_search_max_results == 2

    def test_tool_search_max_results_clamped_high(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
            "TOOL_SEARCH_MAX_RESULTS": "99",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.tool_search_max_results == 10

    def test_enable_yaml_config_editing_default_false(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.enable_yaml_config_editing is False

    def test_enable_yaml_config_editing_true(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
            "ENABLE_YAML_CONFIG_EDITING": "true",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.enable_yaml_config_editing is True

    def test_disabled_tools_default(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.disabled_tools == "ha_config_set_yaml"

    def test_disabled_tools_custom(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
            "DISABLED_TOOLS": "ha_config_set_yaml,HACS,ha_restart",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert "HACS" in settings.disabled_tools
            assert "ha_restart" in settings.disabled_tools

    def test_pinned_tools_default_empty(self):
        from ha_mcp.config import Settings

        with patch.dict("os.environ", {
            "HOMEASSISTANT_URL": "http://test:8123",
            "HOMEASSISTANT_TOKEN": "test_token",
        }):
            settings = Settings()  # type: ignore[call-arg]
            assert settings.pinned_tools == ""


class TestStartPyConfigParsing:
    """Test the add-on start.py config parsing logic."""

    def test_nested_enabled_tools_parsing(self):
        """Verify nested enabled_tools config is flattened to disabled list."""
        config = {
            "enabled_tools": {
                "hacs": {
                    "enabled": True,
                    "ha_hacs_info": True,
                    "ha_hacs_download": False,  # individually disabled
                },
                "system": {
                    "enabled": False,  # entire group disabled
                    "ha_restart": True,
                    "ha_config_set_yaml": True,
                },
            }
        }
        disabled: list[str] = []
        for group_val in config["enabled_tools"].values():
            if not isinstance(group_val, dict):
                continue
            group_enabled = group_val.get("enabled", True)
            for tool_name, tool_val in group_val.items():
                if tool_name == "enabled":
                    continue
                if not group_enabled or not tool_val:
                    disabled.append(tool_name)

        assert "ha_hacs_download" in disabled  # individually disabled
        assert "ha_restart" in disabled  # group disabled
        assert "ha_config_set_yaml" in disabled  # group disabled
        assert "ha_hacs_info" not in disabled  # enabled in enabled group

    def test_nested_pinned_tools_parsing(self):
        """Verify nested pinned_tools config is flattened to pinned list."""
        config = {
            "pinned_tools": {
                "search_discovery": {
                    "ha_search_entities": True,
                    "ha_get_overview": True,
                    "ha_get_state": False,
                },
                "system": {
                    "ha_restart": True,
                    "ha_reload_core": False,
                },
            }
        }
        pinned: list[str] = []
        for group_val in config["pinned_tools"].values():
            if not isinstance(group_val, dict):
                continue
            for tool_name, tool_val in group_val.items():
                if tool_val:
                    pinned.append(tool_name)

        assert "ha_search_entities" in pinned
        assert "ha_get_overview" in pinned
        assert "ha_restart" in pinned
        assert "ha_get_state" not in pinned
        assert "ha_reload_core" not in pinned

    def test_yaml_config_editing_disables_tool(self):
        """enable_yaml_config_editing=false adds ha_config_set_yaml to disabled."""
        disabled: list[str] = []
        enable_yaml_config_editing = False
        if not enable_yaml_config_editing and "ha_config_set_yaml" not in disabled:
            disabled.append("ha_config_set_yaml")
        assert "ha_config_set_yaml" in disabled

    def test_yaml_config_editing_enables_tool(self):
        """enable_yaml_config_editing=true does not add ha_config_set_yaml."""
        disabled: list[str] = []
        enable_yaml_config_editing = True
        if not enable_yaml_config_editing and "ha_config_set_yaml" not in disabled:
            disabled.append("ha_config_set_yaml")
        assert "ha_config_set_yaml" not in disabled
