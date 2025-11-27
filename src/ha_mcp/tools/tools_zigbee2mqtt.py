"""
Zigbee2MQTT (Z2M) integration tools for Home Assistant MCP server.

This module provides tools to discover and query Zigbee2MQTT devices,
enabling AI agents to properly configure MQTT-based automation triggers
using Z2M friendly names.

Detection Methods:
1. Entity attribute inspection - Check for Z2M-specific patterns in entity IDs/attributes
2. Device registry inspection - Use device registry to identify MQTT-sourced entities
3. Integration detection - Filter entities by mqtt domain and Z2M patterns
"""

import logging
import re
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Common Z2M entity patterns
Z2M_ENTITY_PATTERNS = [
    r"^sensor\..*_linkquality$",  # Link quality sensors
    r"^sensor\..*_battery$",  # Battery sensors from Z2M
    r"^binary_sensor\..*_contact$",  # Contact sensors
    r"^binary_sensor\..*_occupancy$",  # Motion/occupancy sensors
    r"^binary_sensor\..*_vibration$",  # Vibration sensors
    r"^light\..*_light$",  # Z2M lights often end with _light
    r"^switch\..*_switch$",  # Z2M switches
    r"^sensor\..*_action$",  # Button action sensors
    r"^sensor\..*_click$",  # Button click sensors
    r"^sensor\..*_power$",  # Power monitoring
    r"^sensor\..*_energy$",  # Energy monitoring
    r"^sensor\..*_voltage$",  # Voltage monitoring
    r"^sensor\..*_current$",  # Current monitoring
    r"^sensor\..*_temperature$",  # Temperature sensors
    r"^sensor\..*_humidity$",  # Humidity sensors
    r"^sensor\..*_pressure$",  # Pressure sensors
    r"^sensor\..*_illuminance.*$",  # Light level sensors
    r"^cover\..*$",  # Covers/blinds
    r"^climate\..*$",  # Climate devices
    r"^lock\..*$",  # Smart locks
]

# Z2M-specific attribute names that indicate a Z2M device
Z2M_ATTRIBUTE_INDICATORS = [
    "linkquality",
    "update_available",
    "update",
    "last_seen",
]

# Common Z2M device manufacturers
Z2M_MANUFACTURERS = [
    "Xiaomi",
    "Aqara",
    "IKEA",
    "IKEA of Sweden",
    "Philips",
    "Signify Netherlands B.V.",
    "SONOFF",
    "SONOFF Zigbee",
    "TuYa",
    "TUYA",
    "_TZ",  # TuYa prefix
    "LUMI",
    "GLEDOPTO",
    "Legrand",
    "Schneider Electric",
    "Develco Products A/S",
    "Konke",
    "Heiman",
    "SmartThings",
    "OSRAM",
    "LEDVANCE",
    "innr",
    "Innr",
    "Sengled",
    "Third Reality",
    "eWeLink",
    "Moes",
    "Zemismart",
]


def _extract_friendly_name_from_entity_id(entity_id: str) -> str:
    """
    Extract a potential Z2M friendly name from an entity ID.

    Z2M typically creates entity IDs like:
    - sensor.living_room_motion_occupancy -> living_room_motion
    - light.bedroom_light -> bedroom
    - sensor.kitchen_temp_sensor_temperature -> kitchen_temp_sensor

    Args:
        entity_id: The entity ID to parse

    Returns:
        Extracted friendly name suitable for MQTT topics
    """
    # Remove domain prefix
    _, object_id = entity_id.split(".", 1)

    # Common Z2M suffixes to strip
    suffixes = [
        "_linkquality",
        "_battery",
        "_contact",
        "_occupancy",
        "_vibration",
        "_light",
        "_switch",
        "_action",
        "_click",
        "_power",
        "_energy",
        "_voltage",
        "_current",
        "_temperature",
        "_humidity",
        "_pressure",
        "_illuminance",
        "_illuminance_lux",
        "_lux",
    ]

    name = object_id
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break

    return name


def _is_likely_z2m_entity(entity: dict[str, Any]) -> bool:
    """
    Determine if an entity is likely from Zigbee2MQTT based on patterns.

    Args:
        entity: Entity state dictionary

    Returns:
        True if entity appears to be from Z2M
    """
    entity_id = entity.get("entity_id", "")
    attributes = entity.get("attributes", {})

    # Check for Z2M-specific attributes
    for indicator in Z2M_ATTRIBUTE_INDICATORS:
        if indicator in attributes:
            return True

    # Check entity ID patterns
    for pattern in Z2M_ENTITY_PATTERNS:
        if re.match(pattern, entity_id):
            return True

    return False


