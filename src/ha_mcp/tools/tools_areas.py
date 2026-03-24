"""
Area and floor management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant areas and floors - essential organizational features for smart homes.

Areas and floors use the same tool interface with a ``type`` parameter
(``"area"`` or ``"floor"``) to select which registry to operate on.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import parse_string_list_param

logger = logging.getLogger(__name__)


def register_area_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant area and floor management tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["area", "floor"],
            "title": "List Areas or Floors",
        }
    )
    @log_tool_usage
    async def ha_config_list_areas(
        type: Annotated[  # noqa: A002
            Literal["area", "floor"],
            Field(
                description="Registry type to list: 'area' for rooms, 'floor' for building levels",
                default="area",
            ),
        ] = "area",
    ) -> dict[str, Any]:
        """
        List all Home Assistant areas (rooms) or floors.

        Set type='area' (default) to list areas. Returns area ID, name, icon,
        floor assignment, aliases, and picture URL.

        Set type='floor' to list floors. Returns floor ID, name, icon,
        level (0=ground, 1=first, -1=basement), and aliases.
        """
        try:
            registry = "area" if type == "area" else "floor"
            message: dict[str, Any] = {
                "type": f"config/{registry}_registry/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                items = result.get("result", [])
                key = f"{registry}s"
                return {
                    "success": True,
                    "count": len(items),
                    key: items,
                    "message": f"Found {len(items)} {registry}(s)",
                }
            else:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        result.get("error", f"Failed to list {registry}s"),
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            registry = "area" if type == "area" else "floor"
            logger.error(f"Error listing {registry}s: {e}")
            exception_to_structured_error(
                e,
                context={"operation": f"list_{registry}s"},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["area", "floor"],
            "title": "Create or Update Area or Floor",
        }
    )
    @log_tool_usage
    async def ha_config_set_area(
        type: Annotated[  # noqa: A002
            Literal["area", "floor"],
            Field(
                description="Registry type: 'area' for rooms, 'floor' for building levels",
                default="area",
            ),
        ] = "area",
        name: Annotated[
            str | None,
            Field(
                description="Name (required for create, optional for update, e.g., 'Living Room', 'Ground Floor')",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Area ID to update (omit to create new area). Only used when type='area'.",
                default=None,
            ),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description=(
                    "When type='area': Floor ID to assign this area to (empty string to remove). "
                    "When type='floor': Floor ID to update (omit to create new floor)."
                ),
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:sofa', 'mdi:home-floor-1', empty string to remove)",
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
                description="URL to a picture representing the area (empty string to remove). Only used when type='area'.",
                default=None,
            ),
        ] = None,
        level: Annotated[
            int | None,
            Field(
                description="Numeric level for ordering (0=ground, 1=first, -1=basement). Only used when type='floor'.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant area (room) or floor.

        For areas: provide name to create, area_id to update.
        For floors: provide name to create, floor_id to update.
        Areas organize entities by physical location; floors organize areas into vertical levels.
        """
        operation = "create"
        registry = "area" if type == "area" else "floor"
        try:
            # Parse aliases if provided as string
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid aliases parameter: {e}",
                    )
                )

            if type == "floor":
                return await _set_floor(
                    name=name,
                    floor_id=floor_id,
                    level=level,
                    icon=icon,
                    parsed_aliases=parsed_aliases,
                )
            else:
                return await _set_area(
                    name=name,
                    area_id=area_id,
                    floor_id=floor_id,
                    icon=icon,
                    parsed_aliases=parsed_aliases,
                    picture=picture,
                )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error {operation} {registry} {name!r}: {e}")
            id_val = floor_id if type == "floor" else area_id
            exception_to_structured_error(
                e,
                context={
                    "operation": operation,
                    "name": name,
                    f"{registry}_id": id_val,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "For create: Verify the name is unique",
                    f"For update: Verify the {registry}_id exists using ha_config_list_areas(type='{registry}')",
                ],
            )

    async def _set_area(
        name: str | None,
        area_id: str | None,
        floor_id: str | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
        picture: str | None,
    ) -> dict[str, Any]:
        """Internal helper for area create/update."""
        if area_id:
            # UPDATE operation
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

            operation = "update"
        else:
            # CREATE operation - name is required
            if not name:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "name is required when creating a new area",
                        context={"operation": "create_area"},
                        suggestions=["Provide a name for the new area"],
                    )
                )

            message = {
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
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            ctx: dict[str, Any] = {"operation": operation}
            if name:
                ctx["name"] = name
            if area_id:
                ctx["area_id"] = area_id
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {operation} area: {error_msg}",
                    context=ctx,
                )
            )

    async def _set_floor(
        name: str | None,
        floor_id: str | None,
        level: int | None,
        icon: str | None,
        parsed_aliases: list[str] | None,
    ) -> dict[str, Any]:
        """Internal helper for floor create/update."""
        if floor_id:
            # UPDATE operation
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

            operation = "update"
        else:
            # CREATE operation - name is required
            if not name:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "name is required when creating a new floor",
                        context={"operation": "create_floor"},
                        suggestions=["Provide a name for the new floor"],
                    )
                )

            message = {
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
            error_msg = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            ctx: dict[str, Any] = {"operation": operation}
            if name:
                ctx["name"] = name
            if floor_id:
                ctx["floor_id"] = floor_id
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Failed to {operation} floor: {error_msg}",
                    context=ctx,
                )
            )

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["area", "floor"],
            "title": "Remove Area or Floor",
        }
    )
    @log_tool_usage
    async def ha_config_remove_area(
        type: Annotated[  # noqa: A002
            Literal["area", "floor"],
            Field(
                description="Registry type to remove from: 'area' or 'floor'",
                default="area",
            ),
        ] = "area",
        area_id: Annotated[
            str | None,
            Field(
                description="Area ID to delete. Required when type='area'.",
                default=None,
            ),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description="Floor ID to delete. Required when type='floor'.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant area or floor.

        For areas: entities and devices in the area are not deleted, just unassigned.
        For floors: areas on this floor are not deleted, just unassigned.
        May break automations referencing this area or floor.
        """
        try:
            if type == "floor":
                if not floor_id:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_MISSING_PARAMETER,
                            "floor_id is required when type='floor'",
                            suggestions=[
                                "Provide floor_id to delete, use ha_config_list_areas(type='floor') to find IDs"
                            ],
                        )
                    )
                message: dict[str, Any] = {
                    "type": "config/floor_registry/delete",
                    "floor_id": floor_id,
                }
                item_id = floor_id
                registry = "floor"
            else:
                if not area_id:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_MISSING_PARAMETER,
                            "area_id is required when type='area'",
                            suggestions=[
                                "Provide area_id to delete, use ha_config_list_areas() to find IDs"
                            ],
                        )
                    )
                message = {
                    "type": "config/area_registry/delete",
                    "area_id": area_id,
                }
                item_id = area_id
                registry = "area"

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    f"{registry}_id": item_id,
                    "message": f"Successfully deleted {registry}: {item_id}",
                }
            else:
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                raise_tool_error(
                    create_error_response(
                        ErrorCode.SERVICE_CALL_FAILED,
                        f"Failed to delete {registry}: {error_msg}",
                        context={f"{registry}_id": item_id},
                    )
                )

        except ToolError:
            raise
        except Exception as e:
            registry = "area" if type == "area" else "floor"
            item_id = (area_id if type == "area" else floor_id) or "unknown"
            logger.error(f"Error removing {registry} {item_id!r}: {e}")
            exception_to_structured_error(
                e,
                context={f"{registry}_id": item_id},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify the {registry}_id exists using ha_config_list_areas(type='{registry}')",
                ],
            )
