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

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_config_list_areas() -> dict[str, Any]:
        """
        List all Home Assistant areas (rooms) with their configurations.

        Returns all areas with:
        - Area ID, name, and icon
        - Floor assignment (if any)
        - Aliases for voice assistants
        - Picture URL (if set)

        EXAMPLES:
        - List all areas: ha_config_list_areas()

        Use this to discover existing areas before creating new ones or
        to find area IDs for entity assignment.
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

    @mcp.tool
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

        Areas are used to organize entities and devices by physical location.
        They enable features like "Turn off all lights in the living room".

        EXAMPLES:
        - Create area: ha_config_set_area("Living Room")
        - Create with floor: ha_config_set_area("Master Bedroom", floor_id="first_floor")
        - Create with icon: ha_config_set_area("Kitchen", icon="mdi:stove")
        - Create with aliases: ha_config_set_area("Living Room", aliases=["lounge", "family room"])
        - Update area: ha_config_set_area("Family Room", area_id="living_room")
        - Remove floor: ha_config_set_area("Bedroom", area_id="bedroom", floor_id="")

        After creating an area, you can assign entities to it using their entity registry settings.
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

    @mcp.tool
    @log_tool_usage
    async def ha_config_remove_area(
        area_id: Annotated[
            str,
            Field(description="Area ID to delete (use ha_config_list_areas to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant area.

        WARNING: Deleting an area will:
        - Remove the area from all assigned entities and devices
        - Break any automations or scripts that reference this area

        The entities and devices themselves are NOT deleted, they just become unassigned.

        EXAMPLES:
        - Delete area: ha_config_remove_area("guest_room")

        Use ha_config_list_areas() first to verify the area ID.
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

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_config_list_floors() -> dict[str, Any]:
        """
        List all Home Assistant floors with their configurations.

        Returns all floors with:
        - Floor ID, name, and icon
        - Level (numeric ordering, e.g., 0=ground, 1=first, -1=basement)
        - Aliases for voice assistants

        EXAMPLES:
        - List all floors: ha_config_list_floors()

        Use this to discover existing floors before creating new ones or
        to find floor IDs for area assignment.
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

    @mcp.tool
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

        Floors organize areas into vertical levels of a building.
        They enable features like "Turn off all lights downstairs".

        EXAMPLES:
        - Create floor: ha_config_set_floor("Ground Floor")
        - Create with level: ha_config_set_floor("First Floor", level=1)
        - Create basement: ha_config_set_floor("Basement", level=-1, icon="mdi:home-floor-b")
        - Create with aliases: ha_config_set_floor("Ground Floor", aliases=["downstairs", "main level"])
        - Update floor: ha_config_set_floor("Main Floor", floor_id="ground_floor")
        - Remove icon: ha_config_set_floor("Basement", floor_id="basement", icon="")

        After creating a floor, assign areas to it using ha_config_set_area().
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

    @mcp.tool
    @log_tool_usage
    async def ha_config_remove_floor(
        floor_id: Annotated[
            str,
            Field(description="Floor ID to delete (use ha_config_list_floors to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant floor.

        WARNING: Deleting a floor will:
        - Remove the floor assignment from all areas on that floor
        - Break any automations or scripts that reference this floor

        The areas themselves are NOT deleted, they just become unassigned from the floor.

        EXAMPLES:
        - Delete floor: ha_config_remove_floor("third_floor")

        Use ha_config_list_floors() first to verify the floor ID.
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