def _is_z2m_device(device: dict[str, Any]) -> bool:
    """
    Determine if a device is from Zigbee2MQTT based on device registry info.

    Args:
        device: Device registry entry

    Returns:
        True if device appears to be from Z2M
    """
    # Check identifiers for zigbee2mqtt
    identifiers = device.get("identifiers", [])
    for identifier in identifiers:
        if isinstance(identifier, (list, tuple)) and len(identifier) >= 1:
            if "mqtt" in str(identifier[0]).lower():
                return True
            if "zigbee2mqtt" in str(identifier).lower():
                return True

    # Check manufacturer
    manufacturer = device.get("manufacturer", "") or ""
    for z2m_manufacturer in Z2M_MANUFACTURERS:
        if z2m_manufacturer.lower() in manufacturer.lower():
            return True

    # Check model for Z2M patterns
    model = device.get("model", "") or ""
    if model:
        # TuYa devices often have model starting with TS
        if model.startswith("TS") and len(model) >= 4:
            return True
        # Xiaomi/Aqara model patterns
        if model.startswith(("lumi.", "RTCGQ", "MCCGQ", "WXKG", "WSDCGQ")):
            return True

    # Check via_device_id (Z2M coordinator)
    # Devices connected via Z2M will have a via_device pointing to the coordinator
    if device.get("via_device_id"):
        return True

    return False


def _build_mqtt_topic(friendly_name: str, suffix: str = "") -> str:
    """
    Build an MQTT topic for a Z2M device.

    Args:
        friendly_name: The Z2M friendly name
        suffix: Optional topic suffix (e.g., 'set', 'get', 'action')

    Returns:
        Full MQTT topic string
    """
    base_topic = f"zigbee2mqtt/{friendly_name}"
    if suffix:
        return f"{base_topic}/{suffix}"
    return base_topic


