"""
ZHA (Zigbee Home Automation) device detection and management tools.

This module provides tools to:
- List all ZHA devices with their IEEE addresses
- Get integration source information for devices/entities
- Support automation creation with ZHA event triggers

ZHA devices are identified by:
- Having 'zha' in their device identifiers (format: ["zha", "IEEE_ADDRESS"])
- Having IEEE addresses in their connections (format: ["ieee", "XX:XX:XX:XX:XX:XX:XX:XX"])
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_zha_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register ZHA device detection and management tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["zha"], "title": "Get ZHA Devices"})
    @log_tool_usage
    async def ha_get_zha_devices(
        area_id: Annotated[
            str | None,
            Field(
                description="Filter devices by area ID (e.g., 'living_room')",
                default=None,
            ),
        ] = None,
        include_entities: Annotated[
            bool,
            Field(
                description="Include list of entities for each device (default: True)",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        List all ZHA (Zigbee Home Automation) devices with their IEEE addresses.

        This tool is essential for creating automations with zha_event triggers,
        which require the device's IEEE address.

        **Use Cases:**
        - "Create automation triggered by ZHA button press" -> needs IEEE for zha_event trigger
        - "Which of my devices are ZHA?" -> list all Zigbee devices
        - "Get IEEE address for my remote" -> find specific device's IEEE

        **ZHA Event Trigger Example:**
        ```yaml
        trigger:
          - platform: device
            device_id: <device_id>
            domain: zha
            type: remote_button_short_press
            subtype: button_1
        # OR using IEEE directly:
        trigger:
          - platform: event
            event_type: zha_event
            event_data:
              device_ieee: "XX:XX:XX:XX:XX:XX:XX:XX"
              command: "on"
        ```

        **Returns:**
        - List of ZHA devices with:
          - device_id: Home Assistant device ID
          - name: Device display name
          - ieee_address: IEEE address for zha_event triggers
          - manufacturer, model: Device identification
          - area_id: Room/area assignment
          - entities: Associated entity IDs (if include_entities=True)

        **Examples:**
        - List all ZHA devices: ha_get_zha_devices()
        - Filter by area: ha_get_zha_devices(area_id="living_room")
        - Quick list without entities: ha_get_zha_devices(include_entities=False)
        """
        try:
            # Get device registry
            device_message: dict[str, Any] = {"type": "config/device_registry/list"}
            device_result = await client.send_websocket_message(device_message)

            if not device_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to access device registry: {device_result.get('error', 'Unknown error')}",
                }

            devices = device_result.get("result", [])

            # Get entity registry if needed
            entity_map: dict[str, list[dict[str, str]]] = {}
            if include_entities:
                entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
                entity_result = await client.send_websocket_message(entity_message)
                if entity_result.get("success"):
                    for entity in entity_result.get("result", []):
                        device_id = entity.get("device_id")
                        if device_id:
                            if device_id not in entity_map:
                                entity_map[device_id] = []
                            entity_map[device_id].append({
                                "entity_id": entity.get("entity_id"),
                                "name": entity.get("name") or entity.get("original_name"),
                                "platform": entity.get("platform"),
                            })

            # Filter for ZHA devices and extract IEEE addresses
            zha_devices = []
            for device in devices:
                # Check if this is a ZHA device by looking at identifiers
                identifiers = device.get("identifiers", [])
                connections = device.get("connections", [])

                # Look for ZHA identifier
                is_zha = False
                ieee_address = None

                for identifier in identifiers:
                    if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
                        if identifier[0] == "zha":
                            is_zha = True
                            # The second element is typically the IEEE address
                            ieee_address = identifier[1]
                            break

                # Also check connections for IEEE address (format: ["ieee", "XX:XX:..."])
                # This supplements the identifier-based IEEE address if not already found
                if not ieee_address:
                    for connection in connections:
                        if isinstance(connection, (list, tuple)) and len(connection) >= 2:
                            if connection[0] == "ieee":
                                ieee_address = connection[1]
                                break

                if not is_zha:
                    continue

                # Apply area filter if specified
                if area_id and device.get("area_id") != area_id:
                    continue

                device_info: dict[str, Any] = {
                    "device_id": device.get("id"),
                    "name": device.get("name_by_user") or device.get("name"),
                    "ieee_address": ieee_address,
                    "manufacturer": device.get("manufacturer"),
                    "model": device.get("model"),
                    "sw_version": device.get("sw_version"),
                    "area_id": device.get("area_id"),
                    "via_device_id": device.get("via_device_id"),
                }

                if include_entities:
                    device_info["entities"] = entity_map.get(device.get("id"), [])

                zha_devices.append(device_info)

            return {
                "success": True,
                "count": len(zha_devices),
                "devices": zha_devices,
                "area_filter": area_id,
                "message": f"Found {len(zha_devices)} ZHA device(s)"
                + (f" in area '{area_id}'" if area_id else ""),
                "usage_hint": "Use 'ieee_address' field for zha_event triggers in automations",
            }

        except Exception as e:
            logger.error(f"Error listing ZHA devices: {e}")
            return {
                "success": False,
                "error": f"Failed to list ZHA devices: {str(e)}",
                "suggestions": [
                    "Verify Home Assistant connection is working",
                    "Check that ZHA integration is configured",
                    "Try ha_list_integrations(query='zha') to verify ZHA is loaded",
                ],
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["system"], "title": "Get Device Integration"})
    @log_tool_usage
    async def ha_get_device_integration(
        device_id: Annotated[
            str | None,
            Field(
                description="Device ID to get integration info for",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to get integration info for (will lookup its device)",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get integration source information for a device or entity.

        Identifies which integration(s) a device belongs to (e.g., zha, mqtt, hue).
        Useful for determining how to create triggers or understand device capabilities.

        **Parameters:**
        - device_id: Direct device ID lookup
        - entity_id: Entity ID - will find the associated device

        At least one of device_id or entity_id must be provided.

        **Returns:**
        - Device identification (name, manufacturer, model)
        - Integration sources (domains like 'zha', 'mqtt', 'hue')
        - IEEE address if available (for ZHA devices)
        - Config entries associated with the device

        **Use Cases:**
        - Determine if a device is ZHA, Z2M (MQTT), or other integration
        - Get IEEE address for ZHA event triggers
        - Understand device capabilities based on integration type

        **Examples:**
        - By device: ha_get_device_integration(device_id="abc123")
        - By entity: ha_get_device_integration(entity_id="light.living_room")
        """
        try:
            if not device_id and not entity_id:
                return {
                    "success": False,
                    "error": "Either device_id or entity_id must be provided",
                }

            # If entity_id provided, look up the device_id
            if entity_id and not device_id:
                entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
                entity_result = await client.send_websocket_message(entity_message)

                if not entity_result.get("success"):
                    return {
                        "success": False,
                        "error": f"Failed to access entity registry: {entity_result.get('error', 'Unknown error')}",
                    }

                entities = entity_result.get("result", [])
                entity_entry = next(
                    (e for e in entities if e.get("entity_id") == entity_id),
                    None
                )

                if not entity_entry:
                    return {
                        "success": False,
                        "error": f"Entity not found: {entity_id}",
                        "suggestion": "Use ha_search_entities() to find valid entity IDs",
                    }

                device_id = entity_entry.get("device_id")
                if not device_id:
                    return {
                        "success": False,
                        "error": f"Entity {entity_id} is not associated with a device",
                        "entity_platform": entity_entry.get("platform"),
                    }

            # Get device registry
            device_message: dict[str, Any] = {"type": "config/device_registry/list"}
            device_result = await client.send_websocket_message(device_message)

            if not device_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to access device registry: {device_result.get('error', 'Unknown error')}",
                }

            devices = device_result.get("result", [])
            device = next((d for d in devices if d.get("id") == device_id), None)

            if not device:
                return {
                    "success": False,
                    "error": f"Device not found: {device_id}",
                    "suggestion": "Use ha_list_devices() to find valid device IDs",
                }

            # Extract integration information
            identifiers = device.get("identifiers", [])
            connections = device.get("connections", [])
            config_entries = device.get("config_entries", [])

            # Determine integration sources from identifiers
            integration_sources = []
            ieee_address = None

            for identifier in identifiers:
                if isinstance(identifier, (list, tuple)) and len(identifier) >= 2:
                    domain = identifier[0]
                    if domain not in integration_sources:
                        integration_sources.append(domain)
                    # Capture IEEE for ZHA
                    if domain == "zha":
                        ieee_address = identifier[1]

            # Check connections for IEEE
            for connection in connections:
                if isinstance(connection, (list, tuple)) and len(connection) >= 2:
                    if connection[0] == "ieee" and not ieee_address:
                        ieee_address = connection[1]

            # Determine primary integration type
            is_zha = "zha" in integration_sources
            is_mqtt = "mqtt" in integration_sources
            is_z2m = is_mqtt and any(
                "zigbee2mqtt" in str(i).lower()
                for i in identifiers
            )

            integration_type = "unknown"
            if is_zha:
                integration_type = "zha"
            elif is_z2m:
                integration_type = "zigbee2mqtt"
            elif is_mqtt:
                integration_type = "mqtt"
            elif integration_sources:
                integration_type = integration_sources[0]

            result: dict[str, Any] = {
                "success": True,
                "device_id": device_id,
                "name": device.get("name_by_user") or device.get("name"),
                "manufacturer": device.get("manufacturer"),
                "model": device.get("model"),
                "integration_type": integration_type,
                "integration_sources": integration_sources,
                "config_entries": config_entries,
                "identifiers": identifiers,
                "connections": connections,
            }

            # Add IEEE address if available (important for ZHA triggers)
            if ieee_address:
                result["ieee_address"] = ieee_address
                result["zha_trigger_hint"] = (
                    f"Use ieee_address '{ieee_address}' for zha_event triggers"
                )

            # Add original entity_id if provided
            if entity_id:
                result["queried_entity_id"] = entity_id

            return result

        except Exception as e:
            logger.error(f"Error getting device integration: {e}")
            return {
                "success": False,
                "error": f"Failed to get device integration: {str(e)}",
                "device_id": device_id,
                "entity_id": entity_id,
            }
