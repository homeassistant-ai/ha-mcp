"""
Entity Registry and Device Registry management tools for Home Assistant.

This module provides tools for:
- Renaming entities (changing entity_id) with optional voice assistant exposure migration
- Managing devices (list, get details, update, remove)
- Convenience wrapper for renaming both entity and device together

Important: Device renaming does NOT cascade to entities - they are independent registries.
"""

import logging
import re
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import coerce_bool_param, parse_string_list_param

# Known voice assistant identifiers
KNOWN_ASSISTANTS = ["conversation", "cloud.alexa", "cloud.google_assistant"]

logger = logging.getLogger(__name__)


def register_registry_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register entity registry and device registry management tools."""

    # Internal helper functions for reuse across tools
    async def _rename_entity_internal(
        entity_id: str,
        new_entity_id: str,
        name: str | None = None,
        icon: str | None = None,
        preserve_voice_exposure: bool = True,
    ) -> dict[str, Any]:
        """Internal implementation of entity rename with voice exposure migration."""
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

            # Step 1: Get current voice exposure settings BEFORE rename
            old_exposure: dict[str, bool] = {}
            exposure_migrated = False
            if preserve_voice_exposure:
                try:
                    exposure_list_msg: dict[str, Any] = {
                        "type": "homeassistant/expose_entity/list"
                    }
                    exposure_result = await client.send_websocket_message(
                        exposure_list_msg
                    )
                    if exposure_result.get("success"):
                        exposed_entities = exposure_result.get("result", {}).get(
                            "exposed_entities", {}
                        )
                        old_exposure = exposed_entities.get(entity_id, {})
                        if old_exposure:
                            logger.info(
                                f"Found voice exposure settings for {entity_id}: {old_exposure}"
                            )
                except Exception as exp_err:
                    logger.warning(
                        f"Could not fetch exposure settings: {exp_err}. "
                        "Continuing with rename without exposure migration."
                    )
                    old_exposure = {}

            # Step 2: Perform the entity rename
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

            if not result.get("success"):
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

            entity_entry = result.get("result", {}).get("entity_entry", {})

            # Step 3: Migrate voice exposure settings to the new entity_id
            exposure_migration_result: dict[str, Any] = {}
            if preserve_voice_exposure and old_exposure:
                try:
                    # Find which assistants the entity was exposed to
                    exposed_assistants = [
                        asst for asst in KNOWN_ASSISTANTS if old_exposure.get(asst)
                    ]
                    hidden_assistants = [
                        asst
                        for asst in KNOWN_ASSISTANTS
                        if old_exposure.get(asst) is False
                    ]

                    # Apply exposure to new entity
                    if exposed_assistants:
                        expose_msg: dict[str, Any] = {
                            "type": "homeassistant/expose_entity",
                            "assistants": exposed_assistants,
                            "entity_ids": [new_entity_id],
                            "should_expose": True,
                        }
                        expose_result = await client.send_websocket_message(expose_msg)
                        if expose_result.get("success"):
                            exposure_migrated = True
                            logger.info(
                                f"Migrated exposure to {exposed_assistants} for {new_entity_id}"
                            )
                        else:
                            logger.warning(
                                f"Failed to migrate exposure settings: {expose_result.get('error')}"
                            )

                    # Apply hidden settings to new entity
                    if hidden_assistants:
                        hide_msg: dict[str, Any] = {
                            "type": "homeassistant/expose_entity",
                            "assistants": hidden_assistants,
                            "entity_ids": [new_entity_id],
                            "should_expose": False,
                        }
                        hide_result = await client.send_websocket_message(hide_msg)
                        if hide_result.get("success"):
                            exposure_migrated = True
                            logger.info(
                                f"Migrated hidden settings to {hidden_assistants} for {new_entity_id}"
                            )

                    exposure_migration_result = {
                        "migrated": exposure_migrated,
                        "exposed_to": exposed_assistants,
                        "hidden_from": hidden_assistants,
                    }
                except Exception as migrate_err:
                    logger.warning(f"Error migrating exposure settings: {migrate_err}")
                    exposure_migration_result = {
                        "migrated": False,
                        "error": str(migrate_err),
                    }

            # Build response
            response: dict[str, Any] = {
                "success": True,
                "old_entity_id": entity_id,
                "new_entity_id": new_entity_id,
                "entity_entry": entity_entry,
                "message": f"Successfully renamed entity from {entity_id} to {new_entity_id}",
                "warning": "Remember to update any automations, scripts, or dashboards that reference the old entity_id",
            }

            if preserve_voice_exposure:
                if exposure_migration_result:
                    response["voice_exposure_migration"] = exposure_migration_result
                elif not old_exposure:
                    response["voice_exposure_migration"] = {
                        "migrated": False,
                        "note": "No custom voice exposure settings found for original entity",
                    }

            return response

        except Exception as e:
            logger.error(f"Error renaming entity: {e}")
            return {
                "success": False,
                "error": f"Entity rename failed: {str(e)}",
                "entity_id": entity_id,
            }

    async def _update_device_internal(
        device_id: str,
        name: str | None = None,
        area_id: str | None = None,
        disabled_by: str | None = None,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """Internal implementation of device update."""
        try:
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
                updates_made.append(
                    f"area_id='{area_id}'" if area_id else "area cleared"
                )

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
                device_entry = result.get("result", {})
                return {
                    "success": True,
                    "device_id": device_id,
                    "updates": updates_made,
                    "device_entry": {
                        "name": device_entry.get("name_by_user")
                        or device_entry.get("name"),
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

    @mcp.tool(annotations={"destructiveHint": True, "title": "Rename Entity"})
    @log_tool_usage
    async def ha_rename_entity(
        entity_id: Annotated[
            str,
            Field(description="Current entity ID to rename (e.g., 'light.old_name')"),
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
        preserve_voice_exposure: Annotated[
            bool | str | None,
            Field(
                description=(
                    "Migrate voice assistant exposure settings to the new entity_id. "
                    "Defaults to True. Set to False to skip exposure migration."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Rename a Home Assistant entity by changing its entity_id.

        Changes the entity_id (e.g., light.old_name -> light.new_name).
        The domain must remain the same - you cannot change a light to a switch.

        VOICE ASSISTANT EXPOSURE:
        By default, this function preserves voice assistant exposure settings
        (Alexa, Google Assistant, Assist) when renaming. The exposure settings
        are stored separately from the entity registry and must be migrated
        manually. Set preserve_voice_exposure=False to skip this migration.

        IMPORTANT LIMITATIONS:
        - References in automations/scripts/dashboards are NOT automatically updated
        - Entity history is preserved (HA 2022.4+)
        - Some entities cannot be renamed:
          - Entities without unique IDs
          - Entities disabled by integration

        EXAMPLES:
        - Rename light: ha_rename_entity("light.bedroom_1", "light.master_bedroom")
        - Rename with friendly name: ha_rename_entity("sensor.temp", "sensor.living_room_temp", name="Living Room Temperature")
        - Rename without exposure migration: ha_rename_entity("light.old", "light.new", preserve_voice_exposure=False)

        NOTE: This is different from renaming a device. Device and entity renaming are independent.
        Renaming a device does NOT rename its entities. See ha_update_device() for device renaming.
        For renaming both entity and device together, use ha_rename_entity_and_device().
        """
        # Parse preserve_voice_exposure (default True)
        should_preserve_exposure = coerce_bool_param(
            preserve_voice_exposure, "preserve_voice_exposure", default=True
        )
        # Delegate to internal implementation
        return await _rename_entity_internal(
            entity_id=entity_id,
            new_entity_id=new_entity_id,
            name=name,
            icon=icon,
            preserve_voice_exposure=should_preserve_exposure,
        )
    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["system"],
            "title": "Enable/Disable Entity",
        }
    )
    @log_tool_usage
    async def ha_set_entity_enabled(
        entity_id: Annotated[
            str,
            Field(description="Entity ID to enable or disable (e.g., 'binary_sensor.motion')"),
        ],
        enabled: Annotated[
            bool | str,
            Field(
                description="True to enable the entity, False to disable it"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Enable or disable a Home Assistant entity.

        Use this to enable disabled entities or disable entities you don't need.
        Disabled entities don't appear in the UI and don't consume resources.

        COMMON USE CASES:
        - Enable sensors that were auto-disabled by integrations
        - Disable entities you don't need (reduces clutter and resource usage)
        - Re-enable entities after troubleshooting

        EXAMPLES:
        - Enable entity: ha_set_entity_enabled("binary_sensor.side_yard_motion", True)
        - Disable entity: ha_set_entity_enabled("sensor.unused_sensor", False)

        NOTE: Some entities disabled by their integration cannot be enabled via this API.
        If enabling fails, check if the integration itself needs reconfiguration.
        """
        try:
            # Coerce enabled parameter
            if isinstance(enabled, str):
                enabled = enabled.lower() in ("true", "1", "yes", "on")

            # Build the WebSocket message
            message: dict[str, Any] = {
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "disabled_by": None if enabled else "user",
            }

            logger.info(f"{'Enabling' if enabled else 'Disabling'} entity {entity_id}")
            result = await client.send_websocket_message(message)

            if result.get("success"):
                entity_entry = result.get("result", {}).get("entity_entry", {})
                new_disabled_by = entity_entry.get("disabled_by")

                return {
                    "success": True,
                    "entity_id": entity_id,
                    "enabled": new_disabled_by is None,
                    "disabled_by": new_disabled_by,
                    "message": f"Entity {entity_id} {'enabled' if enabled else 'disabled'} successfully",
                    "entity_entry": {
                        "entity_id": entity_entry.get("entity_id"),
                        "name": entity_entry.get("name"),
                        "platform": entity_entry.get("platform"),
                        "disabled_by": new_disabled_by,
                    },
                    "note": "Entity state will be available after next Home Assistant state refresh" if enabled else None,
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
                    "error": f"Failed to {'enable' if enabled else 'disable'} entity: {error_msg}",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Verify the entity exists using ha_search_entities() or ha_get_device()",
                        "Check if the entity is disabled by its integration (cannot be enabled via API)",
                        "Some entities require the integration to be reconfigured to enable them",
                    ],
                }

        except Exception as e:
            logger.error(f"Error setting entity enabled state: {e}")
            return {
                "success": False,
                "error": f"Failed to {'enable' if enabled else 'disable'} entity: {str(e)}",
                "entity_id": entity_id,
            }
    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["system"],
            "title": "List Devices",
        }
    )
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
                        d for d in filtered_devices if d.get("area_id") == area_id
                    ]
                if manufacturer:
                    manufacturer_lower = manufacturer.lower()
                    filtered_devices = [
                        d
                        for d in filtered_devices
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
                    + (
                        f" (filtered from {len(devices)} total)"
                        if filters_applied
                        else ""
                    ),
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

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["system", "zigbee"],
            "title": "Get Device Details",
        }
    )
    @log_tool_usage
    async def ha_get_device(
        device_id: Annotated[
            str | None,
            Field(
                description="Device ID to retrieve details for",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to find the associated device for (e.g., 'light.living_room')",
                default=None,
            ),
        ] = None,
        integration: Annotated[
            str | None,
            Field(
                description="Filter/list devices by integration: 'zha', 'zigbee2mqtt', 'mqtt', 'hue', etc.",
                default=None,
            ),
        ] = None,
        area_id: Annotated[
            str | None,
            Field(
                description="Filter devices by area ID (e.g., 'living_room')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get device information including integration source, IEEE address, and friendly name.

        **Single device lookup:**
        - By device_id: ha_get_device(device_id="abc123")
        - By entity_id: ha_get_device(entity_id="light.living_room")

        **List devices by filter:**
        - All Zigbee2MQTT devices: ha_get_device(integration="zigbee2mqtt")
        - All ZHA devices: ha_get_device(integration="zha")
        - ZHA devices in kitchen: ha_get_device(integration="zha", area_id="kitchen")

        **Zigbee automation tips:**
        - ZHA triggers: Use `ieee_address` for zha_event triggers
        - Z2M triggers: Use `friendly_name` for MQTT topics (zigbee2mqtt/{friendly_name}/action)

        **Returns:**
        - device_id, name, manufacturer, model
        - integration_type: 'zha', 'zigbee2mqtt', 'mqtt', etc.
        - ieee_address: For ZHA/Z2M devices (use in automation triggers)
        - friendly_name: Z2M friendly name (use in MQTT topics)
        - entities: Associated entity IDs
        """
        try:
            # Get device registry
            list_message: dict[str, Any] = {"type": "config/device_registry/list"}
            list_result = await client.send_websocket_message(list_message)

            if not list_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to access device registry: {list_result.get('error', 'Unknown error')}",
                }

            all_devices = list_result.get("result", [])

            # Get entity registry
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)
            all_entities = (
                entity_result.get("result", []) if entity_result.get("success") else []
            )

            # Build entity -> device_id map
            entity_to_device: dict[str, str] = {}
            device_to_entities: dict[str, list[dict[str, Any]]] = {}
            for e in all_entities:
                eid = e.get("entity_id")
                did = e.get("device_id")
                if eid and did:
                    entity_to_device[eid] = did
                    if did not in device_to_entities:
                        device_to_entities[did] = []
                    device_to_entities[did].append(
                        {
                            "entity_id": eid,
                            "name": e.get("name") or e.get("original_name"),
                            "platform": e.get("platform"),
                        }
                    )

            # If entity_id provided, find the device_id
            if entity_id and not device_id:
                device_id = entity_to_device.get(entity_id)
                if not device_id:
                    return {
                        "success": False,
                        "error": f"Entity '{entity_id}' not found or has no associated device",
                        "suggestion": "Use ha_search_entities() to find valid entity IDs",
                    }

            # Helper function to extract integration info from a device
            def get_device_info(device: dict[str, Any]) -> dict[str, Any]:
                identifiers = device.get("identifiers", [])
                connections = device.get("connections", [])

                # Determine integration type and extract IEEE address
                integration_sources = []
                ieee_address = None
                friendly_name = device.get("name_by_user") or device.get("name")
                is_z2m = False

                for identifier in identifiers:
                    if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
                        domain = identifier[0]
                        value = str(identifier[1])
                        if domain not in integration_sources:
                            integration_sources.append(domain)

                        # ZHA: identifier is ["zha", "IEEE_ADDRESS"]
                        if domain == "zha":
                            ieee_address = value

                        # Z2M: identifier is ["mqtt", "zigbee2mqtt_0xIEEE"]
                        if domain == "mqtt" and "zigbee2mqtt" in value.lower():
                            is_z2m = True
                            # Extract IEEE from "zigbee2mqtt_0x..." or "zigbee2mqtt_bridge_0x..."
                            if "_0x" in value:
                                ieee_address = "0x" + value.split("_0x")[-1]

                # Also check connections for IEEE
                for connection in connections:
                    if isinstance(connection, (list, tuple)) and len(connection) >= 2:
                        if connection[0] == "ieee" and not ieee_address:
                            ieee_address = connection[1]

                # Determine primary integration type
                if "zha" in integration_sources:
                    integration_type = "zha"
                elif is_z2m:
                    integration_type = "zigbee2mqtt"
                elif "mqtt" in integration_sources:
                    integration_type = "mqtt"
                elif integration_sources:
                    integration_type = integration_sources[0]
                else:
                    integration_type = "unknown"

                device_info: dict[str, Any] = {
                    "device_id": device.get("id"),
                    "name": friendly_name,
                    "manufacturer": device.get("manufacturer"),
                    "model": device.get("model"),
                    "sw_version": device.get("sw_version"),
                    "area_id": device.get("area_id"),
                    "integration_type": integration_type,
                    "integration_sources": integration_sources,
                    "via_device_id": device.get("via_device_id"),
                }

                # Add Zigbee-specific info
                if ieee_address:
                    device_info["ieee_address"] = ieee_address

                if integration_type == "zigbee2mqtt":
                    device_info["friendly_name"] = friendly_name
                    device_info["mqtt_topic_hint"] = f"zigbee2mqtt/{friendly_name}/..."

                if integration_type == "zha" and ieee_address:
                    device_info["zha_trigger_hint"] = (
                        f"Use ieee '{ieee_address}' for zha_event triggers"
                    )

                return device_info

            # Single device lookup mode
            if device_id:
                device = next(
                    (d for d in all_devices if d.get("id") == device_id), None
                )
                if not device:
                    return {
                        "success": False,
                        "error": f"Device not found: {device_id}",
                        "suggestion": "Use ha_list_devices() to find valid device IDs",
                    }

                device_info = get_device_info(device)
                device_info["entities"] = device_to_entities.get(device_id, [])

                # Add extra fields for single lookup
                device_info["name_by_user"] = device.get("name_by_user")
                device_info["default_name"] = device.get("name")
                device_info["hw_version"] = device.get("hw_version")
                device_info["serial_number"] = device.get("serial_number")
                device_info["disabled_by"] = device.get("disabled_by")
                device_info["labels"] = device.get("labels", [])
                device_info["config_entries"] = device.get("config_entries", [])
                device_info["connections"] = device.get("connections", [])
                device_info["identifiers"] = device.get("identifiers", [])

                entities = device_info.get("entities", [])
                return {
                    "success": True,
                    "device": device_info,
                    "entities": entities,  # Also at top level for backward compatibility
                    "entity_count": len(entities),
                    "queried_by": "entity_id" if entity_id else "device_id",
                    "queried_entity_id": entity_id,
                }

            # List mode - must have at least integration filter
            if not integration:
                return {
                    "success": False,
                    "error": "Either device_id, entity_id, or integration filter is required",
                    "suggestion": "Use ha_get_device(integration='zigbee2mqtt') to list Z2M devices, or provide device_id/entity_id for single lookup",
                }

            # Filter devices by integration and area
            matched_devices = []
            integration_lower = integration.lower()

            for device in all_devices:
                # Apply area filter
                if area_id and device.get("area_id") != area_id:
                    continue

                device_info = get_device_info(device)

                # Match integration
                if (
                    integration_lower == "zigbee2mqtt"
                    and device_info["integration_type"] == "zigbee2mqtt"
                ):
                    device_info["entities"] = device_to_entities.get(
                        device.get("id"), []
                    )
                    matched_devices.append(device_info)
                elif (
                    integration_lower == "zha"
                    and device_info["integration_type"] == "zha"
                ):
                    device_info["entities"] = device_to_entities.get(
                        device.get("id"), []
                    )
                    matched_devices.append(device_info)
                elif integration_lower in device_info.get("integration_sources", []):
                    device_info["entities"] = device_to_entities.get(
                        device.get("id"), []
                    )
                    matched_devices.append(device_info)

            # Find bridge device for Z2M
            bridge_info = None
            if integration_lower == "zigbee2mqtt":
                for d in matched_devices:
                    if (
                        d.get("via_device_id") is None
                        and "bridge" in (d.get("name") or "").lower()
                    ):
                        bridge_info = {
                            "device_id": d.get("device_id"),
                            "name": d.get("name"),
                            "ieee_address": d.get("ieee_address"),
                        }
                        break

            result: dict[str, Any] = {
                "success": True,
                "count": len(matched_devices),
                "integration_filter": integration,
                "area_filter": area_id,
                "devices": matched_devices,
            }

            if bridge_info:
                result["bridge"] = bridge_info

            if integration_lower == "zigbee2mqtt":
                result["usage_hint"] = (
                    "Use 'friendly_name' for MQTT topics: zigbee2mqtt/{friendly_name}/action"
                )
            elif integration_lower == "zha":
                result["usage_hint"] = (
                    "Use 'ieee_address' for zha_event triggers in automations"
                )

            return result

        except Exception as e:
            logger.error(f"Error getting device: {e}")
            return {
                "success": False,
                "error": f"Failed to get device: {str(e)}",
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["system"],
            "title": "Update Device",
        }
    )
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
        # Parse labels if provided as string
        parsed_labels = None
        if labels is not None:
            try:
                parsed_labels = parse_string_list_param(labels, "labels")
            except ValueError as e:
                return {"success": False, "error": f"Invalid labels parameter: {e}"}

        # Delegate to internal implementation
        return await _update_device_internal(
            device_id=device_id,
            name=name,
            area_id=area_id,
            disabled_by=disabled_by,
            labels=parsed_labels,
        )

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["system"],
            "title": "Remove Device",
        }
    )
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
                removal_results.append(
                    {
                        "config_entry_id": config_entry_id,
                        "success": remove_result.get("success", False),
                        "error": (
                            remove_result.get("error")
                            if not remove_result.get("success")
                            else None
                        ),
                    }
                )

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

    @mcp.tool(
        annotations={"destructiveHint": True, "title": "Rename Entity and Device"}
    )
    @log_tool_usage
    async def ha_rename_entity_and_device(
        entity_id: Annotated[
            str,
            Field(
                description="Entity ID to rename (e.g., 'light.bedroom_lamp'). Used to find associated device."
            ),
        ],
        new_entity_id: Annotated[
            str,
            Field(
                description="New entity ID (e.g., 'light.master_bedroom_lamp'). Domain must match."
            ),
        ],
        new_device_name: Annotated[
            str | None,
            Field(
                description="New display name for the device. If not provided, device name is not changed.",
                default=None,
            ),
        ] = None,
        new_entity_name: Annotated[
            str | None,
            Field(
                description="New friendly name for the entity. If not provided, entity name is not changed.",
                default=None,
            ),
        ] = None,
        preserve_voice_exposure: Annotated[
            bool | str | None,
            Field(
                description=(
                    "Migrate voice assistant exposure settings to the new entity_id. "
                    "Defaults to True."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Convenience tool to rename both an entity and its associated device in one operation.

        This tool:
        1. Finds the device associated with the entity
        2. Renames the entity (changing entity_id)
        3. Renames the device (changing display name)
        4. Preserves voice assistant exposure settings (by default)

        WHEN TO USE:
        - When you want to rename a device and its primary entity together
        - For smart home devices where device and entity should have matching names

        IMPORTANT:
        - If the entity has multiple associated entities (e.g., a smart bulb with brightness, color, etc.),
          only the specified entity is renamed. Other entities retain their original IDs.
        - If the entity has no associated device, only the entity is renamed.

        EXAMPLES:
        - Rename lamp: ha_rename_entity_and_device("light.bedroom_1", "light.master_bedroom", "Master Bedroom Lamp")
        - Rename only entity_id: ha_rename_entity_and_device("sensor.temp", "sensor.living_room_temp")

        See also:
        - ha_rename_entity() - Rename only the entity
        - ha_update_device() - Update only the device
        """
        try:
            # Parse preserve_voice_exposure (default True)
            should_preserve_exposure = coerce_bool_param(
                preserve_voice_exposure, "preserve_voice_exposure", default=True
            )

            results: dict[str, Any] = {
                "entity_rename": None,
                "device_rename": None,
            }

            # Step 1: Find the device associated with this entity
            entity_registry_msg: dict[str, Any] = {
                "type": "config/entity_registry/list"
            }
            entity_registry_result = await client.send_websocket_message(
                entity_registry_msg
            )

            device_id = None
            if entity_registry_result.get("success"):
                entities = entity_registry_result.get("result", [])
                for ent in entities:
                    if ent.get("entity_id") == entity_id:
                        device_id = ent.get("device_id")
                        break

            # Step 2: Rename the entity using internal helper (handles voice exposure migration)
            entity_rename_result = await _rename_entity_internal(
                entity_id=entity_id,
                new_entity_id=new_entity_id,
                name=new_entity_name,
                preserve_voice_exposure=should_preserve_exposure,
            )

            results["entity_rename"] = entity_rename_result

            if not entity_rename_result.get("success"):
                return {
                    "success": False,
                    "error": f"Entity rename failed: {entity_rename_result.get('error')}",
                    "results": results,
                }

            # Step 3: Rename the device if we found one and a new name was provided
            if device_id and new_device_name:
                device_rename_result = await _update_device_internal(
                    device_id=device_id,
                    name=new_device_name,
                )
                results["device_rename"] = device_rename_result

                if not device_rename_result.get("success"):
                    # Entity was renamed but device rename failed - report partial success
                    return {
                        "success": True,
                        "partial": True,
                        "message": "Entity renamed successfully but device rename failed",
                        "old_entity_id": entity_id,
                        "new_entity_id": new_entity_id,
                        "device_id": device_id,
                        "results": results,
                        "warning": f"Device rename failed: {device_rename_result.get('error')}",
                    }
            elif device_id and not new_device_name:
                results["device_rename"] = {
                    "skipped": True,
                    "reason": "No new_device_name provided",
                    "device_id": device_id,
                }
            elif not device_id:
                results["device_rename"] = {
                    "skipped": True,
                    "reason": "Entity has no associated device",
                }

            # Build success response
            response: dict[str, Any] = {
                "success": True,
                "old_entity_id": entity_id,
                "new_entity_id": new_entity_id,
                "device_id": device_id,
                "results": results,
            }

            if (
                device_id
                and new_device_name
                and results["device_rename"].get("success")
            ):
                response["message"] = (
                    f"Successfully renamed entity ({entity_id} -> {new_entity_id}) "
                    f"and device ({new_device_name})"
                )
            elif device_id:
                response["message"] = (
                    f"Successfully renamed entity ({entity_id} -> {new_entity_id}). "
                    f"Device name was not changed."
                )
            else:
                response["message"] = (
                    f"Successfully renamed entity ({entity_id} -> {new_entity_id}). "
                    f"No associated device found."
                )

            # Include voice exposure migration info if available
            if entity_rename_result.get("voice_exposure_migration"):
                response["voice_exposure_migration"] = entity_rename_result[
                    "voice_exposure_migration"
                ]

            response["warning"] = (
                "Remember to update any automations, scripts, or dashboards "
                "that reference the old entity_id"
            )

            return response

        except Exception as e:
            logger.error(f"Error in rename entity and device: {e}")
            return {
                "success": False,
                "error": f"Rename failed: {str(e)}",
                "entity_id": entity_id,
            }
