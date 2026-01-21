"""
Configuration information tool for Home Assistant MCP Server.

This module provides an informational tool that explains how Home Assistant
configuration works when accessed remotely via ha-mcp, and provides guidance
on generating configuration snippets.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_config_info_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register configuration information tools with the MCP server."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["information", "configuration"],
            "title": "Home Assistant Configuration Info",
        }
    )
    @log_tool_usage
    async def ha_config_info(
        config_type: Annotated[
            str,
            Field(
                default="general",
                description=(
                    "Type of configuration information to retrieve. "
                    "Options: 'general' (default), 'automation', 'script', 'dashboard', "
                    "'integration', or 'yaml'. Use 'general' for an overview of how "
                    "configuration works via ha-mcp."
                ),
            ),
        ] = "general",
    ) -> dict[str, Any]:
        """
        Get information about Home Assistant configuration access via ha-mcp.

        **IMPORTANT FOR AI AGENTS:**
        Home Assistant accessed via ha-mcp is a REMOTE system accessed through
        REST and WebSocket APIs. Configuration files are NOT accessible on the
        local filesystem where this MCP server runs.

        **DO NOT:**
        - Search for configuration.yaml or other HA config files on disk
        - Attempt to read HA configuration files from the filesystem
        - Try to modify configuration files directly
        - Assume HA is running on the same machine as this MCP server

        **DO:**
        - Use ha-mcp tools to query configuration via the API
        - Generate configuration snippets for the user to copy/paste
        - Direct users to the Home Assistant UI for configuration changes
        - Suggest configuration examples based on user needs

        **CONFIGURATION ACCESS:**
        - Automations: Use ha_config_* tools (list, get, create, update, delete)
        - Scripts: Use ha_config_script_* tools
        - Dashboards: Use ha_config_dashboard_* tools
        - Integrations: Most are configured via UI, some via YAML
        - YAML files: Not directly accessible via ha-mcp

        **GENERATING SNIPPETS:**
        When users need configuration examples:
        1. Generate valid YAML/JSON snippets based on their requirements
        2. Provide clear instructions on where to add the configuration
        3. Explain any required steps (restart, reload, etc.)
        4. Link to relevant Home Assistant documentation

        **FEATURE REQUESTS:**
        If users need direct configuration file access or advanced features:
        - Suggest filing a feature request at:
          https://github.com/homeassistant-ai/ha-mcp/issues/new
        - Explain the current capabilities and limitations
        - Provide workarounds using existing tools

        Returns information specific to the requested config_type.
        """
        info: dict[str, Any] = {
            "success": True,
            "config_type": config_type,
            "ha_access_method": "Remote via REST/WebSocket APIs",
            "filesystem_access": False,
        }

        if config_type == "general":
            info["message"] = (
                "Home Assistant is accessed remotely via REST and WebSocket APIs. "
                "Configuration files (configuration.yaml, etc.) are NOT accessible "
                "on the local filesystem. Use ha-mcp tools to query and modify "
                "configuration via the API, or generate configuration snippets "
                "for users to manually add."
            )
            info["available_tools"] = [
                "ha_config_* - Automation management",
                "ha_config_script_* - Script management",
                "ha_config_dashboard_* - Dashboard management",
                "ha_config_helper_* - Helper entity management",
                "ha_get_config - Get basic HA configuration info",
                "ha_list_entities - List all entities",
                "ha_search_* - Search entities, services, etc.",
            ]
            info["not_available"] = [
                "Direct file system access to configuration.yaml",
                "Direct file system access to automations.yaml",
                "Direct file system access to scripts.yaml",
                "Direct file system access to secrets.yaml",
            ]

        elif config_type == "automation":
            info["message"] = (
                "Automations can be managed via ha_config_* tools. "
                "For YAML-based automations, generate snippets for users to add manually."
            )
            info["tools"] = [
                "ha_config_list_automations - List all automations",
                "ha_config_get_automation - Get automation details",
                "ha_config_create_automation - Create new automation",
                "ha_config_update_automation - Update existing automation",
                "ha_config_delete_automation - Delete automation",
                "ha_trigger_automation - Manually trigger automation",
            ]
            info["snippet_example"] = {
                "description": "Example automation snippet",
                "yaml": """# Add to automations.yaml or via HA UI
