"""
Tools registry for Smart MCP Server - manages registration of all MCP tools.

This module acts as an orchestrator, importing and coordinating tool registration
from specialized modules.
"""

from typing import Any

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
