"""
Core Smart MCP Server implementation.
"""

import logging
from typing import Any

from fastmcp import FastMCP

from .client.rest_client import HomeAssistantClient
from .config import get_global_settings
from .prompts.enhanced import EnhancedPromptsMixin
from .tools.enhanced import EnhancedToolsMixin
from .tools.device_control import create_device_control_tools
from .tools.smart_search import create_smart_search_tools
from .prompts.manager import register_mcp_prompts
from .resources.manager import register_mcp_resources
from .tools.registry import ToolsRegistry

logger = logging.getLogger(__name__)


class HomeAssistantSmartMCPServer(EnhancedToolsMixin, EnhancedPromptsMixin):
    """Home Assistant MCP Server with smart tools and fuzzy search."""

    def __init__(self, client: HomeAssistantClient | None = None):
        """Initialize the smart MCP server."""
        self.settings = get_global_settings()
        self.client = client or HomeAssistantClient()

        # Create FastMCP server
        self.mcp = FastMCP(
            name=self.settings.mcp_server_name, version=self.settings.mcp_server_version
        )

        # Initialize smart tools
        self.smart_tools = create_smart_search_tools(self.client)
        self.device_tools = create_device_control_tools(self.client)

        # Initialize tools registry
        self.tools_registry = ToolsRegistry(self)

        # Register all tools, resources, and prompts
        self._initialize_server()

    def _initialize_server(self) -> None:
        """Initialize all server components."""
        # Register tools
        self.tools_registry.register_all_tools()

        # Register enhanced tools and prompts for first/second interaction success
        self.register_enhanced_tools()
        self.register_enhanced_prompts()

        # Register MCP resources and prompts
        register_mcp_resources(self.mcp)
        register_mcp_prompts(self.mcp)

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
        if hasattr(self.client, "close"):
            await self.client.close()
        logger.info("🔧 Home Assistant Smart MCP Server closed")
