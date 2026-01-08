"""
Entity management tools for Home Assistant MCP server.

This module provides tools for managing entity lifecycle and properties
via the Home Assistant entity registry API.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import coerce_bool_param

logger = logging.getLogger(__name__)


def register_entity_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity management tools with the MCP server."""

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["entity"],
            "title": "Set Entity Enabled",
        }
    )
    @log_tool_usage
    async def ha_set_entity_enabled(
        entity_id: Annotated[
            str, Field(description="Entity ID (e.g., 'sensor.temperature')")
        ],
        enabled: Annotated[
            bool | str, Field(description="True to enable, False to disable")
        ],
    ) -> dict[str, Any]:
        """Enable/disable entity. Disabled entities don't appear in UI.

        Use ha_search_entities() or ha_get_device() to find entity IDs.
        """
        try:
            enabled_bool = coerce_bool_param(enabled, "enabled")

            message = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "disabled_by": None if enabled_bool else "user",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "enabled": entity_entry.get("disabled_by") is None,
                    "message": f"Entity {'enabled' if enabled_bool else 'disabled'}",
                }
            else:
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                return {
                    "success": False,
                    "error": f"Failed to {'enable' if enabled_bool else 'disable'}: {error_msg}",
                    "entity_id": entity_id,
                }
        except Exception as e:
            logger.error(f"Error setting entity enabled: {e}")
            return exception_to_structured_error(e, context={"entity_id": entity_id})
