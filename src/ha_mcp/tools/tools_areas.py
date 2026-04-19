"""
Area and floor management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant areas and floors - essential organizational features for smart homes.
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import parse_string_list_param

logger = logging.getLogger(__name__)


class AreaTools:
    """Area and floor management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @staticmethod
    def _build_area_update_message(
        area_id: str,
        name: str | None,
        floor_id: str | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
        picture: str | None,
    ) -> dict[str, Any]:
        """Build a WebSocket message for updating an existing area."""
        message: dict[str, Any] = {
            "type": "config/area_registry/update",
            "area_id": area_id,
        }
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
        return message

    @staticmethod
    def _build_area_create_message(
        name: str,
        floor_id: str | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
        picture: str | None,
    ) -> dict[str, Any]:
        """Build a WebSocket message for creating a new area."""
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
        return message

    @staticmethod
    def _build_floor_update_message(
        floor_id: str,
        name: str | None,
        level: int | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
    ) -> dict[str, Any]:
        """Build a WebSocket message for updating an existing floor."""
        message: dict[str, Any] = {
            "type": "config/floor_registry/update",
            "floor_id": floor_id,
        }
        if name is not None:
            message["name"] = name
        if level is not None:
            message["level"] = level
        if icon is not None:
            message["icon"] = icon if icon else None
        if parsed_aliases is not None:
            message["aliases"] = parsed_aliases
        return message

    @staticmethod
    def _build_floor_create_message(
        name: str,
        level: int | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
    ) -> dict[str, Any]:
        """Build a WebSocket message for creating a new floor."""
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
        return message

    # ============================================================
    # AREA TOOLS
    # ============================================================

    @tool(
        name="ha_config_list_areas",
        tags={"Areas & Floors"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "List Areas"},
    )
    @log_tool_usage
    async def ha_config_list_areas(self) -> dict[str, Any]:
        """
        List all Home Assistant areas (rooms).

        Returns area ID, name, icon, floor assignment, aliases, and picture URL.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/area_registry/list",
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                areas = result.get("result", [])
                return {
                    "success": True,
                    "count": len(areas),
                    "areas": areas,
                    "message": f"Found {len(areas)} area(s)",
                }
            else:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Failed to list areas"),
                ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing areas: {e}")
            exception_to_structured_error(e, context={"operation": "list_areas"}, suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
            ])

    @tool(
        name="ha_config_set_area",
        tags={"Areas & Floors"},
        annotations={"destructiveHint": True, "title": "Create or Update Area"},
    )
    @log_tool_usage
    async def ha_config_set_area(
        self,
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
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid aliases parameter: {e}",
                ))

            # Determine if this is a create or update operation
            if area_id:
                message = self._build_area_update_message(
                    area_id, name, floor_id, icon, parsed_aliases, picture,
                )
                operation = "update"
            else:
                if not name:
                    raise_tool_error(create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "name is required when creating a new area",
                        context={"operation": "create_area"},
                        suggestions=["Provide a name for the new area"],
                    ))
                message = self._build_area_create_message(
                    name, floor_id, icon, parsed_aliases, picture,
                )
                operation = "create"

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                area_data = result.get("result", {})
                area_name = name or area_data.get("name", area_id)
                return {
                    "success": True,
                    "area": area_data,
                    "area_id": area_data.get("area_id", area_id),
                    "message": f"Successfully {operation}d area: {area_name}",
                }

            error = result.get("error", {})
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            ctx: dict[str, Any] = {"operation": operation}
            if name:
                ctx["name"] = name
            if area_id:
                ctx["area_id"] = area_id
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to {operation} area: {error_msg}",
                context=ctx,
            ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error {operation} area {name!r}: {e}")
            exception_to_structured_error(e, context={"operation": operation, "name": name, "area_id": area_id}, suggestions=[
                "Check Home Assistant connection",
                "For create: Verify the name is unique",
                "For update: Verify the area_id exists using ha_config_list_areas()",
                "If assigning to a floor, verify floor_id exists",
            ])

    @tool(
        name="ha_config_remove_area",
        tags={"Areas & Floors"},
        annotations={"destructiveHint": True, "idempotentHint": True, "title": "Remove Area"},
    )
    @log_tool_usage
    async def ha_config_remove_area(
        self,
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

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "area_id": area_id,
                    "message": f"Successfully deleted area: {area_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to delete area: {error_msg}",
                    context={"area_id": area_id},
                ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing area {area_id!r}: {e}")
            exception_to_structured_error(e, context={"area_id": area_id}, suggestions=[
                "Check Home Assistant connection",
                "Verify the area_id exists using ha_config_list_areas()",
            ])

    # ============================================================
    # FLOOR TOOLS
    # ============================================================

    @tool(
        name="ha_config_list_floors",
        tags={"Areas & Floors"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "List Floors"},
    )
    @log_tool_usage
    async def ha_config_list_floors(self) -> dict[str, Any]:
        """
        List all Home Assistant floors.

        Returns floor ID, name, icon, level (0=ground, 1=first, -1=basement), and aliases.
        """
        try:
            message: dict[str, Any] = {
                "type": "config/floor_registry/list",
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                floors = result.get("result", [])
                return {
                    "success": True,
                    "count": len(floors),
                    "floors": floors,
                    "message": f"Found {len(floors)} floor(s)",
                }
            else:
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    result.get("error", "Failed to list floors"),
                ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error listing floors: {e}")
            exception_to_structured_error(e, context={"operation": "list_floors"}, suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
            ])

    @tool(
        name="ha_get_home_topology",
        tags={"Areas & Floors"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Home Topology"},
    )
    @log_tool_usage
    async def ha_get_home_topology(self) -> dict[str, Any]:
        """
        Get the hierarchical floor/area structure of the home.

        Returns floors sorted by level (basement first, upper floors last),
        each with its assigned areas nested underneath, plus unassigned_areas
        for areas without a floor assignment. Pre-joins data from
        ha_config_list_floors and ha_config_list_areas into a single
        hierarchy, useful for location-based queries (e.g., "which rooms
        are on the ground floor").
        """
        try:
            areas_result = await self._client.send_websocket_message(
                {"type": "config/area_registry/list"}
            )
            floors_result = await self._client.send_websocket_message(
                {"type": "config/floor_registry/list"}
            )

            if not (areas_result.get("success") and floors_result.get("success")):
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to retrieve area or floor registry",
                    context={
                        "areas_success": areas_result.get("success"),
                        "floors_success": floors_result.get("success"),
                    },
                ))

            areas = areas_result.get("result", [])
            floors = floors_result.get("result", [])

            # Group areas by floor_id; collect areas without floor assignment
            floor_map: dict[str, list[dict[str, Any]]] = {}
            unassigned_areas: list[dict[str, Any]] = []
            for area in areas:
                fid = area.get("floor_id")
                if fid:
                    floor_map.setdefault(fid, []).append(area)
                else:
                    unassigned_areas.append(area)

            # Build nested hierarchy, preserving all floor-registry fields for
            # forward compatibility with future HA Core additions
            topology = [
                {**floor, "areas": floor_map.get(floor.get("floor_id"), [])}
                for floor in floors
            ]

            # Sort by level ascending; None treated as 0 (Python sort fails on None/int mix)
            topology.sort(key=lambda f: f.get("level") or 0)

            return {
                "success": True,
                "floor_count": len(topology),
                "area_count": len(areas),
                "unassigned_count": len(unassigned_areas),
                "floors": topology,
                "unassigned_areas": unassigned_areas,
                "message": (
                    f"Found {len(topology)} floor(s), {len(areas)} area(s), "
                    f"{len(unassigned_areas)} unassigned"
                ),
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting home topology: {e}")
            exception_to_structured_error(e, context={"operation": "get_home_topology"}, suggestions=[
                "Check Home Assistant connection",
                "Verify WebSocket connection is active",
            ])

    @tool(
        name="ha_config_set_floor",
        tags={"Areas & Floors"},
        annotations={"destructiveHint": True, "title": "Create or Update Floor"},
    )
    @log_tool_usage
    async def ha_config_set_floor(
        self,
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
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid aliases parameter: {e}",
                ))

            # Determine if this is a create or update operation
            if floor_id:
                message = self._build_floor_update_message(
                    floor_id, name, level, icon, parsed_aliases,
                )
                operation = "update"
            else:
                if not name:
                    raise_tool_error(create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "name is required when creating a new floor",
                        context={"operation": "create_floor"},
                        suggestions=["Provide a name for the new floor"],
                    ))
                message = self._build_floor_create_message(
                    name, level, icon, parsed_aliases,
                )
                operation = "create"

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                floor_data = result.get("result", {})
                floor_name = name or floor_data.get("name", floor_id)
                return {
                    "success": True,
                    "floor": floor_data,
                    "floor_id": floor_data.get("floor_id", floor_id),
                    "message": f"Successfully {operation}d floor: {floor_name}",
                }

            error = result.get("error", {})
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            ctx: dict[str, Any] = {"operation": operation}
            if name:
                ctx["name"] = name
            if floor_id:
                ctx["floor_id"] = floor_id
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to {operation} floor: {error_msg}",
                context=ctx,
            ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error {operation} floor {name!r}: {e}")
            exception_to_structured_error(e, context={"operation": operation, "name": name, "floor_id": floor_id}, suggestions=[
                "Check Home Assistant connection",
                "For create: Verify the name is unique",
                "For update: Verify the floor_id exists using ha_config_list_floors()",
            ])

    @tool(
        name="ha_config_remove_floor",
        tags={"Areas & Floors"},
        annotations={"destructiveHint": True, "idempotentHint": True, "title": "Remove Floor"},
    )
    @log_tool_usage
    async def ha_config_remove_floor(
        self,
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

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "floor_id": floor_id,
                    "message": f"Successfully deleted floor: {floor_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to delete floor: {error_msg}",
                    context={"floor_id": floor_id},
                ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing floor {floor_id!r}: {e}")
            exception_to_structured_error(e, context={"floor_id": floor_id}, suggestions=[
                "Check Home Assistant connection",
                "Verify the floor_id exists using ha_config_list_floors()",
            ])


def register_area_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant area and floor management tools."""
    register_tool_methods(mcp, AreaTools(client))
