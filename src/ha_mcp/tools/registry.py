"""
Tools registry for Smart MCP Server - manages registration of all MCP tools.

This module uses lazy auto-discovery to find and register all tool modules.
Tool modules are discovered at startup but only imported when first accessed,
improving server startup time significantly (especially for binary distributions).

Adding a new tools module is simple:
1. Create tools_*.py file with a register_*_tools(mcp, client, **kwargs) function
2. The function will be auto-discovered and registered lazily

No changes to this file are needed when adding new tool modules!
"""

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
    """Manages registration of all MCP tools for the smart server.

    Implements lazy loading pattern: tool modules are discovered at startup
    but only imported and registered when the server starts accepting connections.
    This significantly improves startup time for binary distributions.
    """

    def __init__(self, server: Any) -> None:
        self.server = server
        self.client = server.client
        self.mcp = server.mcp
        # These are now lazily initialized via server properties
        self._smart_tools = None
        self._device_tools = None
        self._modules_registered = False
        # Discover modules at init time (fast - no imports)
        self._discovered_modules = self._discover_tool_modules()

    @property
    def smart_tools(self) -> Any:
        """Lazily get smart_tools from server."""
        if self._smart_tools is None:
            self._smart_tools = self.server.smart_tools
        return self._smart_tools

    @property
    def device_tools(self) -> Any:
        """Lazily get device_tools from server."""
        if self._device_tools is None:
            self._device_tools = self.server.device_tools
        return self._device_tools

    def _discover_tool_modules(self) -> list[str]:
        """Discover tool module names without importing them.

        This is a fast operation that only reads file names.
        Returns list of module names that follow the tools_*.py convention.
        """
        discovered = []
        package_path = Path(__file__).parent

        for module_info in pkgutil.iter_modules([str(package_path)]):
            module_name = module_info.name
            if module_name.startswith("tools_"):
                discovered.append(module_name)

        # Add explicit modules
        discovered.extend(EXPLICIT_MODULES.keys())

        logger.debug(f"Discovered {len(discovered)} tool modules (not yet imported)")
        return discovered

    def register_all_tools(self) -> None:
        """Register all tools with the MCP server using lazy auto-discovery.

        Tool modules are imported and registered only when this method is called,
        which happens after the MCP server is ready to accept connections.
        """
        if self._modules_registered:
            logger.debug("Tools already registered, skipping")
            return

        import importlib

        # Build kwargs with all available dependencies (lazy access)
        kwargs = {
            "smart_tools": self.smart_tools,
            "device_tools": self.device_tools,
        }

        registered_count = 0

        # Import and register tools_*.py modules
        for module_name in self._discovered_modules:
            # Skip explicit modules - handled separately
            if module_name in EXPLICIT_MODULES:
                continue

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

        self._modules_registered = True
        logger.info(f"Auto-discovery registered tools from {registered_count} modules")
