"""
Tools registry for Smart MCP Server - manages registration of all MCP tools.

This module uses auto-discovery to find and register all tool modules.
Adding a new tools module is simple:
1. Create tools_*.py file with a register_*_tools(mcp, client, **kwargs) function
2. The function will be auto-discovered and called during registration

No changes to this file are needed when adding new tool modules!
"""

import importlib
import logging
import pkgutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Modules that don't follow the tools_*.py naming convention
# These are handled explicitly for backward compatibility
EXPLICIT_MODULES = {
    "backup": "register_backup_tools",
}


class ToolsRegistry:
    """Manages registration of all MCP tools for the smart server."""

    def __init__(self, server: Any) -> None:
        self.server = server
        self.client = server.client
        self.mcp = server.mcp
        self.smart_tools = server.smart_tools
        self.device_tools = server.device_tools

    def register_all_tools(self) -> None:
        """Register all tools with the MCP server using auto-discovery."""
        # Build kwargs with all available dependencies
        kwargs = {
            "smart_tools": self.smart_tools,
            "device_tools": self.device_tools,
        }

        registered_count = 0

        # Auto-discover and register tools_*.py modules
        package_path = Path(__file__).parent
        for module_info in pkgutil.iter_modules([str(package_path)]):
            module_name = module_info.name

            # Skip non-tool modules
            if not module_name.startswith("tools_"):
                continue

            # Skip the registry itself (tools_registry.py is entity/device registry tools)
            # Note: tools_registry.py contains ha_list_devices, etc. - not this file
            try:
                module = importlib.import_module(f".{module_name}", "ha_mcp.tools")

                # Find the register function (convention: register_*_tools)
                register_func = None
                for attr_name in dir(module):
                    if attr_name.startswith("register_") and attr_name.endswith("_tools"):
                        register_func = getattr(module, attr_name)
                        break

                if register_func:
                    register_func(self.mcp, self.client, **kwargs)
                    registered_count += 1
                    logger.debug(f"Registered tools from {module_name}")
                else:
                    logger.warning(
                        f"Module {module_name} has no register_*_tools function"
                    )

            except Exception as e:
                logger.error(f"Failed to register tools from {module_name}: {e}")
                raise

        # Register explicit modules (those not following tools_*.py convention)
        for module_name, func_name in EXPLICIT_MODULES.items():
            try:
                module = importlib.import_module(f".{module_name}", "ha_mcp.tools")
                register_func = getattr(module, func_name)
                register_func(self.mcp, self.client, **kwargs)
                registered_count += 1
                logger.debug(f"Registered tools from {module_name}")
            except Exception as e:
                logger.error(f"Failed to register tools from {module_name}: {e}")
                raise

        logger.info(f"Auto-discovery registered tools from {registered_count} modules")
