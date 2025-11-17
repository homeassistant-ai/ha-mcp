"""
Configuration management tools for Home Assistant scripts.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant script configurations.
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_config_get_script(
        script_id: Annotated[
            str, Field(description="Script identifier (e.g., 'morning_routine')")
        ],
    ) -> dict[str, Any]:
        """
        Retrieve Home Assistant script configuration.

        Returns the complete configuration for a script, including sequence, mode, fields, and other settings.

        EXAMPLES:
        - Get script: ha_config_get_script("morning_routine")
        - Get script: ha_config_get_script("backup_script")

        For detailed script configuration help, use: ha_get_domain_docs("script")
        """
        try:
            config_result = await client.get_script_config(script_id)
            return {
                "success": True,
                "action": "get",
                "script_id": script_id,
                "config": config_result,
            }
        except Exception as e:
            logger.error(f"Error getting script: {e}")
            return {
                "success": False,
                "action": "get",
                "script_id": script_id,
                "error": str(e),
                "suggestions": [
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check Home Assistant connection",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_set_script(
        script_id: Annotated[
            str, Field(description="Script identifier (e.g., 'morning_routine')")
        ],
        config: Annotated[
            str | dict[str, Any],
            Field(
                description="Script configuration dictionary with 'sequence' (required) and optional fields like 'alias', 'description', 'icon', 'mode', 'max', 'fields'"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant script.

        Creates a new script or updates an existing one with the provided configuration.

        Required config fields:
            - sequence: List of actions to execute

        Optional config fields:
            - alias: Display name (defaults to script_id)
            - description: Script description
            - icon: Icon to display
            - mode: Execution mode ('single', 'restart', 'queued', 'parallel')
            - max: Maximum concurrent executions (for queued/parallel modes)
            - fields: Input parameters for the script

        IMPORTANT: The 'config' parameter must be passed as a proper dictionary/object.

        EXAMPLES:

        Create basic delay script:
        ha_config_set_script("wait_script", {
            "sequence": [{"delay": {"seconds": 5}}],
            "alias": "Wait 5 Seconds",
            "description": "Simple delay script"
        })

        Create service call script:
        ha_config_set_script("blink_light", {
            "sequence": [
                {"service": "light.turn_on", "target": {"entity_id": "light.living_room"}},
                {"delay": {"seconds": 2}},
                {"service": "light.turn_off", "target": {"entity_id": "light.living_room"}}
            ],
            "alias": "Light Blink",
            "mode": "single"
        })

        Create script with parameters:
        ha_config_set_script("backup_script", {
            "alias": "Backup with Reference",
            "description": "Create backup with optional reference parameter",
            "fields": {
                "reference": {
                    "name": "Reference",
                    "description": "Optional reference for backup identification",
                    "selector": {"text": None}
                }
            },
            "sequence": [
                {
                    "action": "hassio.backup_partial",
                    "data": {
                        "compressed": False,
                        "homeassistant": True,
                        "homeassistant_exclude_database": True,
                        "name": "Backup_{{ reference | default('auto') }}_{{ now().strftime('%Y%m%d_%H%M%S') }}"
                    }
                }
            ]
        })

        Update script:
        ha_config_set_script("morning_routine", {
            "sequence": [
                {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}}
            ],
            "alias": "Updated Morning Routine"
        })

        For detailed script configuration help, use: ha_get_domain_docs("script")

        Note: Scripts use Home Assistant's action syntax. Check the documentation for advanced
        features like conditions, variables, parallel execution, and service call options.
        """
        try:
            # Parse JSON config if provided as string
            try:
                parsed_config = parse_json_param(config, "config")
            except ValueError as e:
                return {
                    "success": False,
                    "error": f"Invalid config parameter: {e}",
                    "provided_config_type": type(config).__name__,
                }

            # Ensure config is a dict
            if parsed_config is None or not isinstance(parsed_config, dict):
                return {
                    "success": False,
                    "error": "Config parameter must be a JSON object",
                    "provided_type": type(parsed_config).__name__,
                }

            config_dict = cast(dict[str, Any], parsed_config)

            if "sequence" not in config_dict:
                return {
                    "success": False,
                    "error": "config must include 'sequence' field",
                    "required_fields": ["sequence"],
                }

            result = await client.upsert_script_config(config_dict, script_id)
            return {
                "success": True,
                **result,
                "config_provided": config_dict,
            }

        except Exception as e:
            logger.error(f"Error upserting script: {e}")
            return {
                "success": False,
                "script_id": script_id,
                "error": str(e),
                "suggestions": [
                    "Ensure config includes 'sequence' field",
                    "Validate sequence actions syntax",
                    "Check entity_ids exist if using service calls",
                    "Use ha_search_entities(domain_filter='script') to find scripts",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_remove_script(
        script_id: Annotated[
            str, Field(description="Script identifier to delete (e.g., 'old_script')")
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant script.

        EXAMPLES:
        - Delete script: ha_config_remove_script("old_script")
        - Delete script: ha_config_remove_script("temporary_script")

        **WARNING:** Deleting a script that is used by automations may cause those automations to fail.
        """
        try:
            result = await client.delete_script_config(script_id)
            return {"success": True, "action": "delete", **result}
        except Exception as e:
            logger.error(f"Error deleting script: {e}")
            return {
                "success": False,
                "action": "delete",
                "script_id": script_id,
                "error": str(e),
                "suggestions": [
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check if script is being used by automations",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }
