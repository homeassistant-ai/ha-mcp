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
    async def ha_list_areas() -> dict[str, Any]:
        """
        List all Home Assistant areas (rooms) with their configurations.

        Returns all areas with:
        - Area ID, name, and icon
        - Floor assignment (if any)
        - Aliases for voice assistants
        - Picture URL (if set)

        EXAMPLES:
        - List all areas: ha_list_areas()

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
    async def ha_create_area(
        name: Annotated[
            str,
            Field(description="Name for the area (e.g., 'Living Room', 'Kitchen')"),
        ],
        floor_id: Annotated[
            str | None,
            Field(
                description="Floor ID to assign this area to (use ha_list_floors to find IDs)",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:sofa', 'mdi:bed')",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="Alternative names for voice assistant recognition (e.g., ['lounge', 'family room'])",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="URL to a picture representing the area",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create a new Home Assistant area (room).

        Areas are used to organize entities and devices by physical location.
        They enable features like "Turn off all lights in the living room".

        EXAMPLES:
        - Basic area: ha_create_area("Living Room")
        - With floor: ha_create_area("Master Bedroom", floor_id="first_floor")
        - With icon: ha_create_area("Kitchen", icon="mdi:stove")
        - With aliases: ha_create_area("Living Room", aliases=["lounge", "family room"])
        - Complete: ha_create_area("Office", floor_id="basement", icon="mdi:desk", aliases=["study"])

        After creating an area, you can assign entities to it using their entity registry settings.
        """
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return {"success": False, "error": f"Invalid aliases parameter: {e}"}

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

            result = await client.send_websocket_message(message)

            if result.get("success"):
                area_data = result.get("result", {})
                return {
                    "success": True,
                    "area": area_data,
                    "area_id": area_data.get("area_id"),
                    "message": f"Successfully created area: {name}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                return {
                    "success": False,
                    "error": f"Failed to create area: {error_msg}",
                    "name": name,
                }

        except Exception as e:
            logger.error(f"Error creating area: {e}")
            return {
                "success": False,
                "error": f"Failed to create area: {str(e)}",
                "name": name,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the name is unique",
                    "If assigning to a floor, verify floor_id exists",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_update_area(
        area_id: Annotated[
            str,
            Field(description="Area ID to update (use ha_list_areas to find IDs)"),
        ],
        name: Annotated[
            str | None,
            Field(description="New name for the area", default=None),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description="New floor ID to assign (use empty string '' to remove floor assignment)",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="New icon (e.g., 'mdi:sofa'). Use empty string '' to remove.",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="New list of aliases (replaces existing). Use empty list [] to clear.",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="New picture URL. Use empty string '' to remove.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update an existing Home Assistant area.

        Only provided fields will be updated; others remain unchanged.

        EXAMPLES:
        - Rename: ha_update_area("living_room", name="Family Room")
        - Change floor: ha_update_area("bedroom", floor_id="second_floor")
        - Remove floor: ha_update_area("bedroom", floor_id="")
        - Update icon: ha_update_area("kitchen", icon="mdi:pot-steam")
        - Update aliases: ha_update_area("office", aliases=["study", "workspace"])
        - Multiple updates: ha_update_area("garage", name="Workshop", icon="mdi:tools")
        """
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return {"success": False, "error": f"Invalid aliases parameter: {e}"}

            message: dict[str, Any] = {
                "type": "config/area_registry/update",
                "area_id": area_id,
            }

            # Only add fields that were explicitly provided
            if name is not None:
                message["name"] = name
            if floor_id is not None:
                # Empty string means remove floor assignment
                message["floor_id"] = floor_id if floor_id else None
            if icon is not None:
                # Empty string means remove icon
                message["icon"] = icon if icon else None
            if parsed_aliases is not None:
                message["aliases"] = parsed_aliases
            if picture is not None:
                # Empty string means remove picture
                message["picture"] = picture if picture else None

            result = await client.send_websocket_message(message)

            if result.get("success"):
                area_data = result.get("result", {})
                return {
                    "success": True,
                    "area": area_data,
                    "area_id": area_id,
                    "message": f"Successfully updated area: {area_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                return {
                    "success": False,
                    "error": f"Failed to update area: {error_msg}",
                    "area_id": area_id,
                }

        except Exception as e:
            logger.error(f"Error updating area: {e}")
            return {
                "success": False,
                "error": f"Failed to update area: {str(e)}",
                "area_id": area_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the area_id exists using ha_list_areas()",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_delete_area(
        area_id: Annotated[
            str,
            Field(description="Area ID to delete (use ha_list_areas to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant area.

        WARNING: Deleting an area will:
        - Remove the area from all assigned entities and devices
        - Break any automations or scripts that reference this area

        The entities and devices themselves are NOT deleted, they just become unassigned.

        EXAMPLES:
        - Delete area: ha_delete_area("guest_room")

        Use ha_list_areas() first to verify the area ID.
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
                    "Verify the area_id exists using ha_list_areas()",
                ],
            }

    # ============================================================
    # FLOOR TOOLS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_floors() -> dict[str, Any]:
        """
        List all Home Assistant floors with their configurations.

        Returns all floors with:
        - Floor ID, name, and icon
        - Level (numeric ordering, e.g., 0=ground, 1=first, -1=basement)
        - Aliases for voice assistants

        EXAMPLES:
        - List all floors: ha_list_floors()

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
    async def ha_create_floor(
        name: Annotated[
            str,
            Field(description="Name for the floor (e.g., 'Ground Floor', 'Basement')"),
        ],
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
                description="Material Design Icon (e.g., 'mdi:home-floor-1', 'mdi:home-floor-b')",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="Alternative names for voice assistant recognition (e.g., ['downstairs', 'main level'])",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create a new Home Assistant floor.

        Floors organize areas into vertical levels of a building.
        They enable features like "Turn off all lights downstairs".

        EXAMPLES:
        - Basic floor: ha_create_floor("Ground Floor")
        - With level: ha_create_floor("First Floor", level=1)
        - Basement: ha_create_floor("Basement", level=-1, icon="mdi:home-floor-b")
        - With aliases: ha_create_floor("Ground Floor", aliases=["downstairs", "main level"])
        - Complete: ha_create_floor("Second Floor", level=2, icon="mdi:home-floor-2", aliases=["upstairs"])

        After creating a floor, assign areas to it using ha_update_area().
        """
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return {"success": False, "error": f"Invalid aliases parameter: {e}"}

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

            result = await client.send_websocket_message(message)

            if result.get("success"):
                floor_data = result.get("result", {})
                return {
                    "success": True,
                    "floor": floor_data,
                    "floor_id": floor_data.get("floor_id"),
                    "message": f"Successfully created floor: {name}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                return {
                    "success": False,
                    "error": f"Failed to create floor: {error_msg}",
                    "name": name,
                }

        except Exception as e:
            logger.error(f"Error creating floor: {e}")
            return {
                "success": False,
                "error": f"Failed to create floor: {str(e)}",
                "name": name,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the name is unique",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_update_floor(
        floor_id: Annotated[
            str,
            Field(description="Floor ID to update (use ha_list_floors to find IDs)"),
        ],
        name: Annotated[
            str | None,
            Field(description="New name for the floor", default=None),
        ] = None,
        level: Annotated[
            int | None,
            Field(
                description="New level number for ordering",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="New icon (e.g., 'mdi:home-floor-2'). Use empty string '' to remove.",
                default=None,
            ),
        ] = None,
        aliases: Annotated[
            str | list[str] | None,
            Field(
                description="New list of aliases (replaces existing). Use empty list [] to clear.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update an existing Home Assistant floor.

        Only provided fields will be updated; others remain unchanged.

        EXAMPLES:
        - Rename: ha_update_floor("ground_floor", name="Main Floor")
        - Change level: ha_update_floor("basement", level=-2)
        - Update icon: ha_update_floor("first_floor", icon="mdi:home-floor-1")
        - Update aliases: ha_update_floor("ground_floor", aliases=["main", "downstairs"])
        - Multiple updates: ha_update_floor("attic", name="Loft", level=3, icon="mdi:home-roof")
        """
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                return {"success": False, "error": f"Invalid aliases parameter: {e}"}

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
                # Empty string means remove icon
                message["icon"] = icon if icon else None
            if parsed_aliases is not None:
                message["aliases"] = parsed_aliases

            result = await client.send_websocket_message(message)

            if result.get("success"):
                floor_data = result.get("result", {})
                return {
                    "success": True,
                    "floor": floor_data,
                    "floor_id": floor_id,
                    "message": f"Successfully updated floor: {floor_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                return {
                    "success": False,
                    "error": f"Failed to update floor: {error_msg}",
                    "floor_id": floor_id,
                }

        except Exception as e:
            logger.error(f"Error updating floor: {e}")
            return {
                "success": False,
                "error": f"Failed to update floor: {str(e)}",
                "floor_id": floor_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify the floor_id exists using ha_list_floors()",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_delete_floor(
        floor_id: Annotated[
            str,
            Field(description="Floor ID to delete (use ha_list_floors to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant floor.

        WARNING: Deleting a floor will:
        - Remove the floor assignment from all areas on that floor
        - Break any automations or scripts that reference this floor

        The areas themselves are NOT deleted, they just become unassigned from the floor.

        EXAMPLES:
        - Delete floor: ha_delete_floor("third_floor")

        Use ha_list_floors() first to verify the floor ID.
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
                    "Verify the floor_id exists using ha_list_floors()",
                ],
            }
