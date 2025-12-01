"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

from typing import Any, cast


from .util_helpers import parse_json_param


def register_service_tools(mcp, client, **kwargs):
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")

    @mcp.tool(annotations={"idempotentHint": True, "title": "Call Service"})
    async def ha_call_service(
        domain: str,
        service: str,
        entity_id: str | None = None,
        data: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Execute Home Assistant services with comprehensive validation and examples.

        This is the universal tool for controlling all Home Assistant entities and executing automations.

        **Common Usage Examples:**

        **Light Control:**
        ```python
        # Turn on light
        ha_call_service("light", "turn_on", entity_id="light.living_room")

        # Turn on with brightness and color
        ha_call_service("light", "turn_on", entity_id="light.bedroom",
                      data={"brightness_pct": 75, "color_temp_kelvin": 2700})

        # Turn off all lights
        ha_call_service("light", "turn_off")
        ```

        **Climate Control:**
        ```python
        # Set temperature
        ha_call_service("climate", "set_temperature",
                      entity_id="climate.thermostat", data={"temperature": 22})

        # Change mode
        ha_call_service("climate", "set_hvac_mode",
                      entity_id="climate.living_room", data={"hvac_mode": "heat"})
        ```

        **Automation Control:**
        ```python
        # Trigger automation (replaces ha_trigger_automation)
        ha_call_service("automation", "trigger", entity_id="automation.morning_routine")

        # Turn automation on/off
        ha_call_service("automation", "turn_off", entity_id="automation.night_mode")
        ha_call_service("automation", "turn_on", entity_id="automation.security_check")
        ```

        **Scene Activation:**
        ```python
        # Activate scene
        ha_call_service("scene", "turn_on", entity_id="scene.movie_night")
        ha_call_service("scene", "turn_on", entity_id="scene.bedtime")
        ```

        **Input Helpers:**
        ```python
        # Set input number
        ha_call_service("input_number", "set_value",
                      entity_id="input_number.temp_offset", data={"value": 2.5})

        # Toggle input boolean
        ha_call_service("input_boolean", "toggle", entity_id="input_boolean.guest_mode")

        # Set input text
        ha_call_service("input_text", "set_value",
                      entity_id="input_text.status", data={"value": "Away"})
        ```

        **Universal Controls (works with any entity):**
        ```python
        # Universal toggle
        ha_call_service("homeassistant", "toggle", entity_id="switch.porch_light")

        # Universal turn on/off
        ha_call_service("homeassistant", "turn_on", entity_id="media_player.spotify")
        ha_call_service("homeassistant", "turn_off", entity_id="fan.ceiling_fan")
        ```

        **Script Execution:**
        ```python
        # Run script
        ha_call_service("script", "turn_on", entity_id="script.bedtime_routine")
        ha_call_service("script", "good_night_sequence")
        ```

        **Media Player Control:**
        ```python
        # Volume control
        ha_call_service("media_player", "volume_set",
                      entity_id="media_player.living_room", data={"volume_level": 0.5})

        # Play media
        ha_call_service("media_player", "play_media",
                      entity_id="media_player.spotify",
                      data={"media_content_type": "music", "media_content_id": "spotify:playlist:123"})
        ```

        **Cover Control:**
        ```python
        # Open/close covers
        ha_call_service("cover", "open_cover", entity_id="cover.garage_door")
        ha_call_service("cover", "close_cover", entity_id="cover.living_room_blinds")

        # Set position
        ha_call_service("cover", "set_cover_position",
                      entity_id="cover.bedroom_curtains", data={"position": 50})
        ```

        **Parameter Guidelines:**
        - **entity_id**: Optional for services that affect all entities of a domain
        - **data**: Service-specific parameters (brightness, temperature, volume, etc.)
        - Use ha_get_state() first to check current values and supported features
        - Use ha_get_domain_docs() for detailed service documentation
        """
        try:
            # Parse JSON data if provided as string
            try:
                parsed_data = parse_json_param(data, "data")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid data parameter: {e}",
                    "provided_data_type": type(data).__name__,
                }

            # Ensure service_data is a dict
            service_data: dict[str, Any] = {}
            if parsed_data is not None:
                if isinstance(parsed_data, dict):
                    service_data = parsed_data
                else:
                    return {
                        "success": False,
                        "error": "Data parameter must be a JSON object",
                        "provided_type": type(parsed_data).__name__,
                    }

            if entity_id:
                service_data["entity_id"] = entity_id
            result = await client.call_service(domain, service, service_data)

            return {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "parameters": data,
                "result": result,
                "message": f"Successfully executed {domain}.{service}",
            }
        except Exception as error:
            return {
                "success": False,
                "error": str(error),
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "suggestions": [
                    f"Verify {entity_id} exists using ha_get_state()",
                    f"Check available services for {domain} domain using ha_get_domain_docs()",
                    f"For automation: ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                    "Use ha_search_entities() to find correct entity IDs",
                ],
                "examples": {
                    "automation_trigger": f"ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    "universal_toggle": f"ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                    "light_control": "ha_call_service('light', 'turn_on', entity_id='light.bedroom', data={'brightness_pct': 75})",
                },
            }

    @mcp.tool(annotations={"readOnlyHint": True})
    async def ha_get_operation_status(
        operation_id: str, timeout_seconds: int = 10
    ) -> dict[str, Any]:
        """Check status of device operation with real-time WebSocket verification."""
        result = await device_tools.get_device_operation_status(
            operation_id=operation_id, timeout_seconds=timeout_seconds
        )
        return cast(dict[str, Any], result)

    @mcp.tool(annotations={"idempotentHint": True, "title": "Bulk Control"})
    async def ha_bulk_control(
        operations: str | list[dict[str, Any]], parallel: bool = True
    ) -> dict[str, Any]:
        """Control multiple devices with bulk operation support and WebSocket tracking."""
        # Parse JSON operations if provided as string
        try:
            parsed_operations = parse_json_param(operations, "operations")
        except ValueError as e:
            return {
                "success": False,
                "error": f"Invalid operations parameter: {e}",
                "provided_operations_type": type(operations).__name__,
            }

        # Ensure operations is a list of dicts
        if parsed_operations is None or not isinstance(parsed_operations, list):
            return {
                "success": False,
                "error": "Operations parameter must be a list",
                "provided_type": type(parsed_operations).__name__,
            }

        operations_list = cast(list[dict[str, Any]], parsed_operations)
        result = await device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel
        )
        return cast(dict[str, Any], result)

    @mcp.tool(annotations={"readOnlyHint": True})
    async def ha_get_bulk_status(operation_ids: list[str]) -> dict[str, Any]:
        """Check status of multiple WebSocket-monitored operations."""
        result = await device_tools.get_bulk_operation_status(
            operation_ids=operation_ids
        )
        return cast(dict[str, Any], result)
