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


def _strip_empty_script_fields(config: dict[str, Any]) -> dict[str, Any]:
    """
    Strip empty sequence array from script config.

    Blueprint-based scripts should not have a sequence field since this comes
    from the blueprint itself. If an empty array is present, it overrides the
    blueprint's configuration and breaks the script.

    Args:
        config: Script configuration dict

    Returns:
        Configuration with empty sequence array removed
    """
    cleaned = config.copy()

    # Remove empty sequence array for blueprint scripts
    if "sequence" in cleaned and cleaned["sequence"] == []:
        del cleaned["sequence"]

    return cleaned


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["script"],
            "title": "Get Script Config",
        }
    )
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

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["script"],
            "title": "Create or Update Script",
        }
    )
    @log_tool_usage
    async def ha_config_set_script(
        script_id: Annotated[
            str, Field(description="Script identifier (e.g., 'morning_routine')")
        ],
        config: Annotated[
            str | dict[str, Any],
            Field(
                description="Script configuration dictionary. Must include EITHER 'sequence' (for regular scripts) OR 'use_blueprint' (for blueprint-based scripts). Optional fields: 'alias', 'description', 'icon', 'mode', 'max', 'fields'"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant script.

        Creates a new script or updates an existing one with the provided configuration.
        Supports both regular scripts (with sequence) and blueprint-based scripts.

        Required config fields (choose one):
            - sequence: List of actions to execute (for regular scripts)
            - use_blueprint: Blueprint configuration (for blueprint-based scripts)

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

        Create blueprint-based script:
        ha_config_set_script("notification_script", {
            "alias": "My Notification Script",
            "use_blueprint": {
                "path": "notification_script.yaml",
                "input": {
                    "message": "Hello World",
                    "title": "Test Notification"
                }
            }
        })

        Update blueprint script inputs:
        ha_config_set_script("notification_script", {
            "alias": "My Notification Script",
            "use_blueprint": {
                "path": "notification_script.yaml",
                "input": {
                    "message": "Updated message",
                    "title": "Updated Title"
                }
            }
        })

        PREFER NATIVE ACTIONS OVER TEMPLATES:
        Before using template-based logic in scripts, check if native actions exist:
        - Use `choose` action instead of template-based service names
        - Use `if/then/else` action instead of template conditions
        - Use `repeat` action with `for_each` instead of template loops
        - Use `wait_for_trigger` instead of `wait_template` when waiting for state changes
        - Use native action variables instead of complex template calculations

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

            # Validate required fields based on script type
            # Blueprint scripts only need use_blueprint, regular scripts need sequence
            if "use_blueprint" in config_dict:
                # Strip empty sequence array that would override blueprint
                config_dict = _strip_empty_script_fields(config_dict)
            elif "sequence" not in config_dict:
                return {
                    "success": False,
                    "error": "config must include either 'sequence' field (for regular scripts) or 'use_blueprint' field (for blueprint-based scripts)",
                    "required_fields": ["sequence OR use_blueprint"],
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
                    "Ensure config includes either 'sequence' field (regular scripts) or 'use_blueprint' field (blueprint-based scripts)",
                    "For blueprint scripts, use ha_get_blueprint(domain='script') to list available blueprints",
                    "Validate sequence actions syntax for regular scripts",
                    "Check entity_ids exist if using service calls",
                    "Use ha_search_entities(domain_filter='script') to find scripts",
                    "Use ha_get_domain_docs('script') for configuration help",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["script"],
            "title": "Remove Script",
        }
    )
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

        **IMPORTANT LIMITATION:**
        This tool can only delete scripts created via the Home Assistant UI.
        Scripts defined in YAML configuration files (scripts.yaml or configuration.yaml)
        cannot be deleted through the API and will return a 405 Method Not Allowed error.

        To remove YAML-defined scripts, you must edit the configuration file directly.

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
