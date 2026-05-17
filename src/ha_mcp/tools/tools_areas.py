"""
Area and floor management tools for Home Assistant.

This module provides tools for listing, creating, updating, and deleting
Home Assistant areas and floors - essential organizational features for smart homes.
"""

import logging
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
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
        name="ha_list_floors_areas",
        tags={"Areas & Floors"},
        annotations={"idempotentHint": True, "readOnlyHint": True, "title": "List Floors and Areas"},
    )
    @log_tool_usage
    async def ha_list_floors_areas(self) -> dict[str, Any]:
        """
        List floors sorted by level ascending, each with their assigned areas nested, plus areas without a floor.

        Do not use for flat listings — ha_config_list_areas and ha_config_list_floors cover those.

        Use for location-based reasoning where floor-to-area relationships matter, such as "which rooms are on the ground floor" or operations scoped to a level.

        Floors with level=None sort alongside level 0 (ground floor). Areas without a floor assignment appear in unassigned_areas; areas whose floor_id points to a non-existent floor appear in orphaned_areas — a topology snapshot may diverge from individual list calls if the registries change between reads.
        """
        progress: dict[str, Any] = {
            "operation": "list_floors_areas",
            "phase": "start",
        }
        try:
            areas_result = await self._client.send_websocket_message(
                {"type": "config/area_registry/list"}
            )
            progress["phase"] = "areas_fetched"
            floors_result = await self._client.send_websocket_message(
                {"type": "config/floor_registry/list"}
            )
            progress["phase"] = "floors_fetched"

            # A response with success=True but no "result" key is malformed —
            # treat it as a service call failure rather than silently returning
            # floor_count=0, area_count=0 on a populated instance.
            areas_ok = areas_result.get("success") and "result" in areas_result
            floors_ok = floors_result.get("success") and "result" in floors_result
            if not (areas_ok and floors_ok):
                raise_tool_error(create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Failed to retrieve area or floor registry",
                    context={
                        "areas_success": areas_result.get("success"),
                        "floors_success": floors_result.get("success"),
                        "areas_response_keys": sorted(areas_result.keys()),
                        "floors_response_keys": sorted(floors_result.keys()),
                    },
                    suggestions=[
                        "Check Home Assistant connection",
                        "Verify WebSocket connection is active",
                    ],
                ))

            areas = areas_result["result"]
            floors = floors_result["result"]

            # Partition areas into three disjoint sets:
            #   - nested:    floor_id present AND points to a known floor
            #   - orphaned:  floor_id present BUT points to a non-existent floor
            #                (race between the two sequential reads, or manual
            #                .storage inconsistency)
            #   - unassigned: no floor_id at all
            # Orphaned is surfaced as a separate key so the LLM can diagnose
            # registry drift without introspecting individual area fields.
            # Use `is None` rather than falsy-check so that a floor_id of ""
            # (valid but unusual) is treated as orphaned if it does not resolve,
            # not as unassigned.
            valid_floor_ids = {
                f.get("floor_id") for f in floors if f.get("floor_id") is not None
            }
            floor_map: dict[str, list[dict[str, Any]]] = {}
            unassigned_areas: list[dict[str, Any]] = []
            orphaned_areas: list[dict[str, Any]] = []
            for area in areas:
                fid = area.get("floor_id")
                if fid is None:
                    unassigned_areas.append(area)
                elif fid in valid_floor_ids:
                    floor_map.setdefault(fid, []).append(area)
                else:
                    orphaned_areas.append(area)
            progress["phase"] = "partitioned"

            # Build nested hierarchy, preserving all floor-registry fields for
            # forward compatibility with future HA Core additions
            topology = [
                {**floor, "areas": floor_map.get(floor.get("floor_id"), [])}
                for floor in floors
            ]

            # Sort by level ascending; coerce defensively so a malformed
            # string `level` cannot raise TypeError mid-sort and get
            # flattened by the broad `except Exception` below.
            def _floor_sort_key(floor: dict[str, Any]) -> int:
                raw = floor.get("level")
                if raw is None:
                    return 0
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    logger.warning(
                        f"Floor {floor.get('floor_id')!r} has non-numeric "
                        f"level {raw!r}; treating as 0 for sort"
                    )
                    return 0

            topology.sort(key=_floor_sort_key)
            progress["phase"] = "sorted"

            return {
                "success": True,
                "floor_count": len(topology),
                "area_count": len(areas),
                "unassigned_count": len(unassigned_areas),
                "orphaned_count": len(orphaned_areas),
                "floors": topology,
                "unassigned_areas": unassigned_areas,
                "orphaned_areas": orphaned_areas,
                "message": (
                    f"Found {len(topology)} floor(s), {len(areas)} area(s), "
                    f"{len(unassigned_areas)} unassigned, "
                    f"{len(orphaned_areas)} orphaned"
                ),
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(
                f"Error listing floors and areas in phase {progress['phase']!r}: {e} "
                f"(progress={progress})"
            )
            exception_to_structured_error(
                e,
                context=progress,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                ],
            )

    # ============================================================
    # COMBINED SET / REMOVE
    # ============================================================

    @tool(
        name="ha_set_area_or_floor",
        tags={"Areas & Floors"},
        annotations={"destructiveHint": True, "title": "Create or Update Area or Floor"},
    )
    @log_tool_usage
    async def ha_set_area_or_floor(
        self,
        kind: Annotated[
            Literal["area", "floor"],
            Field(
                description="Which registry to operate on: 'area' for rooms, 'floor' for building levels",
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                description="Name (required when creating; optional when updating, e.g., 'Living Room', 'Ground Floor')",
                default=None,
            ),
        ] = None,
        id: Annotated[  # noqa: A002
            str | None,
            Field(
                description="Existing area_id or floor_id to update (omit to create a new entry; use ha_list_floors_areas to find IDs)",
                default=None,
            ),
        ] = None,
        floor_id: Annotated[
            str | None,
            Field(
                description="Floor assignment when kind='area' (use empty string to clear). Only valid when kind='area'.",
                default=None,
            ),
        ] = None,
        level: Annotated[
            int | None,
            Field(
                description="Numeric level when kind='floor' (0=ground, 1=first, -1=basement). Only valid when kind='floor'.",
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
                description="Alternative names for voice assistant recognition (e.g., ['lounge'], empty list to clear)",
                default=None,
            ),
        ] = None,
        picture: Annotated[
            str | None,
            Field(
                description="Picture URL when kind='area' (empty string to remove). Only valid when kind='area'.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant area or floor.

        Pass kind='area' (with optional floor_id, picture) or kind='floor' (with optional level).
        Provide name only to create a new entry; provide id to update an existing one.
        Cross-kind parameters (e.g., picture under kind='floor') are rejected with VALIDATION_INVALID_PARAMETER.

        EXAMPLES:
        ha_set_area_or_floor(kind="area", name="Kitchen")
        ha_set_area_or_floor(kind="area", id="kitchen", floor_id="ground_floor")
        ha_set_area_or_floor(kind="floor", name="Basement", level=-1)
        ha_set_area_or_floor(kind="floor", id="ground_floor", level=0)
        """
        operation = "create"
        try:
            try:
                parsed_aliases = parse_string_list_param(aliases, "aliases")
            except ValueError as e:
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid aliases parameter: {e}",
                ))

            # Reject cross-kind params loudly so silent intent loss can't happen
            # (e.g., kind='floor' with picture='...' previously dropped the picture
            # without a diagnostic).
            cross_kind_params: list[str] = []
            if kind == "area" and level is not None:
                cross_kind_params.append("level")
            elif kind == "floor":
                if floor_id is not None:
                    cross_kind_params.append("floor_id")
                if picture is not None:
                    cross_kind_params.append("picture")
            if cross_kind_params:
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Parameter(s) {cross_kind_params} are not valid for kind={kind!r}",
                    context={"kind": kind, "invalid_parameters": cross_kind_params},
                    suggestions=[
                        "For kind='area' use: name, id, floor_id, icon, aliases, picture",
                        "For kind='floor' use: name, id, level, icon, aliases",
                    ],
                ))

            # ``None`` stays the documented "create-new" sentinel; explicit
            # empty/whitespace would silently route to the ``if id:`` create
            # branch below and lose update intent.
            if id is not None:
                validate_identifier_not_empty(
                    id,
                    "id",
                    suggestions=[
                        "Omit id entirely to create a new entry",
                        "Pass a real area_id/floor_id to update an existing entry",
                    ],
                    context={"kind": kind},
                )

            if kind == "area":
                if id:
                    message = self._build_area_update_message(
                        id, name, floor_id, icon, parsed_aliases, picture,
                    )
                    operation = "update"
                else:
                    # Reassignment narrows ``name`` from ``str | None`` to
                    # ``str`` for the build-message call below.
                    name = validate_identifier_not_empty(
                        name,
                        "name",
                        message="name is required when creating a new area",
                        context={"operation": "create_area"},
                        suggestions=["Provide a non-empty name for the new area"],
                    )
                    message = self._build_area_create_message(
                        name, floor_id, icon, parsed_aliases, picture,
                    )
                    operation = "create"
                result_key = "area"
                id_key = "area_id"
            else:  # kind == "floor"
                if id:
                    message = self._build_floor_update_message(
                        id, name, level, icon, parsed_aliases,
                    )
                    operation = "update"
                else:
                    name = validate_identifier_not_empty(
                        name,
                        "name",
                        message="name is required when creating a new floor",
                        context={"operation": "create_floor"},
                        suggestions=["Provide a non-empty name for the new floor"],
                    )
                    message = self._build_floor_create_message(
                        name, level, icon, parsed_aliases,
                    )
                    operation = "create"
                result_key = "floor"
                id_key = "floor_id"

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                data = result.get("result", {})
                returned_id = data.get(id_key, id)
                display_name = name or data.get("name", returned_id)
                return {
                    "success": True,
                    result_key: data,
                    id_key: returned_id,
                    "kind": kind,
                    "message": f"Successfully {operation}d {kind}: {display_name}",
                }

            error = result.get("error", {})
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            ctx: dict[str, Any] = {"operation": operation, "kind": kind}
            if name:
                ctx["name"] = name
            if id:
                ctx[id_key] = id
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to {operation} {kind}: {error_msg}",
                context=ctx,
            ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error {operation} {kind} {name!r}: {e}")
            suggestions = [
                "Check Home Assistant connection",
                "For create: Verify the name is unique",
                f"For update: Verify the {kind} id exists using ha_list_floors_areas()",
            ]
            if kind == "area":
                suggestions.append("If assigning to a floor, verify floor_id exists")
            exception_to_structured_error(
                e,
                context={"operation": operation, "kind": kind, "name": name, "id": id},
                suggestions=suggestions,
            )

    @tool(
        name="ha_remove_area_or_floor",
        tags={"Areas & Floors"},
        annotations={"destructiveHint": True, "idempotentHint": True, "title": "Remove Area or Floor"},
    )
    @log_tool_usage
    async def ha_remove_area_or_floor(
        self,
        kind: Annotated[
            Literal["area", "floor"],
            Field(description="Which registry to delete from: 'area' or 'floor'"),
        ],
        id: Annotated[  # noqa: A002
            str,
            Field(description="Area ID or floor ID to delete (use ha_list_floors_areas to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """Remove a Home Assistant area or floor.

        Removing an area unassigns its entities and devices (the entities and
        devices themselves are not removed). Removing a floor unassigns its
        areas. May break automations referencing the removed area/floor.
        """
        registry = "area_registry" if kind == "area" else "floor_registry"
        id_key = "area_id" if kind == "area" else "floor_id"
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                id,
                "id",
                suggestions=[
                    f"Pass a valid {id_key} (use ha_list_floors_areas() to list)",
                ],
                context={"action": "remove", "kind": kind},
            )
            message: dict[str, Any] = {
                "type": f"config/{registry}/delete",
                id_key: id,
            }

            result = await self._client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    id_key: id,
                    "kind": kind,
                    "message": f"Successfully removed {kind}: {id}",
                }

            error = result.get("error", {})
            error_msg = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Failed to remove {kind}: {error_msg}",
                context={"kind": kind, id_key: id},
            ))

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error removing {kind} {id!r}: {e}")
            exception_to_structured_error(
                e,
                context={"kind": kind, id_key: id},
                suggestions=[
                    "Check Home Assistant connection",
                    f"Verify the {kind} id exists using ha_list_floors_areas()",
                ],
            )


def register_area_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant area and floor management tools."""
    register_tool_methods(mcp, AreaTools(client))
