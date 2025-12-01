"""
Area and floor management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant areas and floors - essential organizational features for smart homes.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_string_list_param

logger = logging.getLogger(__name__)


def register_area_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant area and floor management tools."""

    # ============================================================
    # AREA TOOLS
    # ============================================================

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["area"], "title": "List Areas"})
    @log_tool_usage
    async def ha_config_list_areas() -> dict[str, Any]:
        """
        List all Home Assistant areas (rooms).

        Returns area ID, name, icon, floor assignment, aliases, and picture URL.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/area_registry/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                areas = result.get("result", [])
                return {
                    "success": True,
                    "count": len(areas),
                    "areas": areas,
                    "message": f"Found {len(areas)} area(s)",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to list areas: {result.get('error', 'Unknown error')}",
                }

        except Exception as e:
            logger.error(f"Error listing areas: {e}")
            return {
                "success": False,
                "error": f"Failed to list areas: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["area"], "title": "Create or Update Area"})
    @log_tool_usage
    async def ha_config_set_area(
        name: Annotated[
            str | None,
            Field(
                description="Name for the area (required for create, optional for update, e.g., 'Living Room', 'Kitchen')",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Area ID to update (omit to create new area, use ha_config_list_areas to find IDs)",
                default=None,
            ),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description="Floor ID to assign this area to (use ha_config_list_floors to find IDs, empty string to remove)",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:sofa', 'mdi:bed', empty string to remove)",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="Alternative names for voice assistant recognition (e.g., ['lounge', 'family room'], empty list to clear)",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="URL to a picture representing the area (empty string to remove)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant area (room).

        Provide name only to create a new area. Provide area_id to update existing.
        Areas organize entities by physical location for room-based control.
        """
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return {"success": False, "error": f"Invalid aliases parameter: {e}"}

            # Determine if this is a create or update operation
            if area_id:
                # UPDATE operation
                message: dict[str, Any] = {
                    "type": "config/area_registry/update",
                    "area_id": area_id,
                }

                # Only add fields that were explicitly provided
                if name is not None:
                    message["name"] = name
                if floor_id is not None:
                    message["floor_id"] = floor_id if floor_id else None
                if icon is not None:
                    message["icon"] = icon if icon else None
                if parsed_aliases is not None:
                    message["aliases"] = parsed_aliases
                if picture is not None:
                    message["picture"] = picture if picture else None

                operation = "update"
            else:
                # CREATE operation - name is required
                if not name:
                    return {
                        "success": False,
                        "error": "name is required when creating a new area",
                    }

                message: dict[str, Any] = {
                    "type": "config/area_registry/create",
                    "name": name,
                }

                if floor_id:
                    message["floor_id"] = floor_id
                if icon:
                    message["icon"] = icon
                if parsed_aliases:
                    message["aliases"] = parsed_aliases
                if picture:
                    message["picture"] = picture

                operation = "create"

            result = await client.send_websocket_message(message)

            if result.get("success"):
                area_data = result.get("result", {})
                area_name = name or area_data.get("name", area_id)
                return {
                    "success": True,
                    "area": area_data,
                    "area_id": area_data.get("area_id", area_id),
                    "message": f"Successfully {operation}d area: {area_name}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                error_response = {
                    "success": False,
                    "error": f"Failed to {operation} area: {error_msg}",
                }
                if name:
                    error_response["name"] = name
                return error_response

        except Exception as e:
            logger.error(f"Error in ha_config_set_area: {e}")
            error_response = {
                "success": False,
                "error": f"Failed to set area: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "For create: Verify the name is unique",
                    "For update: Verify the area_id exists using ha_config_list_areas()",
                    "If assigning to a floor, verify floor_id exists",
                ],
            }
            if name:
                error_response["name"] = name
            return error_response

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["area"], "title": "Remove Area"})
    @log_tool_usage
    async def ha_config_remove_area(
        area_id: Annotated[
            str,
            Field(description="Area ID to delete (use ha_config_list_areas to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant area.

        Entities and devices in the area are not deleted, just unassigned.
        May break automations referencing this area.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/area_registry/delete",
                "area_id": area_id,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "area_id": area_id,
                    "message": f"Successfully deleted area: {area_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                return {
                    "success": False,
                    "error": f"Failed to delete area: {error_msg}",
                    "area_id": area_id,
                }

        except Exception as e:
            logger.error(f"Error deleting area: {e}")
            return {
                "success": False,
                "error": f"Failed to delete area: {str(e)}",
                "area_id": area_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the area_id exists using ha_config_list_areas()",
                ],
            }

    # ============================================================
    # FLOOR TOOLS
    # ============================================================

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["floor"], "title": "List Floors"})
    @log_tool_usage
    async def ha_config_list_floors() -> dict[str, Any]:
        """
        List all Home Assistant floors.

        Returns floor ID, name, icon, level (0=ground, 1=first, -1=basement), and aliases.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/floor_registry/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                floors = result.get("result", [])
                return {
                    "success": True,
                    "count": len(floors),
                    "floors": floors,
                    "message": f"Found {len(floors)} floor(s)",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to list floors: {result.get('error', 'Unknown error')}",
                }

        except Exception as e:
            logger.error(f"Error listing floors: {e}")
            return {
                "success": False,
                "error": f"Failed to list floors: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["floor"], "title": "Create or Update Floor"})
    @log_tool_usage
    async def ha_config_set_floor(
        name: Annotated[
            str | None,
            Field(
                description="Name for the floor (required for create, optional for update, e.g., 'Ground Floor', 'Basement')",
                default=None,
            ),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description="Floor ID to update (omit to create new floor, use ha_config_list_floors to find IDs)",
                default=None,
            ),
        ] = None,
        level: Annotated[
            int | None,
            Field(
                description="Numeric level for ordering (0=ground, 1=first, -1=basement, etc.)",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:home-floor-1', 'mdi:home-floor-b', empty string to remove)",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="Alternative names for voice assistant recognition (e.g., ['downstairs', 'main level'], empty list to clear)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant floor.

        Provide name only to create a new floor. Provide floor_id to update existing.
        Floors organize areas into vertical levels for building-wide control.
        """
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return {"success": False, "error": f"Invalid aliases parameter: {e}"}

            # Determine if this is a create or update operation
            if floor_id:
                # UPDATE operation
                message: dict[str, Any] = {
                    "type": "config/floor_registry/update",
                    "floor_id": floor_id,
                }

                # Only add fields that were explicitly provided
                if name is not None:
                    message["name"] = name
                if level is not None:
                    message["level"] = level
                if icon is not None:
                    message["icon"] = icon if icon else None
                if parsed_aliases is not None:
                    message["aliases"] = parsed_aliases

                operation = "update"
            else:
                # CREATE operation - name is required
                if not name:
                    return {
                        "success": False,
                        "error": "name is required when creating a new floor",
                    }

                message: dict[str, Any] = {
                    "type": "config/floor_registry/create",
                    "name": name,
                }

                if level is not None:
                    message["level"] = level
                if icon:
                    message["icon"] = icon
                if parsed_aliases:
                    message["aliases"] = parsed_aliases

                operation = "create"

            result = await client.send_websocket_message(message)

            if result.get("success"):
                floor_data = result.get("result", {})
                floor_name = name or floor_data.get("name", floor_id)
                return {
                    "success": True,
                    "floor": floor_data,
                    "floor_id": floor_data.get("floor_id", floor_id),
                    "message": f"Successfully {operation}d floor: {floor_name}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                error_response = {
                    "success": False,
                    "error": f"Failed to {operation} floor: {error_msg}",
                }
                if name:
                    error_response["name"] = name
                return error_response

        except Exception as e:
            logger.error(f"Error in ha_config_set_floor: {e}")
            error_response = {
                "success": False,
                "error": f"Failed to set floor: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "For create: Verify the name is unique",
                    "For update: Verify the floor_id exists using ha_config_list_floors()",
                ],
            }
            if name:
                error_response["name"] = name
            return error_response

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["floor"], "title": "Remove Floor"})
    @log_tool_usage
    async def ha_config_remove_floor(
        floor_id: Annotated[
            str,
            Field(description="Floor ID to delete (use ha_config_list_floors to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant floor.

        Areas on this floor are not deleted, just unassigned.
        May break automations referencing this floor.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/floor_registry/delete",
                "floor_id": floor_id,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "floor_id": floor_id,
                    "message": f"Successfully deleted floor: {floor_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                return {
                    "success": False,
                    "error": f"Failed to delete floor: {error_msg}",
                    "floor_id": floor_id,
                }

        except Exception as e:
            logger.error(f"Error deleting floor: {e}")
            return {
                "success": False,
                "error": f"Failed to delete floor: {str(e)}",
                "floor_id": floor_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the floor_id exists using ha_config_list_floors()",
                ],
            }
