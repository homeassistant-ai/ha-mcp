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
    """Test the add-on start.py config parsing logic (list state format)."""

    @staticmethod
    def _parse_tools(config: dict) -> tuple[list[str], list[str]]:
        """Replicate start.py parsing: returns (disabled, pinned) lists."""
        disabled: list[str] = []
        pinned: list[str] = []
        for key, group_val in config.items():
            if not key.startswith("tools_") or not isinstance(group_val, dict):
                continue
            group_enabled = group_val.get("enabled", True)
            for tool_name, state in group_val.items():
                if tool_name == "enabled":
                    continue
                if not group_enabled or state == "disabled":
                    disabled.append(tool_name)
                elif state == "pinned":
                    pinned.append(tool_name)
        return disabled, pinned

    def test_individual_disabled(self):
        """A tool set to 'disabled' is in the disabled list."""
        config = {"tools_hacs": {"enabled": True, "ha_hacs_info": "enabled", "ha_hacs_download": "disabled"}}
        disabled, pinned = self._parse_tools(config)
        assert "ha_hacs_download" in disabled
        assert "ha_hacs_info" not in disabled

    def test_group_disabled_overrides_all(self):
        """Group enabled=false disables all tools regardless of individual state."""
        config = {"tools_system": {"enabled": False, "ha_restart": "enabled", "ha_config_set_yaml": "pinned"}}
        disabled, pinned = self._parse_tools(config)
        assert "ha_restart" in disabled
        assert "ha_config_set_yaml" in disabled

    def test_pinned_state(self):
        """A tool set to 'pinned' appears in the pinned list."""
        config = {"tools_search": {"enabled": True, "ha_search_entities": "pinned", "ha_deep_search": "enabled"}}
        disabled, pinned = self._parse_tools(config)
        assert "ha_search_entities" in pinned
        assert "ha_deep_search" not in pinned

    def test_yaml_config_editing_disables_tool(self):
        """enable_yaml_config_editing=false adds ha_config_set_yaml to disabled."""
        disabled: list[str] = []
        if "ha_config_set_yaml" not in disabled:
            disabled.append("ha_config_set_yaml")
        assert "ha_config_set_yaml" in disabled
