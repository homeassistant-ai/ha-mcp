"""
Tools registry for Smart MCP Server - manages registration of all MCP tools.

This module acts as an orchestrator, importing and coordinating tool registration
from specialized modules.
"""

from typing import Any, cast

from .backup import register_backup_tools
from .tools_config_automations import register_config_automation_tools
from .tools_config_helpers import register_config_helper_tools
from .tools_config_scripts import register_config_script_tools
from .tools_search import register_search_tools
from .tools_service import register_service_tools
from .tools_utility import register_utility_tools


class ToolsRegistry:
    """Manages registration of all MCP tools for the smart server."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.client = server.client
        self.mcp = server.mcp
        self.smart_tools = server.smart_tools
        self.device_tools = server.device_tools
        self.convenience_tools = server.convenience_tools

    def register_all_tools(self) -> None:
        """Register all tools with the MCP server."""
        # Register search and discovery tools
        register_search_tools(
            self.mcp, self.client, self.smart_tools
        )

        # Register service call and operation monitoring tools
        register_service_tools(
            self.mcp, self.client, self.device_tools
        )

        # Register config management tools (helpers, scripts, automations)
        register_config_helper_tools(self.mcp, self.client)
        register_config_script_tools(self.mcp, self.client)
        register_config_automation_tools(self.mcp, self.client)

        # Register utility tools (logbook, templates, docs)
        register_utility_tools(self.mcp, self.client)

        # Register backup tools
        register_backup_tools(self.mcp, self.client)

        # Register convenience delegator tools
        self._register_convenience_delegators()

    def _register_convenience_delegators(self) -> None:
        """Register simple convenience delegator tools for scenes, weather, and energy."""

        @self.mcp.tool
        async def ha_activate_scene(scene_name: str) -> dict[str, Any]:
            """Activate a Home Assistant scene by name or entity ID."""
            result = await self.convenience_tools.activate_scene(scene_name=scene_name)
            return cast(dict[str, Any], result)

        @self.mcp.tool
        async def ha_get_weather(location: str | None = None) -> dict[str, Any]:
            """Get current weather information from Home Assistant weather entities."""
            result = await self.convenience_tools.get_weather_info(location=location)
            return cast(dict[str, Any], result)

        @self.mcp.tool
        async def ha_get_energy(period: str = "today") -> dict[str, Any]:
            """Get energy usage information from Home Assistant energy monitoring."""
            result = await self.convenience_tools.get_energy_usage(period=period)
            return cast(dict[str, Any], result)
