"""
Entity Registry and Device Registry management tools for Home Assistant.

This module provides tools for:
- Renaming entities (changing entity_id)
- Managing devices (list, get details, update, remove)

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import logging
import re
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_string_list_param

logger = logging.getLogger(__name__)


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity registry and device registry management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "title": "Rename Entity"})
    @log_tool_usage
    async def ha_rename_entity(
        entity_id: Annotated[
            str,
            Field(
                description="Current entity ID to rename (e.g., 'light.old_name')"
            ),
        ],
        new_entity_id: Annotated[
            str,
            Field(
                description="New entity ID (e.g., 'light.new_name'). Domain must match the original."
            ),
        ],
        name: Annotated[
            str | None,
            Field(
                description="Optional: New friendly name for the entity",
                default=None,
            ),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="Optional: New icon (e.g., 'mdi:lightbulb')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Rename a Home Assistant entity by changing its entity_id.

        Changes the entity_id (e.g., light.old_name -> light.new_name).
        The domain must remain the same - you cannot change a light to a switch.

        IMPORTANT LIMITATIONS:
        - References in automations/scripts/dashboards are NOT automatically updated
        - Entity history is preserved (HA 2022.4+)
        - Some entities cannot be renamed:
          - Entities without unique IDs
          - Entities disabled by integration

        EXAMPLES:
        - Rename light: ha_rename_entity("light.bedroom_1", "light.master_bedroom")
        - Rename with friendly name: ha_rename_entity("sensor.temp", "sensor.living_room_temp", name="Living Room Temperature")

        NOTE: This is different from renaming a device. Device and entity renaming are independent.
        Renaming a device does NOT rename its entities. See ha_update_device() for device renaming.
        """
        try:
            # Validate entity_id format
            entity_pattern = r"^[a-z_]+\.[a-z0-9_]+$"
            if not re.match(entity_pattern, entity_id):
                return {
                    "success": False,
                    "error": f"Invalid entity_id format: {entity_id}",
                    "expected_format": "domain.object_id (lowercase letters, numbers, underscores only)",
                }

            if not re.match(entity_pattern, new_entity_id):
                return {
                    "success": False,
                    "error": f"Invalid new_entity_id format: {new_entity_id}",
                    "expected_format": "domain.object_id (lowercase letters, numbers, underscores only)",
                }

            # Extract and validate domains match
            current_domain = entity_id.split(".")[0]
            new_domain = new_entity_id.split(".")[0]

            if current_domain != new_domain:
                return {
                    "success": False,
                    "error": f"Domain mismatch: cannot change from '{current_domain}' to '{new_domain}'",
                    "suggestion": f"New entity_id must start with '{current_domain}.'",
                }

            # Build update message
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "new_entity_id": new_entity_id,
            }

            if name is not None:
                message["name"] = name
            if icon is not None:
                message["icon"] = icon

            logger.info(f"Renaming entity {entity_id} to {new_entity_id}")
            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                return {
                    "success": True,
                    "old_entity_id": entity_id,
                    "new_entity_id": new_entity_id,
                    "entity_entry": entity_entry,
                    "message": f"Successfully renamed entity from {entity_id} to {new_entity_id}",
                    "warning": "Remember to update any automations, scripts, or dashboards that reference the old entity_id",
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
                    "error": f"Failed to rename entity: {error_msg}",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Verify the entity exists using ha_search_entities()",
                        "Check that the new entity_id doesn't already exist",
                        "Ensure the entity has a unique_id (some legacy entities cannot be renamed)",
                    ],
                }

        except Exception as e:
            logger.error(f"Error renaming entity: {e}")
            return {
                "success": False,
                "error": f"Entity rename failed: {str(e)}",
                "entity_id": entity_id,
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "List Devices"})
    @log_tool_usage
    async def ha_list_devices(
        area_id: Annotated[
            str | None,
            Field(
                description="Filter devices by area ID (e.g., 'living_room')",
                default=None,
            ),
        ] = None,
        manufacturer: Annotated[
            str | None,
            Field(
                description="Filter devices by manufacturer name (e.g., 'Philips')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        List all devices in the Home Assistant device registry.

        Returns a list of all registered devices with their properties.
        Optionally filter by area or manufacturer.

        Device information includes:
        - device_id: Unique identifier for the device
        - name: Display name (name_by_user if set, otherwise default name)
        - manufacturer, model, sw_version: Device identification
        - area_id: Assigned area/room
        - disabled_by: Whether device is disabled and by whom
        - labels: Assigned labels

        EXAMPLES:
        - List all devices: ha_list_devices()
        - Filter by area: ha_list_devices(area_id="living_room")
        - Filter by manufacturer: ha_list_devices(manufacturer="Philips")
        """
        try:
            message: dict[str, Any] = {"type": "config/device_registry/list"}

            result = await client.send_websocket_message(message)

            if result.get("success"):
                devices = result.get("result", [])

                # Apply filters if specified
                filtered_devices = devices
                if area_id:
                    filtered_devices = [
                        d for d in filtered_devices
                        if d.get("area_id") == area_id
                    ]
                if manufacturer:
                    manufacturer_lower = manufacturer.lower()
                    filtered_devices = [
                        d for d in filtered_devices
                        if manufacturer_lower in (d.get("manufacturer") or "").lower()
                    ]

                # Simplify device data for output
                device_list = []
                for device in filtered_devices:
                    device_info = {
                        "device_id": device.get("id"),
                        "name": device.get("name_by_user") or device.get("name"),
                        "manufacturer": device.get("manufacturer"),
                        "model": device.get("model"),
                        "sw_version": device.get("sw_version"),
                        "area_id": device.get("area_id"),
                        "disabled_by": device.get("disabled_by"),
                        "labels": device.get("labels", []),
                    }
                    device_list.append(device_info)

                filters_applied = []
                if area_id:
                    filters_applied.append(f"area_id={area_id}")
                if manufacturer:
                    filters_applied.append(f"manufacturer={manufacturer}")

                return {
                    "success": True,
                    "count": len(device_list),
                    "total_devices": len(devices),
                    "devices": device_list,
                    "filters": filters_applied if filters_applied else None,
                    "message": f"Found {len(device_list)} device(s)"
                    + (f" (filtered from {len(devices)} total)" if filters_applied else ""),
                }
            else:
                return {
                    "success": False,
                    "error": f"Failed to list devices: {result.get('error', 'Unknown error')}",
                }

        except Exception as e:
            logger.error(f"Error listing devices: {e}")
            return {
                "success": False,
                "error": f"Failed to list devices: {str(e)}",
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "Get Device Details"})
    @log_tool_usage
    async def ha_get_device(
        device_id: Annotated[
            str,
            Field(description="Device ID to retrieve details for"),
        ],
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific device including its entities.

        Returns comprehensive device details:
        - Basic info: name, manufacturer, model, sw_version
        - Configuration: area_id, disabled_by, labels
        - Associated entities: List of all entities belonging to this device

        EXAMPLES:
        - Get device details: ha_get_device("abc123def456")

        TIP: Use ha_list_devices() first to find device IDs, or ha_smart_search_entities()
        to find devices by name.
        """
        try:
            # Get device registry to find the device
            list_message: dict[str, Any] = {"type": "config/device_registry/list"}
            list_result = await client.send_websocket_message(list_message)

            if not list_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
                }

            devices = list_result.get("result", [])
            device = next((d for d in devices if d.get("id") == device_id), None)

            if not device:
                return {
                    "success": False,
                    "error": f"Device not found: {device_id}",
                    "suggestion": "Use ha_list_devices() to find valid device IDs",
                }

            # Get entity registry to find entities for this device
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)

            device_entities = []
            if entity_result.get("success"):
                all_entities = entity_result.get("result", [])
                device_entities = [
                    {
                        "entity_id": e.get("entity_id"),
                        "name": e.get("name") or e.get("original_name"),
                        "platform": e.get("platform"),
                        "disabled_by": e.get("disabled_by"),
                    }
                    for e in all_entities
                    if e.get("device_id") == device_id
                ]

            device_info = {
                "device_id": device.get("id"),
                "name": device.get("name_by_user") or device.get("name"),
                "name_by_user": device.get("name_by_user"),
                "default_name": device.get("name"),
                "manufacturer": device.get("manufacturer"),
                "model": device.get("model"),
                "sw_version": device.get("sw_version"),
                "hw_version": device.get("hw_version"),
                "serial_number": device.get("serial_number"),
                "area_id": device.get("area_id"),
                "disabled_by": device.get("disabled_by"),
                "labels": device.get("labels", []),
                "config_entries": device.get("config_entries", []),
                "connections": device.get("connections", []),
                "identifiers": device.get("identifiers", []),
                "via_device_id": device.get("via_device_id"),
            }

            return {
                "success": True,
                "device": device_info,
                "entities": device_entities,
                "entity_count": len(device_entities),
                "message": f"Device '{device_info['name']}' has {len(device_entities)} entities",
            }

        except Exception as e:
            logger.error(f"Error getting device: {e}")
            return {
                "success": False,
                "error": f"Failed to get device: {str(e)}",
                "device_id": device_id,
            }

    @mcp.tool(annotations={"idempotentHint": True, "tags": ["system"], "title": "Update Device"})
    @log_tool_usage
    async def ha_update_device(
        device_id: Annotated[
            str,
            Field(description="Device ID to update"),
        ],
        name: Annotated[
            str | None,
            Field(
                description="New display name for the device (sets name_by_user)",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Area/room ID to assign the device to. Use empty string '' to unassign.",
                default=None,
            ),
        ] = None,
        disabled_by: Annotated[
            str | None,
            Field(
                description="Set to 'user' to disable, or None/empty string to enable",
                default=None,
            ),
        ] = None,
        labels: Annotated[
            str | list[str] | None,
            Field(
                description="Labels to assign to the device (replaces existing labels)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update device properties such as name, area, disabled state, or labels.

        IMPORTANT: Renaming a device does NOT rename its entities!
        Device and entity names are independent. To rename entities, use ha_rename_entity().

        Common workflow for full rename:
        1. ha_update_device(device_id="abc", name="Living Room Sensor")  # Rename device
        2. ha_rename_entity(entity_id="sensor.old", new_entity_id="sensor.living_room")  # Rename entities separately

        PARAMETERS:
        - name: Sets the user-defined display name (name_by_user)
        - area_id: Assigns device to an area/room. Use '' to remove from area.
        - disabled_by: Set to 'user' to disable, or empty to enable
        - labels: List of labels (replaces existing labels)

        EXAMPLES:
        - Rename device: ha_update_device("abc123", name="Living Room Hub")
        - Move to area: ha_update_device("abc123", area_id="living_room")
        - Disable device: ha_update_device("abc123", disabled_by="user")
        - Enable device: ha_update_device("abc123", disabled_by="")
        - Add labels: ha_update_device("abc123", labels=["important", "sensor"])
        """
        try:
            # Parse labels if provided as string
            if labels is not None:
                try:
                    labels = parse_string_list_param(labels, "labels")
                except ValueError as e:
                    return {"success": False, "error": f"Invalid labels parameter: {e}"}

            # Build update message
            message: dict[str, Any] = {
                "type": "config/device_registry/update",
                "device_id": device_id,
            }

            updates_made = []

            if name is not None:
                message["name_by_user"] = name if name else None
                updates_made.append(f"name='{name}'" if name else "name cleared")

            if area_id is not None:
                message["area_id"] = area_id if area_id else None
                updates_made.append(f"area_id='{area_id}'" if area_id else "area cleared")

            if disabled_by is not None:
                message["disabled_by"] = disabled_by if disabled_by else None
                updates_made.append(
                    f"disabled_by='{disabled_by}'" if disabled_by else "enabled"
                )

            if labels is not None:
                message["labels"] = labels
                updates_made.append(f"labels={labels}")

            if not updates_made:
                return {
                    "success": False,
                    "error": "No updates specified",
                    "suggestion": "Provide at least one of: name, area_id, disabled_by, or labels",
                }

            logger.info(f"Updating device {device_id}: {', '.join(updates_made)}")
            result = await client.send_websocket_message(message)

            if result.get("success"):
                # The result is the device entry directly (from dict_repr)
                device_entry = result.get("result", {})
                return {
                    "success": True,
                    "device_id": device_id,
                    "updates": updates_made,
                    "device_entry": {
                        "name": device_entry.get("name_by_user") or device_entry.get("name"),
                        "name_by_user": device_entry.get("name_by_user"),
                        "area_id": device_entry.get("area_id"),
                        "disabled_by": device_entry.get("disabled_by"),
                        "labels": device_entry.get("labels", []),
                    },
                    "message": f"Device updated: {', '.join(updates_made)}",
                    "note": "Remember: Device rename does NOT cascade to entities. Use ha_rename_entity() to rename entities.",
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
                    "error": f"Failed to update device: {error_msg}",
                    "device_id": device_id,
                    "suggestions": [
                        "Verify the device_id exists using ha_list_devices()",
                        "Check that area_id exists if specified",
                    ],
                }

        except Exception as e:
            logger.error(f"Error updating device: {e}")
            return {
                "success": False,
                "error": f"Device update failed: {str(e)}",
                "device_id": device_id,
            }

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["system"], "title": "Remove Device"})
    @log_tool_usage
    async def ha_remove_device(
        device_id: Annotated[
            str,
            Field(description="Device ID to remove from the registry"),
        ],
    ) -> dict[str, Any]:
        """
        Remove an orphaned device from the Home Assistant device registry.

        WARNING: This removes the device entry from the registry.
        - Use only for orphaned devices that are no longer connected
        - Active devices will typically be re-added by their integration
        - Associated entities may also be removed

        This uses the config entry removal which is the safe way to remove devices.
        If the device has multiple config entries, they must all be removed.

        EXAMPLES:
        - Remove orphaned device: ha_remove_device("abc123def456")

        NOTE: For most use cases, consider disabling the device instead:
        ha_update_device(device_id="abc123", disabled_by="user")
        """
        try:
            # First, get device details to find config entries
            list_message: dict[str, Any] = {"type": "config/device_registry/list"}
            list_result = await client.send_websocket_message(list_message)

            if not list_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
                }

            devices = list_result.get("result", [])
            device = next((d for d in devices if d.get("id") == device_id), None)

            if not device:
                return {
                    "success": False,
                    "error": f"Device not found: {device_id}",
                    "suggestion": "Use ha_list_devices() to find valid device IDs",
                }

            config_entries = device.get("config_entries", [])

            if not config_entries:
                return {
                    "success": False,
                    "error": "Device has no config entries - cannot be removed via this method",
                    "device_id": device_id,
                    "device_name": device.get("name_by_user") or device.get("name"),
                    "suggestion": "This device may be managed by an integration directly. Try disabling it instead.",
                }

            # Remove device from each config entry
            removal_results = []
            for config_entry_id in config_entries:
                remove_message: dict[str, Any] = {
                    "type": "config/device_registry/remove_config_entry",
                    "device_id": device_id,
                    "config_entry_id": config_entry_id,
                }

                remove_result = await client.send_websocket_message(remove_message)
                removal_results.append({
                    "config_entry_id": config_entry_id,
                    "success": remove_result.get("success", False),
                    "error": remove_result.get("error") if not remove_result.get("success") else None,
                })

            # Check if all removals succeeded
            all_succeeded = all(r["success"] for r in removal_results)
            any_succeeded = any(r["success"] for r in removal_results)

            if all_succeeded:
                return {
                    "success": True,
                    "device_id": device_id,
                    "device_name": device.get("name_by_user") or device.get("name"),
                    "config_entries_removed": len(config_entries),
                    "message": f"Successfully removed device from {len(config_entries)} config entry/entries",
                }
            elif any_succeeded:
                return {
                    "success": True,
                    "partial": True,
                    "device_id": device_id,
                    "device_name": device.get("name_by_user") or device.get("name"),
                    "removal_results": removal_results,
                    "message": "Device partially removed - some config entries could not be removed",
                }
            else:
                return {
                    "success": False,
                    "error": "Failed to remove device from any config entries",
                    "device_id": device_id,
                    "removal_results": removal_results,
                    "suggestion": "Device may be actively managed by its integration. Try disabling it instead.",
                }

        except Exception as e:
            logger.error(f"Error removing device: {e}")
            return {
                "success": False,
                "error": f"Device removal failed: {str(e)}",
                "device_id": device_id,
            }