- alias: "Example Automation"
  description: "Turn on light when motion detected"
  trigger:
    - platform: state
      entity_id: binary_sensor.motion_sensor
      to: "on"
  condition: []
  action:
    - service: light.turn_on
      target:
        entity_id: light.living_room
      data:
        brightness: 255""",
            }

        elif config_type == "script":
            info["message"] = (
                "Scripts can be managed via ha_config_script_* tools. "
                "For YAML-based scripts, generate snippets for users to add manually."
            )
            info["tools"] = [
                "ha_config_list_scripts - List all scripts",
                "ha_config_get_script - Get script details",
                "ha_config_create_script - Create new script",
                "ha_config_update_script - Update existing script",
                "ha_config_delete_script - Delete script",
                "ha_execute_script - Run a script",
            ]
            info["snippet_example"] = {
                "description": "Example script snippet",
                "yaml": """# Add to scripts.yaml or via HA UI
good_night:
  alias: "Good Night"
  description: "Turn off all lights and lock doors"
  sequence:
    - service: light.turn_off
      target:
        area_id: all
    - service: lock.lock
      target:
        entity_id: lock.front_door""",
            }

        elif config_type == "dashboard":
            info["message"] = (
                "Dashboards can be managed via ha_config_dashboard_* tools. "
                "Dashboard configuration is stored in the HA database, not YAML files."
            )
            info["tools"] = [
                "ha_config_get_dashboard(list_only=True) - List all dashboards",
                "ha_config_get_dashboard(url_path=...) - Get dashboard configuration",
                "ha_config_set_dashboard - Create/update dashboard",
                "ha_config_delete_dashboard - Delete dashboard",
            ]
            info["note"] = (
                "Dashboard YAML configuration is for Lovelace cards, not file access. "
                "Use the dashboard tools to manage dashboards programmatically."
            )

        elif config_type == "integration":
            info["message"] = (
                "Most integrations are configured via the Home Assistant UI. "
                "Some integrations support YAML configuration, but files are not "
                "accessible via ha-mcp."
            )
            info["recommendations"] = [
                "Use ha_get_integration() to see installed integrations",
                "Generate YAML snippets for manual addition to configuration.yaml",
                "Direct users to Settings > Devices & Services in HA UI",
                "Provide documentation links for specific integrations",
            ]
            info["snippet_example"] = {
                "description": "Example integration configuration",
                "yaml": """# Add to configuration.yaml
mqtt:
  broker: 192.168.1.100
  port: 1883
  username: !secret mqtt_username
  password: !secret mqtt_password

sensor:
  - platform: mqtt
    name: "Temperature"
    state_topic: "home/temperature"
    unit_of_measurement: "°C\"""",
            }

        elif config_type == "yaml":
            info["message"] = (
                "YAML configuration files (configuration.yaml, automations.yaml, etc.) "
                "are NOT accessible via ha-mcp. Generate snippets for users to add manually."
            )
            info["workflow"] = [
                "1. Ask user what configuration they need",
                "2. Generate valid YAML snippet based on requirements",
                "3. Explain which file to edit (configuration.yaml, automations.yaml, etc.)",
                "4. Provide instructions on reloading/restarting HA",
                "5. Link to relevant documentation",
            ]
            info["common_files"] = {
                "configuration.yaml": "Main configuration file",
                "automations.yaml": "Automation definitions (or use UI)",
                "scripts.yaml": "Script definitions (or use UI)",
                "secrets.yaml": "Sensitive data (passwords, API keys, etc.)",
                "customize.yaml": "Entity customization",
                "groups.yaml": "Entity groups",
                "scenes.yaml": "Scene definitions",
            }
            info["feature_request_url"] = (
                "https://github.com/homeassistant-ai/ha-mcp/issues/new"
            )
            info["feature_request_note"] = (
                "If direct file access is needed, file a feature request. "
                "Explain your use case and why current tools are insufficient."
            )

        else:
            info["success"] = False
            info["error"] = (
                f"Unknown config_type: {config_type}. "
                "Valid options: general, automation, script, dashboard, integration, yaml"
            )

        return info
