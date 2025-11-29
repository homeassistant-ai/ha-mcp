"""
Configuration management tools for Home Assistant automations.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant automation configurations.
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_automation_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant automation configuration tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_config_get_automation(
        identifier: Annotated[
            str,
            Field(
                description="Automation entity_id (e.g., 'automation.morning_routine') or unique_id"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Retrieve Home Assistant automation configuration.

        Returns the complete configuration including triggers, conditions, actions, and mode settings.

        EXAMPLES:
        - Get automation: ha_config_get_automation("automation.morning_routine")
        - Get by unique_id: ha_config_get_automation("my_unique_automation_id")

        For comprehensive automation documentation, use: ha_get_domain_docs("automation")
        """
        try:
            config_result = await client.get_automation_config(identifier)
            return {
                "success": True,
                "action": "get",
                "identifier": identifier,
                "config": config_result,
            }
        except Exception as e:
            # Handle 404 errors gracefully (often used to verify deletion)
            error_str = str(e)
            if (
                "404" in error_str
                or "not found" in error_str.lower()
                or "entity not found" in error_str.lower()
            ):
                logger.debug(
                    f"Automation {identifier} not found (expected for deletion verification)"
                )
                return {
                    "success": False,
                    "action": "get",
                    "identifier": identifier,
                    "error": f"Automation {identifier} does not exist",
                    "reason": "not_found",
                }

            logger.error(f"Error getting automation: {e}")
            return {
                "success": False,
                "action": "get",
                "identifier": identifier,
                "error": str(e),
                "suggestions": [
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Check Home Assistant connection",
                    "Use ha_get_domain_docs('automation') for configuration help",
                ],
            }

    @mcp.tool
    @log_tool_usage
    async def ha_config_set_automation(
        config: Annotated[
            str | dict[str, Any],
            Field(
                description="Complete automation configuration with required fields: 'alias', 'trigger', 'action'. Optional: 'description', 'condition', 'mode', 'max', 'initial_state', 'variables'"
            ),
        ],
        identifier: Annotated[
            str | None,
            Field(
                description="Automation entity_id or unique_id for updates. Omit to create new automation with generated unique_id.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant automation.

        Creates a new automation (if identifier omitted) or updates existing automation with provided configuration.

        REQUIRED CONFIG FIELDS:
        - alias: Human-readable automation name
        - trigger: List of trigger conditions (time, state, event, etc.)
        - action: List of actions to execute

        OPTIONAL CONFIG FIELDS:
        - description: Detailed description of the user's intent (RECOMMENDED: helps safely modify implementation later)
        - condition: Additional conditions that must be met
        - mode: 'single' (default), 'restart', 'queued', 'parallel'
        - max: Maximum concurrent executions (for queued/parallel modes)
        - initial_state: Whether automation starts enabled (true/false)
        - variables: Variables for use in automation

        BASIC EXAMPLES:

        Simple time-based automation:
        ha_config_set_automation({
            "alias": "Morning Lights",
            "description": "Turn on bedroom lights at 7 AM to help wake up",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "action": [{"service": "light.turn_on", "target": {"area_id": "bedroom"}}]
        })

        Motion-activated lighting with condition:
        ha_config_set_automation({
            "alias": "Motion Light",
            "trigger": [{"platform": "state", "entity_id": "binary_sensor.motion", "to": "on"}],
            "condition": [{"condition": "sun", "after": "sunset"}],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.hallway"}},
                {"delay": {"minutes": 5}},
                {"service": "light.turn_off", "target": {"entity_id": "light.hallway"}}
            ],
            "mode": "restart"
        })

        Update existing automation:
        ha_config_set_automation(
            identifier="automation.morning_routine",
            config={
                "alias": "Updated Morning Routine",
                "trigger": [{"platform": "time", "at": "06:30:00"}],
                "action": [
                    {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                    {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}}
                ]
            }
        )

        TRIGGER TYPES: time, time_pattern, sun, state, numeric_state, event, device, zone, template, and more
        CONDITION TYPES: state, numeric_state, time, sun, template, device, zone, and more
        ACTION TYPES: service calls, delays, wait_for_trigger, wait_template, if/then/else, choose, repeat, parallel

        For comprehensive automation documentation with all trigger/condition/action types and advanced examples:
        - Use: ha_get_domain_docs("automation")
        - Or visit: https://www.home-assistant.io/docs/automation/

        TROUBLESHOOTING:
        - Use ha_get_state() to verify entity_ids exist
        - Use ha_search_entities() to find correct entity_ids
        - Use ha_eval_template() to test Jinja2 templates before using in automations
        - Use ha_search_entities(domain_filter='automation') to find existing automations
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

            # Validate required fields
            required_fields = ["alias", "trigger", "action"]
            missing_fields = [f for f in required_fields if f not in config_dict]
            if missing_fields:
                return {
                    "success": False,
                    "error": f"Missing required fields: {', '.join(missing_fields)}",
                    "required_fields": required_fields,
                    "missing_fields": missing_fields,
                }

            result = await client.upsert_automation_config(
                config_dict, identifier
            )
            return {
                "success": True,
                **result,
                "config_provided": config_dict,
            }

        except Exception as e:
            logger.error(f"Error upserting automation: {e}")
            return {
                "success": False,
                "identifier": identifier,
                "error": str(e),
                "suggestions": [
                    "Check automation configuration format",
                    "Ensure required fields: alias, trigger, action",
                    "Use entity_id format: automation.morning_routine or unique_id",
                    "Use ha_search_entities(domain_filter='automation') to find automations",
                    "Use ha_get_domain_docs('automation') for comprehensive configuration help",
                ],
            }

    @mcp.tool(annotations={"destructiveHint": True})
    @log_tool_usage
    async def ha_config_remove_automation(
        identifier: Annotated[
            str,
            Field(
                description="Automation entity_id (e.g., 'automation.old_automation') or unique_id to delete"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant automation.

        EXAMPLES:
        - Delete automation: ha_config_remove_automation("automation.old_automation")
        - Delete by unique_id: ha_config_remove_automation("my_unique_id")

        **WARNING:** Deleting an automation removes it permanently from your Home Assistant configuration.
        """
        try:
            result = await client.delete_automation_config(identifier)
            return {"success": True, "action": "delete", **result}
        except Exception as e:
            logger.error(f"Error deleting automation: {e}")
            return {
                "success": False,
                "action": "delete",
                "identifier": identifier,
                "error": str(e),
                "suggestions": [
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Use entity_id format: automation.morning_routine or unique_id",
                    "Check Home Assistant connection",
                ],
            }