def register_zigbee2mqtt_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Zigbee2MQTT discovery and query tools with the MCP server."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_z2m_list_devices(
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
                description="Include list of entities for each device",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        List all Zigbee2MQTT devices detected in Home Assistant.

        Identifies Z2M devices by checking:
        - Device identifiers containing 'mqtt' or 'zigbee2mqtt'
        - Known Z2M manufacturers (Xiaomi, Aqara, IKEA, Philips, etc.)
        - Device model patterns typical of Z2M devices
        - Devices connected via a coordinator (via_device_id)

        Returns device information including:
        - device_id: Home Assistant device ID
        - name: Device display name
        - friendly_name: Extracted Z2M friendly name for MQTT topics
        - manufacturer, model: Device identification
        - mqtt_topic: Base MQTT topic for this device
        - area_id: Assigned area/room

        EXAMPLES:
        - List all Z2M devices: ha_z2m_list_devices()
        - Filter by area: ha_z2m_list_devices(area_id="living_room")
        - Include entities: ha_z2m_list_devices(include_entities=True)

        USE CASES:
        - "Which devices are connected via Zigbee2MQTT?"
        - "Get the MQTT topic for my bedroom motion sensor"
        - "Create automation triggered by Z2M button"
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

            all_devices = device_result.get("result", [])

            # Get entity registry for entity mapping
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)
            all_entities = (
                entity_result.get("result", [])
                if entity_result.get("success")
                else []
            )

            # Build entity-to-device mapping
            device_entities: dict[str, list[dict[str, Any]]] = {}
            for entity in all_entities:
                device_id = entity.get("device_id")
                if device_id:
                    if device_id not in device_entities:
                        device_entities[device_id] = []
                    device_entities[device_id].append(
                        {
                            "entity_id": entity.get("entity_id"),
                            "name": entity.get("name") or entity.get("original_name"),
                            "platform": entity.get("platform"),
                            "disabled_by": entity.get("disabled_by"),
                        }
                    )

            # Filter Z2M devices
            z2m_devices = []
            for device in all_devices:
                if not _is_z2m_device(device):
                    continue

                # Apply area filter if specified
                if area_id and device.get("area_id") != area_id:
                    continue

                device_id = device.get("id")
                name = device.get("name_by_user") or device.get("name", "")

                # Extract friendly name for MQTT
                # First try to get from device name, then from entity patterns
                friendly_name = name.lower().replace(" ", "_").replace("-", "_")

                # Check entities for better friendly name extraction
                entities = device_entities.get(device_id, [])
                if entities:
                    # Use the first entity to extract friendly name
                    first_entity_id = entities[0].get("entity_id", "")
                    if first_entity_id:
                        extracted = _extract_friendly_name_from_entity_id(
                            first_entity_id
                        )
                        if extracted:
                            friendly_name = extracted

                device_info: dict[str, Any] = {
                    "device_id": device_id,
                    "name": name,
                    "friendly_name": friendly_name,
                    "manufacturer": device.get("manufacturer"),
                    "model": device.get("model"),
                    "sw_version": device.get("sw_version"),
                    "area_id": device.get("area_id"),
                    "mqtt_base_topic": _build_mqtt_topic(friendly_name),
                    "mqtt_action_topic": _build_mqtt_topic(friendly_name, "action"),
                    "via_device_id": device.get("via_device_id"),
                }

                if include_entities:
                    device_info["entities"] = entities
                    device_info["entity_count"] = len(entities)

                z2m_devices.append(device_info)

            # Group by area for summary
            area_summary: dict[str, int] = {}
            for device in z2m_devices:
                area = device.get("area_id") or "unassigned"
                area_summary[area] = area_summary.get(area, 0) + 1

            return {
                "success": True,
                "count": len(z2m_devices),
                "total_devices_checked": len(all_devices),
                "devices": z2m_devices,
                "area_summary": area_summary,
                "filter_applied": {"area_id": area_id} if area_id else None,
                "message": f"Found {len(z2m_devices)} Zigbee2MQTT device(s)",
            }

        except Exception as e:
            logger.error(f"Failed to list Z2M devices: {e}")
            return {
                "success": False,
                "error": f"Failed to list Z2M devices: {str(e)}",
                "suggestions": [
                    "Verify Home Assistant connection is working",
                    "Ensure Zigbee2MQTT integration is set up",
                    "Check that MQTT integration is configured",
                ],
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_z2m_get_device(
        device_id: Annotated[
            str | None,
            Field(
                description="Device ID to look up (from ha_z2m_list_devices)",
                default=None,
            ),
        ] = None,
        friendly_name: Annotated[
            str | None,
            Field(
                description="Z2M friendly name to search for",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="Entity ID to find the parent Z2M device for",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific Zigbee2MQTT device.

        Lookup by one of:
        - device_id: Direct lookup by Home Assistant device ID
        - friendly_name: Search by Z2M friendly name (fuzzy match)
        - entity_id: Find the parent device for an entity

        Returns comprehensive device details including:
        - All device metadata (name, manufacturer, model, etc.)
        - Z2M friendly name for MQTT topics
        - MQTT topics for common operations (state, set, get, action)
        - All entities belonging to this device
        - Automation trigger examples

        EXAMPLES:
        - By device ID: ha_z2m_get_device(device_id="abc123")
        - By friendly name: ha_z2m_get_device(friendly_name="living_room_motion")
        - By entity: ha_z2m_get_device(entity_id="binary_sensor.motion_occupancy")

        USE CASES:
        - "Get MQTT topic for my motion sensor"
        - "How do I trigger automation from Z2M button?"
        - "What's the friendly name for this device?"
        """
        try:
            # Validate input - need at least one lookup method
            if not any([device_id, friendly_name, entity_id]):
                return {
                    "success": False,
                    "error": "Must provide one of: device_id, friendly_name, or entity_id",
                    "suggestion": "Use ha_z2m_list_devices() to find device IDs",
                }

            # Get device registry
            device_message: dict[str, Any] = {"type": "config/device_registry/list"}
            device_result = await client.send_websocket_message(device_message)

            if not device_result.get("success"):
                return {
                    "success": False,
                    "error": f"Failed to access device registry: {device_result.get('error', 'Unknown error')}",
                }

            all_devices = device_result.get("result", [])

            # Get entity registry
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)
            all_entities = (
                entity_result.get("result", [])
                if entity_result.get("success")
                else []
            )

            # Find target device
            target_device = None
            search_method = None

            if device_id:
                # Direct lookup by device ID
                search_method = f"device_id={device_id}"
                for device in all_devices:
                    if device.get("id") == device_id:
                        target_device = device
                        break

            elif entity_id:
                # Find device via entity
                search_method = f"entity_id={entity_id}"
                target_device_id = None
                for entity in all_entities:
                    if entity.get("entity_id") == entity_id:
                        target_device_id = entity.get("device_id")
                        break

                if target_device_id:
                    for device in all_devices:
                        if device.get("id") == target_device_id:
                            target_device = device
                            break
                else:
                    return {
                        "success": False,
                        "error": f"Entity not found: {entity_id}",
                        "suggestion": "Verify the entity ID is correct",
                    }

            elif friendly_name:
                # Fuzzy search by friendly name
                search_method = f"friendly_name={friendly_name}"
                friendly_name_lower = friendly_name.lower().replace(" ", "_")

                best_match = None
                best_score = 0

                for device in all_devices:
                    if not _is_z2m_device(device):
                        continue

                    device_name = (
                        device.get("name_by_user") or device.get("name", "")
                    ).lower()
                    device_name_normalized = device_name.replace(" ", "_").replace(
                        "-", "_"
                    )

                    # Exact match
                    if device_name_normalized == friendly_name_lower:
                        best_match = device
                        best_score = 100
                        break

                    # Partial match scoring
                    if friendly_name_lower in device_name_normalized:
                        score = len(friendly_name_lower) / len(device_name_normalized) * 80
                        if score > best_score:
                            best_match = device
                            best_score = score
                    elif device_name_normalized in friendly_name_lower:
                        score = (
                            len(device_name_normalized) / len(friendly_name_lower) * 70
                        )
                        if score > best_score:
                            best_match = device
                            best_score = score

                target_device = best_match

            if not target_device:
                return {
                    "success": False,
                    "error": f"Device not found ({search_method})",
                    "suggestion": "Use ha_z2m_list_devices() to see available devices",
                }

            # Check if it's a Z2M device
            if not _is_z2m_device(target_device):
                return {
                    "success": False,
                    "error": "Device found but does not appear to be a Zigbee2MQTT device",
                    "device_id": target_device.get("id"),
                    "device_name": target_device.get("name"),
                    "suggestion": "Use ha_list_devices() for non-Z2M devices",
                }

            # Get entities for this device
            device_id_resolved = target_device.get("id")
            device_entities = [
                {
                    "entity_id": e.get("entity_id"),
                    "name": e.get("name") or e.get("original_name"),
                    "platform": e.get("platform"),
                    "disabled_by": e.get("disabled_by"),
                }
                for e in all_entities
                if e.get("device_id") == device_id_resolved
            ]

            # Extract friendly name
            name = target_device.get("name_by_user") or target_device.get("name", "")
            extracted_friendly_name = name.lower().replace(" ", "_").replace("-", "_")

            # Try to get better friendly name from entities
            if device_entities:
                first_entity_id = device_entities[0].get("entity_id", "")
                if first_entity_id:
                    extracted = _extract_friendly_name_from_entity_id(first_entity_id)
                    if extracted:
                        extracted_friendly_name = extracted

            # Build comprehensive response
            mqtt_topics = {
                "base": _build_mqtt_topic(extracted_friendly_name),
                "state": _build_mqtt_topic(extracted_friendly_name),
                "set": _build_mqtt_topic(extracted_friendly_name, "set"),
                "get": _build_mqtt_topic(extracted_friendly_name, "get"),
                "action": _build_mqtt_topic(extracted_friendly_name, "action"),
                "availability": _build_mqtt_topic(extracted_friendly_name, "availability"),
            }

            # Generate automation trigger examples
            automation_examples = {
                "mqtt_action_trigger": {
                    "platform": "mqtt",
                    "topic": mqtt_topics["action"],
                    "payload": "single",  # Example payload
                },
                "mqtt_state_trigger": {
                    "platform": "mqtt",
                    "topic": mqtt_topics["base"],
                },
                "note": "Actual payloads depend on device type (buttons: single/double/long, sensors: on/off, etc.)",
            }

            return {
                "success": True,
                "device": {
                    "device_id": device_id_resolved,
                    "name": name,
                    "name_by_user": target_device.get("name_by_user"),
                    "default_name": target_device.get("name"),
                    "manufacturer": target_device.get("manufacturer"),
                    "model": target_device.get("model"),
                    "sw_version": target_device.get("sw_version"),
                    "hw_version": target_device.get("hw_version"),
                    "area_id": target_device.get("area_id"),
                    "via_device_id": target_device.get("via_device_id"),
                    "identifiers": target_device.get("identifiers", []),
                },
                "zigbee2mqtt": {
                    "friendly_name": extracted_friendly_name,
                    "mqtt_topics": mqtt_topics,
                },
                "entities": device_entities,
                "entity_count": len(device_entities),
                "automation_examples": automation_examples,
                "search_method": search_method,
            }

        except Exception as e:
            logger.error(f"Failed to get Z2M device: {e}")
            return {
                "success": False,
                "error": f"Failed to get Z2M device: {str(e)}",
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_z2m_get_mqtt_topic(
        entity_id: Annotated[
            str,
            Field(
                description="Entity ID to get MQTT topic for (e.g., 'sensor.motion_action')"
            ),
        ],
        topic_type: Annotated[
            str,
            Field(
                description="Type of topic: 'base', 'set', 'get', 'action', 'availability'",
                default="base",
            ),
        ] = "base",
    ) -> dict[str, Any]:
        """
        Get the MQTT topic for a Zigbee2MQTT entity.

        Quick helper to get the MQTT topic needed for automations without
        fetching full device details.

        Topic types:
        - base: Main state topic (zigbee2mqtt/{friendly_name})
        - set: Command topic (zigbee2mqtt/{friendly_name}/set)
        - get: Query topic (zigbee2mqtt/{friendly_name}/get)
        - action: Button/remote action topic (zigbee2mqtt/{friendly_name}/action)
        - availability: Online/offline status topic

        EXAMPLES:
        - Get action topic: ha_z2m_get_mqtt_topic("sensor.button_action", "action")
        - Get set topic: ha_z2m_get_mqtt_topic("light.bedroom_light", "set")

        USE CASES:
        - "What MQTT topic should I use for button automation?"
        - "Get the set topic to control this light via MQTT"
        """
        try:
            # Get entity registry to find device
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)

            if not entity_result.get("success"):
                return {
                    "success": False,
                    "error": "Failed to access entity registry",
                }

            all_entities = entity_result.get("result", [])

            # Find entity
            target_entity = None
            for entity in all_entities:
                if entity.get("entity_id") == entity_id:
                    target_entity = entity
                    break

            if not target_entity:
                return {
                    "success": False,
                    "error": f"Entity not found: {entity_id}",
                    "suggestion": "Verify the entity ID is correct",
                }

            # Extract friendly name from entity ID
            friendly_name = _extract_friendly_name_from_entity_id(entity_id)

            # Build topic based on type
            valid_types = ["base", "set", "get", "action", "availability"]
            if topic_type not in valid_types:
                return {
                    "success": False,
                    "error": f"Invalid topic_type: {topic_type}",
                    "valid_types": valid_types,
                }

            suffix = "" if topic_type == "base" else topic_type
            mqtt_topic = _build_mqtt_topic(friendly_name, suffix)

            return {
                "success": True,
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "topic_type": topic_type,
                "mqtt_topic": mqtt_topic,
                "all_topics": {
                    "base": _build_mqtt_topic(friendly_name),
                    "set": _build_mqtt_topic(friendly_name, "set"),
                    "get": _build_mqtt_topic(friendly_name, "get"),
                    "action": _build_mqtt_topic(friendly_name, "action"),
                    "availability": _build_mqtt_topic(friendly_name, "availability"),
                },
            }

        except Exception as e:
            logger.error(f"Failed to get MQTT topic: {e}")
            return {
                "success": False,
                "error": f"Failed to get MQTT topic: {str(e)}",
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_z2m_search_entities(
        query: Annotated[
            str | None,
            Field(
                description="Search query to filter Z2M entities (fuzzy match on entity_id or name)",
                default=None,
            ),
        ] = None,
        domain: Annotated[
            str | None,
            Field(
                description="Filter by entity domain (e.g., 'sensor', 'light', 'binary_sensor')",
                default=None,
            ),
        ] = None,
        device_class: Annotated[
            str | None,
            Field(
                description="Filter by device class (e.g., 'motion', 'temperature', 'battery')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Search for Zigbee2MQTT entities with optional filtering.

        Finds entities that appear to be from Z2M based on:
        - Entity patterns (linkquality, action, common Z2M suffixes)
        - Z2M-specific attributes (linkquality, update_available)
        - Associated device being a Z2M device

        Filter options:
        - query: Fuzzy search on entity_id or friendly name
        - domain: Exact match on entity domain
        - device_class: Match on device_class attribute

        Returns entity information including:
        - entity_id and friendly name
        - Device class and unit of measurement
        - Current state
        - Z2M friendly name and MQTT topics

        EXAMPLES:
        - All Z2M entities: ha_z2m_search_entities()
        - Motion sensors: ha_z2m_search_entities(device_class="motion")
        - Sensors only: ha_z2m_search_entities(domain="sensor")
        - Search: ha_z2m_search_entities(query="bedroom")

        USE CASES:
        - "Find all Z2M motion sensors"
        - "List battery levels for Z2M devices"
        - "Which Z2M entities are in the bedroom?"
        """
        try:
            # Get all states
            states = await client.get_states()

            # Get entity registry for device mapping
            entity_message: dict[str, Any] = {"type": "config/entity_registry/list"}
            entity_result = await client.send_websocket_message(entity_message)
            all_registry_entities = (
                entity_result.get("result", [])
                if entity_result.get("success")
                else []
            )

            # Get device registry
            device_message: dict[str, Any] = {"type": "config/device_registry/list"}
            device_result = await client.send_websocket_message(device_message)
            all_devices = (
                device_result.get("result", [])
                if device_result.get("success")
                else []
            )

            # Build device lookup and Z2M device set
            z2m_device_ids = set()
            for device in all_devices:
                if _is_z2m_device(device):
                    z2m_device_ids.add(device.get("id"))

            # Build entity to device mapping
            entity_device_map = {}
            for entity in all_registry_entities:
                entity_device_map[entity.get("entity_id")] = entity.get("device_id")

            # Filter entities
            z2m_entities = []
            for state in states:
                entity_id = state.get("entity_id", "")
                attributes = state.get("attributes", {})

                # Check if entity is from a Z2M device
                device_id = entity_device_map.get(entity_id)
                is_from_z2m_device = device_id in z2m_device_ids

                # Check if entity has Z2M patterns
                is_z2m_pattern = _is_likely_z2m_entity(state)

                if not (is_from_z2m_device or is_z2m_pattern):
                    continue

                # Apply domain filter
                if domain:
                    entity_domain = entity_id.split(".")[0]
                    if entity_domain != domain:
                        continue

                # Apply device_class filter
                if device_class:
                    entity_device_class = attributes.get("device_class", "")
                    if entity_device_class != device_class:
                        continue

                # Apply query filter (fuzzy match)
                if query:
                    query_lower = query.lower()
                    entity_id_lower = entity_id.lower()
                    friendly_name = attributes.get("friendly_name", "").lower()

                    if (
                        query_lower not in entity_id_lower
                        and query_lower not in friendly_name
                    ):
                        continue

                # Extract Z2M friendly name
                z2m_friendly_name = _extract_friendly_name_from_entity_id(entity_id)

                z2m_entities.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": attributes.get("friendly_name"),
                        "state": state.get("state"),
                        "device_class": attributes.get("device_class"),
                        "unit_of_measurement": attributes.get("unit_of_measurement"),
                        "device_id": device_id,
                        "z2m_friendly_name": z2m_friendly_name,
                        "mqtt_topic": _build_mqtt_topic(z2m_friendly_name),
                    }
                )

            # Sort by entity_id
            z2m_entities.sort(key=lambda x: x["entity_id"])

            # Group by domain for summary
            domain_summary: dict[str, int] = {}
            for entity in z2m_entities:
                entity_domain = entity["entity_id"].split(".")[0]
                domain_summary[entity_domain] = domain_summary.get(entity_domain, 0) + 1

            filters_applied = []
            if query:
                filters_applied.append(f"query={query}")
            if domain:
                filters_applied.append(f"domain={domain}")
            if device_class:
                filters_applied.append(f"device_class={device_class}")

            return {
                "success": True,
                "count": len(z2m_entities),
                "entities": z2m_entities,
                "domain_summary": domain_summary,
                "filters": filters_applied if filters_applied else None,
                "message": f"Found {len(z2m_entities)} Zigbee2MQTT entity(ies)",
            }

        except Exception as e:
            logger.error(f"Failed to search Z2M entities: {e}")
            return {
                "success": False,
                "error": f"Failed to search Z2M entities: {str(e)}",
            }
