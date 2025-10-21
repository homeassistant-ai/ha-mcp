"""Core Smart MCP Server implementation."""

import asyncio
import functools
import json
import logging
from typing import Any, Callable

from fastmcp import FastMCP

from .client.rest_client import HomeAssistantClient
from .config import get_global_settings
from .prompts.enhanced import EnhancedPromptsMixin
from .tools.enhanced import EnhancedToolsMixin
from .tools.device_control import create_device_control_tools
from .tools.smart_search import create_smart_search_tools
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

        # Install verbose tool logging when requested via environment flag
        if self.settings.log_all_tools:
            self._install_tool_logging()

        # Initialize smart tools
        self.smart_tools = create_smart_search_tools(self.client)
        self.device_tools = create_device_control_tools(self.client)

        # Initialize tools registry
        self.tools_registry = ToolsRegistry(self)

        # Register all tools and expert prompts
        self._initialize_server()

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
        if hasattr(self.client, "close"):
            await self.client.close()
        logger.info("🔧 Home Assistant Smart MCP Server closed")

    # ------------------------------------------------------------------
    # Tool logging helpers
    # ------------------------------------------------------------------

    def _install_tool_logging(self) -> None:
        """Wrap FastMCP tool registration to log requests and responses."""

        original_tool = self.mcp.tool

        def logging_tool(
            *tool_args: Any, **tool_kwargs: Any
        ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            decorator = original_tool(*tool_args, **tool_kwargs)

            def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
                tool_name = tool_kwargs.get("name") or getattr(
                    func, "__name__", "unknown"
                )

                if asyncio.iscoroutinefunction(func):

                    @functools.wraps(func)
                    async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                        return await self._execute_tool_with_logging(
                            tool_name, func, args, kwargs
                        )

                    return decorator(async_wrapper)

                @functools.wraps(func)
                def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                    return self._execute_tool_with_logging_sync(
                        tool_name, func, args, kwargs
                    )

                return decorator(sync_wrapper)

            return wrapper

        self.mcp.tool = logging_tool

    async def _execute_tool_with_logging(
        self,
        tool_name: str,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Execute async tool with logging of request and response payloads."""

        log_entry = self._prepare_log_entry(tool_name, args, kwargs)
        try:
            result = await func(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - passthrough for logging only
            log_entry["status"] = "error"
            log_entry["error"] = repr(exc)
            logger.info(
                "[TOOL_CALL] %s",
                json.dumps(log_entry, ensure_ascii=False, sort_keys=True),
            )
            raise

        self._attach_response(log_entry, result)
        logger.info(
            "[TOOL_CALL] %s", json.dumps(log_entry, ensure_ascii=False, sort_keys=True)
        )
        return result

    def _execute_tool_with_logging_sync(
        self,
        tool_name: str,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        """Execute sync tool with logging of request and response payloads."""

        log_entry = self._prepare_log_entry(tool_name, args, kwargs)
        try:
            result = func(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - passthrough for logging only
            log_entry["status"] = "error"
            log_entry["error"] = repr(exc)
            logger.info(
                "[TOOL_CALL] %s",
                json.dumps(log_entry, ensure_ascii=False, sort_keys=True),
            )
            raise

        self._attach_response(log_entry, result)
        logger.info(
            "[TOOL_CALL] %s", json.dumps(log_entry, ensure_ascii=False, sort_keys=True)
        )
        return result

    def _prepare_log_entry(
        self, tool_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> dict[str, Any]:
        """Create the base log entry with serialized request data."""

        request_payload, request_characters = self._serialize_with_size(
            {"args": list(args), "kwargs": kwargs}
        )
        return {
            "event": "tool_call",
            "tool": tool_name,
            "status": "success",
            "request": request_payload,
            "request_characters": request_characters,
        }

    def _attach_response(self, log_entry: dict[str, Any], result: Any) -> None:
        """Attach serialized response information to an existing log entry."""

        response_payload, response_characters = self._serialize_with_size(result)
        log_entry["response"] = response_payload
        log_entry["response_characters"] = response_characters

    def _serialize_with_size(self, value: Any) -> tuple[Any, int]:
        """Serialize complex values into JSON-friendly structures and measure size."""

        serialized = self._to_serializable(value)
        serialized_text = json.dumps(serialized, ensure_ascii=False, sort_keys=True)
        return serialized, len(serialized_text)

    def _to_serializable(self, value: Any) -> Any:
        """Best-effort conversion of values into JSON-serializable data."""

        if isinstance(value, dict):
            return {str(key): self._to_serializable(val) for key, val in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._to_serializable(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return repr(value)
