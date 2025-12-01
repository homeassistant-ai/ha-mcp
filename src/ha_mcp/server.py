"""
Core Smart MCP Server implementation.

Implements lazy initialization pattern for improved startup time:
- Settings and FastMCP server are created immediately (fast)
- Smart tools and device tools are created lazily on first access
- Tool modules are discovered at startup but imported on first use
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastmcp import FastMCP

from .config import get_global_settings
from .prompts.enhanced import EnhancedPromptsMixin
from .tools.enhanced import EnhancedToolsMixin

if TYPE_CHECKING:
    from .client.rest_client import HomeAssistantClient
    from .tools.registry import ToolsRegistry

logger = logging.getLogger(__name__)


class HomeAssistantSmartMCPServer(EnhancedToolsMixin, EnhancedPromptsMixin):
    """Home Assistant MCP Server with smart tools and fuzzy search.

    Uses lazy initialization to improve startup time:
    - Client, smart_tools, device_tools are created on first access
    - Tool modules are discovered at startup but imported when first called
    """

    def __init__(
        self,
        client: HomeAssistantClient | None = None,
        server_name: str = "ha-mcp",
        server_version: str = "0.1.0",
    ):
        """Initialize the smart MCP server with lazy loading support."""
        # Load settings first (fast operation)
        self.settings = get_global_settings()

        # Store provided client or mark for lazy creation
        self._client: HomeAssistantClient | None = client
        self._client_provided = client is not None

        # Lazy initialization placeholders
        self._smart_tools: Any = None
        self._device_tools: Any = None
        self._tools_registry: ToolsRegistry | None = None

        # Get server name/version from settings if no client provided
        if not self._client_provided:
            server_name = self.settings.mcp_server_name
            server_version = self.settings.mcp_server_version

        # Create FastMCP server immediately (it's fast and needed for tool registration)
        self.mcp = FastMCP(name=server_name, version=server_version)

        # Register all tools and expert prompts
        self._initialize_server()

    @property
    def client(self) -> HomeAssistantClient:
        """Lazily create and return the Home Assistant client."""
        if self._client is None:
            from .client.rest_client import HomeAssistantClient
            self._client = HomeAssistantClient()
            logger.debug("Lazily created HomeAssistantClient")
        return self._client

    @property
    def smart_tools(self) -> Any:
        """Lazily create and return the smart search tools."""
        if self._smart_tools is None:
            from .tools.smart_search import create_smart_search_tools
            self._smart_tools = create_smart_search_tools(self.client)
            logger.debug("Lazily created SmartSearchTools")
        return self._smart_tools

    @property
    def device_tools(self) -> Any:
        """Lazily create and return the device control tools."""
        if self._device_tools is None:
            from .tools.device_control import create_device_control_tools
            self._device_tools = create_device_control_tools(self.client)
            logger.debug("Lazily created DeviceControlTools")
        return self._device_tools

    @property
    def tools_registry(self) -> ToolsRegistry:
        """Lazily create and return the tools registry."""
        if self._tools_registry is None:
            from .tools.registry import ToolsRegistry
            self._tools_registry = ToolsRegistry(self)
            logger.debug("Lazily created ToolsRegistry")
        return self._tools_registry

    def _initialize_server(self) -> None:
        """Initialize all server components."""
        # Register tools
        self.tools_registry.register_all_tools()

        # Register enhanced tools and prompts for first/second interaction success
        self.register_enhanced_tools()
        self.register_enhanced_prompts()

    # Helper methods required by EnhancedToolsMixin

    async def smart_entity_search(
        self, query: str, domain_filter: str | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Bridge method to existing smart search implementation."""
        return await self.smart_tools.smart_entity_search(
            query=query, limit=limit, include_attributes=False
        )

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """Bridge method to existing entity state implementation."""
        return await self.client.get_entity_state(entity_id)

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Bridge method to existing service call implementation."""
        service_data = data or {}
        if entity_id:
            service_data["entity_id"] = entity_id
        return await self.client.call_service(domain, service, service_data)

    async def get_entities_by_area(self, area_name: str) -> dict[str, Any]:
        """Bridge method to existing area functionality."""
        return await self.smart_tools.get_entities_by_area(
            area_query=area_name, group_by_domain=True
        )

    async def start(self) -> None:
        """Start the Smart MCP server with async compatibility."""
        logger.info(
            f"🚀 Starting Smart {self.settings.mcp_server_name} v{self.settings.mcp_server_version}"
        )

        # Test connection on startup
        try:
            success, error = await self.client.test_connection()
            if success:
                config = await self.client.get_config()
                logger.info(
                    f"✅ Successfully connected to Home Assistant: {config.get('location_name', 'Unknown')}"
                )
            else:
                logger.warning(f"⚠️ Failed to connect to Home Assistant: {error}")
        except Exception as e:
            logger.error(f"❌ Error testing connection: {e}")

        # Log available tools count
        logger.info("🔧 Smart server with enhanced tools loaded")

        # Run the MCP server with async compatibility
        await self.mcp.run_async()

    async def close(self) -> None:
        """Close the MCP server and cleanup resources."""
        # Only close client if it was actually created
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()
        logger.info("🔧 Home Assistant Smart MCP Server closed")
