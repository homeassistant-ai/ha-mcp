"""
Configuration management tools for Home Assistant zones.

This module provides tools for listing, creating/updating, and removing
Home Assistant zones (location-based areas for presence automation).
"""

import logging
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import create_validation_error
from .helpers import exception_to_structured_error, log_tool_usage

logger = logging.getLogger(__name__)


def register_zone_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant zone configuration tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["zone"], "title": "Get Zone"})
    @log_tool_usage
    async def ha_get_zone(
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID to get details for (from ha_get_zone() list). "
                "If omitted, lists all zones.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get zone information - list all zones or get details for a specific one.

        Without a zone_id: Lists all Home Assistant zones with their coordinates and radius.
        With a zone_id: Returns detailed configuration for a specific zone.

        ZONE PROPERTIES:
        - ID, name, icon
        - Latitude, longitude, radius
        - Passive mode setting

        EXAMPLES:
        - List all zones: ha_get_zone()
        - Get specific zone: ha_get_zone(zone_id="abc123")

        **NOTE:** This returns storage-based zones (created via UI/API), not YAML-defined zones.
        The 'home' zone is typically defined in YAML and may not appear in this list.
        """
        try:
            message: dict[str, Any] = {
                "type": "zone/list",
            }

            result = await client.send_websocket_message(message)

            if not result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to get zones: {result.get('error', 'Unknown error')}",
                }

            zones = result.get("result", [])

            # If no zone_id provided, return list of all zones
            if zone_id is None:
                return {
                    "success": True,
                    "count": len(zones),
                    "zones": zones,
                    "message": f"Found {len(zones)} zone(s)",
                }

            # Find specific zone by ID
            zone = next((z for z in zones if z.get("id") == zone_id), None)

            if zone is None:
                available_ids = [z.get("id") for z in zones[:10]]  # Show first 10
                return {
                    "success": False,
                    "error": f"Zone not found: {zone_id}",
                    "zone_id": zone_id,
                    "available_zone_ids": available_ids,
                    "suggestion": "Use ha_get_zone() without zone_id to see all available zones",
                }

            return {
                "success": True,
                "zone_id": zone_id,
                "zone": zone,
            }

        except Exception as e:
            logger.error(f"Error getting zone(s) (zone_id={zone_id}): {e}")
            return {
                "success": False,
                "error": f"Failed to get zones: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search_entities(domain_filter='zone') as alternative",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["zone"], "title": "Set Zone"})
    @log_tool_usage
    async def ha_set_zone(
        name: Annotated[
            str | None,
            Field(
                description="Display name for the zone (required for create)",
                default=None,
            ),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(
                description="Latitude coordinate of the zone center (required for create)",
                default=None,
            ),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(
                description="Longitude coordinate of the zone center (required for create)",
                default=None,
            ),
        ] = None,
        zone_id: Annotated[
            str | None,
            Field(
                description="Zone ID to update (omit to create new zone, use ha_get_zone to find IDs)",
                default=None,
            ),
        ] = None,
        radius: Annotated[
            float | None,
            Field(
                description="Radius of the zone in meters (must be > 0, defaults to 100 on create)",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:briefcase', 'mdi:school')",
                default=None,
            ),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(
                description="Passive mode - if True, zone will not trigger enter/exit automations (defaults to False on create)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant zone.

        Omit zone_id to create a new zone (name, latitude, longitude required).
        Provide zone_id to update an existing zone (only specified fields change).

        EXAMPLES:
        - Create: ha_set_zone(name="Office", latitude=40.7128, longitude=-74.0060, radius=150, icon="mdi:briefcase")
        - Update name: ha_set_zone(zone_id="abc123", name="New Office")
        - Update radius: ha_set_zone(zone_id="abc123", radius=200)
        - Update location: ha_set_zone(zone_id="abc123", latitude=40.7128, longitude=-74.0060)

        Note: The 'home' zone is typically defined in YAML and cannot be modified via this API.
        """
        operation = "create"
        try:
            if zone_id:
                # UPDATE operation
                operation = "update"
                update_fields = {
                    "name": name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius": radius,
                    "icon": icon,
                    "passive": passive,
                }
                fields_to_update = {k: v for k, v in update_fields.items() if v is not None}

                if not fields_to_update:
                    return create_validation_error(
                        "No fields to update. Provide at least one field to change.",
                        context={"zone_id": zone_id},
                    )

                # Validate coordinates if provided
                if latitude is not None and not (-90 <= latitude <= 90):
                    return create_validation_error(
                        f"Invalid latitude: {latitude}. Must be between -90 and 90.",
                        parameter="latitude",
                    )
                if longitude is not None and not (-180 <= longitude <= 180):
                    return create_validation_error(
                        f"Invalid longitude: {longitude}. Must be between -180 and 180.",
                        parameter="longitude",
                    )
                if radius is not None and radius <= 0:
                    return create_validation_error(
                        f"Invalid radius: {radius}. Must be greater than 0.",
                        parameter="radius",
                    )

                message: dict[str, Any] = {
                    "type": "zone/update",
                    "zone_id": zone_id,
                    **fields_to_update,
                }
            else:
                # CREATE operation
                if name is None or latitude is None or longitude is None:
                    return create_validation_error(
                        "name, latitude, and longitude are required when creating a zone.",
                    )

                # Validate coordinates
                if not (-90 <= latitude <= 90):
                    return create_validation_error(
                        f"Invalid latitude: {latitude}. Must be between -90 and 90.",
                        parameter="latitude",
                    )
                if not (-180 <= longitude <= 180):
                    return create_validation_error(
                        f"Invalid longitude: {longitude}. Must be between -180 and 180.",
                        parameter="longitude",
                    )
                if radius is not None and radius <= 0:
                    return create_validation_error(
                        f"Invalid radius: {radius}. Must be greater than 0.",
                        parameter="radius",
                    )

                message = {
                    "type": "zone/create",
                    "name": name,
                    "latitude": latitude,
                    "longitude": longitude,
                    "radius": radius if radius is not None else 100,
                    "passive": passive if passive is not None else False,
                }
                if icon:
                    message["icon"] = icon

            result = await client.send_websocket_message(message)

            if result.get("success"):
                zone_data = result.get("result", {})
                zone_name = name or zone_data.get("name", zone_id)
                response: dict[str, Any] = {
                    "success": True,
                    "zone_data": zone_data,
                    "zone_id": zone_data.get("id", zone_id),
                    "message": f"Successfully {'updated' if zone_id else 'created'} zone: {zone_name}",
                }
                if zone_id and fields_to_update:
                    response["updated_fields"] = list(fields_to_update.keys())
                return response
            else:
                return {
                    "success": False,
                    "error": f"Failed to {operation} zone: {result.get('error', 'Unknown error')}",
                }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error in ha_set_zone ({operation}, zone_id={zone_id}, name={name}): {e}")
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id, "operation": operation},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify coordinates are valid" if operation == "create" else "Verify zone_id exists using ha_get_zone()",
                ],
            )

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["zone"], "title": "Remove Zone"})
    @log_tool_usage
    async def ha_remove_zone(
        zone_id: Annotated[
            str,
            Field(description="Zone ID to remove (use ha_get_zone to find IDs)"),
        ],
    ) -> dict[str, Any]:
        """
        Remove a Home Assistant zone.

        EXAMPLES:
        - Remove zone: ha_remove_zone("abc123")

        **WARNING:** Removing a zone used in automations may cause those automations to fail.
        Use ha_get_zone() to find the zone_id for the zone you want to remove.

        **NOTE:** The 'home' zone cannot be removed as it is typically defined in configuration.yaml.
        """
        try:
            message: dict[str, Any] = {
                "type": "zone/delete",
                "zone_id": zone_id,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                return {
                    "success": True,
                    "zone_id": zone_id,
                    "message": f"Successfully removed zone: {zone_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to remove zone: {result.get('error', 'Unknown error')}",
                    "zone_id": zone_id,
                }

        except Exception as e:
            logger.error(f"Error removing zone (zone_id={zone_id}): {e}")
            exception_to_structured_error(
                e,
                context={"zone_id": zone_id},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify zone_id exists using ha_get_zone()",
                    "Ensure zone is not the 'home' zone (YAML-defined)",
                ],
            )
