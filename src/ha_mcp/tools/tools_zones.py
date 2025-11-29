"""
Configuration management tools for Home Assistant zones.

This module provides tools for listing, creating, updating, and deleting
Home Assistant zones (location-based areas for presence automation).
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_zone_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant zone configuration tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_zones() -> dict[str, Any]:
        """
        List all Home Assistant zones with their coordinates and radius.

        Returns complete configuration for all zones including:
        - ID, name, icon
        - Latitude, longitude, radius
        - Passive mode setting

        EXAMPLES:
        - List all zones: ha_list_zones()

        **NOTE:** This returns storage-based zones (created via UI/API), not YAML-defined zones.
        The 'home' zone is typically defined in YAML and may not appear in this list.
        """
        try:
            message: dict[str, Any] = {
                "type": "zone/list",
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                zones = result.get("result", [])
                return {
                    "success": True,
                    "count": len(zones),
                    "zones": zones,
                    "message": f"Found {len(zones)} zone(s)",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to list zones: {result.get('error', 'Unknown error')}",
                }

        except Exception as e:
            logger.error(f"Error listing zones: {e}")
            return {
                "success": False,
                "error": f"Failed to list zones: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify WebSocket connection is active",
                    "Use ha_search_entities(domain_filter='zone') as alternative",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_create_zone(
        name: Annotated[str, Field(description="Display name for the zone")],
        latitude: Annotated[
            float,
            Field(description="Latitude coordinate of the zone center"),
        ],
        longitude: Annotated[
            float,
            Field(description="Longitude coordinate of the zone center"),
        ],
        radius: Annotated[
            float,
            Field(
                description="Radius of the zone in meters (default: 100)",
                default=100,
            ),
        ] = 100,
        icon: Annotated[
            str | None,
            Field(
                description="Material Design Icon (e.g., 'mdi:briefcase', 'mdi:school')",
                default=None,
            ),
        ] = None,
        passive: Annotated[
            bool,
            Field(
                description="If True, zone will not trigger automations on enter/exit (default: False)",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Create a new Home Assistant zone for presence detection.

        Zones are location-based areas that can trigger automations when
        devices enter or exit. Common use cases include:
        - Home, Work, School locations
        - Gym, Shopping areas
        - Family members' locations

        EXAMPLES:
        - Create office zone: ha_create_zone("Office", 40.7128, -74.0060, radius=150, icon="mdi:briefcase")
        - Create gym zone: ha_create_zone("Gym", 40.7580, -73.9855, radius=50, icon="mdi:dumbbell")
        - Create passive zone: ha_create_zone("City Center", 40.7484, -73.9857, radius=500, passive=True)

        Note: The 'home' zone is typically defined in configuration.yaml and cannot be created via this API.
        """
        try:
            # Validate coordinates
            if not (-90 <= latitude <= 90):
                return {
                    "success": False,
                    "error": f"Invalid latitude: {latitude}. Must be between -90 and 90.",
                }
            if not (-180 <= longitude <= 180):
                return {
                    "success": False,
                    "error": f"Invalid longitude: {longitude}. Must be between -180 and 180.",
                }
            if radius <= 0:
                return {
                    "success": False,
                    "error": f"Invalid radius: {radius}. Must be greater than 0.",
                }

            # Build create message
            message: dict[str, Any] = {
                "type": "zone/create",
                "name": name,
                "latitude": latitude,
                "longitude": longitude,
                "radius": radius,
                "passive": passive,
            }

            if icon:
                message["icon"] = icon

            result = await client.send_websocket_message(message)

            if result.get("success"):
                zone_data = result.get("result", {})
                return {
                    "success": True,
                    "zone_data": zone_data,
                    "zone_id": zone_data.get("id"),
                    "message": f"Successfully created zone: {name}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to create zone: {result.get('error', 'Unknown error')}",
                    "name": name,
                }

        except Exception as e:
            logger.error(f"Error creating zone: {e}")
            return {
                "success": False,
                "error": f"Zone creation failed: {str(e)}",
                "name": name,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify coordinates are valid",
                    "Ensure zone name is unique",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_update_zone(
        zone_id: Annotated[
            str,
            Field(description="Zone ID to update (from ha_list_zones)"),
        ],
        name: Annotated[
            str | None,
            Field(description="New display name for the zone", default=None),
        ] = None,
        latitude: Annotated[
            float | None,
            Field(description="New latitude coordinate", default=None),
        ] = None,
        longitude: Annotated[
            float | None,
            Field(description="New longitude coordinate", default=None),
        ] = None,
        radius: Annotated[
            float | None,
            Field(description="New radius in meters", default=None),
        ] = None,
        icon: Annotated[
            str | None,
            Field(description="New Material Design Icon", default=None),
        ] = None,
        passive: Annotated[
            bool | None,
            Field(description="New passive mode setting", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update an existing Home Assistant zone.

        Only the fields you specify will be updated. Other fields remain unchanged.

        EXAMPLES:
        - Update zone name: ha_update_zone("abc123", name="New Office")
        - Update zone radius: ha_update_zone("abc123", radius=200)
        - Update zone location: ha_update_zone("abc123", latitude=40.7128, longitude=-74.0060)
        - Update multiple fields: ha_update_zone("abc123", name="Gym", radius=75, icon="mdi:dumbbell")

        **TIP:** Use ha_list_zones() to get the zone_id for the zone you want to update.
        """
        try:
            # Validate that at least one field is being updated
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
                return {
                    "success": False,
                    "error": "No fields to update. Provide at least one field to change.",
                    "zone_id": zone_id,
                }

            # Validate coordinates if provided
            if latitude is not None and not (-90 <= latitude <= 90):
                return {
                    "success": False,
                    "error": f"Invalid latitude: {latitude}. Must be between -90 and 90.",
                }
            if longitude is not None and not (-180 <= longitude <= 180):
                return {
                    "success": False,
                    "error": f"Invalid longitude: {longitude}. Must be between -180 and 180.",
                }
            if radius is not None and radius <= 0:
                return {
                    "success": False,
                    "error": f"Invalid radius: {radius}. Must be greater than 0.",
                }

            # Build update message
            message: dict[str, Any] = {
                "type": "zone/update",
                "zone_id": zone_id,
                **fields_to_update,
            }

            result = await client.send_websocket_message(message)

            if result.get("success"):
                zone_data = result.get("result", {})
                return {
                    "success": True,
                    "zone_data": zone_data,
                    "zone_id": zone_id,
                    "updated_fields": list(fields_to_update.keys()),
                    "message": f"Successfully updated zone: {zone_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to update zone: {result.get('error', 'Unknown error')}",
                    "zone_id": zone_id,
                }

        except Exception as e:
            logger.error(f"Error updating zone: {e}")
            return {
                "success": False,
                "error": f"Zone update failed: {str(e)}",
                "zone_id": zone_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify zone_id exists using ha_list_zones()",
                    "Ensure values are valid",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True})
    @log_tool_usage
    async def ha_delete_zone(
        zone_id: Annotated[
            str,
            Field(description="Zone ID to delete (from ha_list_zones)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant zone.

        EXAMPLES:
        - Delete zone: ha_delete_zone("abc123")

        **WARNING:** Deleting a zone that is used in automations may cause those automations to fail.
        Use ha_list_zones() to get the zone_id for the zone you want to delete.

        **NOTE:** The 'home' zone cannot be deleted as it is typically defined in configuration.yaml.
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
                    "message": f"Successfully deleted zone: {zone_id}",
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to delete zone: {result.get('error', 'Unknown error')}",
                    "zone_id": zone_id,
                }

        except Exception as e:
            logger.error(f"Error deleting zone: {e}")
            return {
                "success": False,
                "error": f"Zone deletion failed: {str(e)}",
                "zone_id": zone_id,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify zone_id exists using ha_list_zones()",
                    "Ensure zone is not the 'home' zone (YAML-defined)",
                ],
            }
