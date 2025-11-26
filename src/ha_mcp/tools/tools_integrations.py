"""
Integration management tools for Home Assistant MCP server.

This module provides tools to list and query Home Assistant integrations
(config entries) via the REST API.
"""

import logging
from typing import Any

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_integration_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register integration management tools with the MCP server."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_integrations(
        domain: str | None = None,
        type_filter: str | None = None,
    ) -> dict[str, Any]:
        """
        List installed/configured Home Assistant integrations (config entries).

        This tool returns information about all configured integrations in your
        Home Assistant instance, including their status, configuration source,
        and capabilities.

        **Parameters:**
        - domain: Optional filter by integration domain (e.g., 'mqtt', 'zwave_js', 'hue')
        - type_filter: Optional filter by integration type. Valid values:
          - 'hub' - Integration hubs (Hue, Z-Wave, Zigbee)
          - 'device' - Device integrations
          - 'service' - Service integrations (cloud services, APIs)
          - 'helper' - Helper integrations
          - 'entity' - Entity integrations
          - 'hardware' - Hardware integrations
          - 'system' - System integrations

        **Response includes for each integration:**
        - entry_id: Unique identifier for the config entry
        - domain: Integration domain (e.g., 'mqtt', 'hue')
        - title: User-friendly name/title
        - state: Current state (loaded, setup_error, not_loaded, etc.)
        - source: How it was added (user, discovery, import, etc.)
        - supports_options: Whether the integration has a configuration UI
        - supports_unload: Whether the integration can be unloaded
        - disabled_by: Why it's disabled (if applicable)

        **Common Use Cases:**
        - Check which integrations are installed and their status
        - Troubleshoot integration errors (state: setup_error)
        - Verify integration configuration after setup
        - Monitor integration health across your system

        **Examples:**

        List all integrations:
        ```python
        ha_list_integrations()
        ```

        List only MQTT integrations:
        ```python
        ha_list_integrations(domain="mqtt")
        ```

        List all hub-type integrations:
        ```python
        ha_list_integrations(type_filter="hub")
        ```

        **States explained:**
        - 'loaded': Integration is running normally
        - 'setup_error': Integration failed to set up
        - 'setup_retry': Integration is retrying setup
        - 'not_loaded': Integration is configured but not loaded
        - 'failed_unload': Integration failed to unload
        - 'migration_error': Configuration migration failed
        """
        try:
            # Use REST API endpoint for config entries
            response = await client._request(
                "GET", "/config/config_entries/entry"
            )

            if not isinstance(response, list):
                return {
                    "success": False,
                    "error": "Unexpected response format from Home Assistant",
                    "response_type": type(response).__name__,
                }

            entries = response

            # Filter by domain if specified
            if domain:
                entries = [e for e in entries if e.get("domain") == domain]

            # Filter by type if specified
            # Note: Home Assistant doesn't expose type directly in config entries,
            # but we can filter by common domains associated with types
            if type_filter:
                type_domains = _get_domains_for_type(type_filter)
                if type_domains:
                    entries = [e for e in entries if e.get("domain") in type_domains]

            # Format entries for response
            formatted_entries = []
            for entry in entries:
                formatted_entry = {
                    "entry_id": entry.get("entry_id"),
                    "domain": entry.get("domain"),
                    "title": entry.get("title"),
                    "state": entry.get("state"),
                    "source": entry.get("source"),
                    "supports_options": entry.get("supports_options", False),
                    "supports_unload": entry.get("supports_unload", False),
                    "disabled_by": entry.get("disabled_by"),
                }

                # Include pref_disable_new_entities and pref_disable_polling if present
                if "pref_disable_new_entities" in entry:
                    formatted_entry["pref_disable_new_entities"] = entry[
                        "pref_disable_new_entities"
                    ]
                if "pref_disable_polling" in entry:
                    formatted_entry["pref_disable_polling"] = entry[
                        "pref_disable_polling"
                    ]

                formatted_entries.append(formatted_entry)

            # Group by state for summary
            state_summary: dict[str, int] = {}
            for entry in formatted_entries:
                state = entry.get("state", "unknown")
                state_summary[state] = state_summary.get(state, 0) + 1

            return {
                "success": True,
                "total": len(formatted_entries),
                "entries": formatted_entries,
                "state_summary": state_summary,
                "filters_applied": {
                    "domain": domain,
                    "type_filter": type_filter,
                },
            }

        except Exception as e:
            logger.error(f"Failed to list integrations: {e}")
            return {
                "success": False,
                "error": f"Failed to list integrations: {str(e)}",
                "suggestions": [
                    "Verify Home Assistant connection is working",
                    "Check that the API is accessible",
                    "Ensure your token has sufficient permissions",
                ],
            }


def _get_domains_for_type(type_filter: str) -> list[str] | None:
    """
    Get list of domains associated with integration type.

    This is a best-effort mapping since Home Assistant doesn't expose
    integration types directly in the config entries API.
    """
    type_mappings = {
        "hub": [
            "hue",
            "zwave_js",
            "zha",
            "zigbee2mqtt",
            "homekit_controller",
            "matter",
            "thread",
            "insteon",
            "lutron",
            "lutron_caseta",
            "deconz",
            "homematic",
            "knx",
            "unifi",
        ],
        "device": [
            "esphome",
            "shelly",
            "tasmota",
            "sonoff",
            "tuya",
            "xiaomi_miio",
            "yeelight",
            "tplink",
            "wemo",
            "lifx",
            "nanoleaf",
            "ring",
            "nest",
            "ecobee",
        ],
        "service": [
            "google_assistant",
            "alexa",
            "ifttt",
            "pushover",
            "telegram_bot",
            "slack",
            "discord",
            "spotify",
            "plex",
            "openweathermap",
            "met",
            "accuweather",
        ],
        "helper": [
            "input_boolean",
            "input_number",
            "input_text",
            "input_select",
            "input_datetime",
            "input_button",
            "counter",
            "timer",
            "group",
            "template",
        ],
        "entity": [
            "mqtt",
            "rest",
            "template",
            "command_line",
            "file",
            "generic",
            "local_file",
            "trend",
            "derivative",
            "min_max",
            "statistics",
        ],
        "hardware": [
            "bluetooth",
            "usb",
            "serial",
            "gpio",
            "rpi_power",
            "hardware",
        ],
        "system": [
            "homeassistant",
            "default_config",
            "frontend",
            "logger",
            "recorder",
            "history",
            "logbook",
            "system_log",
            "persistent_notification",
            "automation",
            "script",
            "scene",
            "person",
            "zone",
        ],
    }

    return type_mappings.get(type_filter.lower())
