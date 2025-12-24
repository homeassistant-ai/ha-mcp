"""
Configuration management tools for Home Assistant automations.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant automation configurations.
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from ..errors import (
    create_config_error,
    create_resource_not_found_error,
    create_validation_error,
)
from .helpers import exception_to_structured_error, log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def _normalize_automation_config(config: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize automation config field names to HA API format.

    Home Assistant accepts both singular ('trigger', 'action', 'condition')
    and plural ('triggers', 'actions', 'conditions') field names in YAML,
    but the API expects singular forms. This function normalizes plural
    to singular for consistency.

    Args:
        config: Automation configuration dict

    Returns:
        Normalized configuration with singular field names
    """
    normalized = config.copy()

    # Map plural field names to singular (HA API format)
    field_mappings = {
        "triggers": "trigger",
        "actions": "action",
        "conditions": "condition",
    }

    for plural, singular in field_mappings.items():
        if plural in normalized and singular not in normalized:
            normalized[singular] = normalized.pop(plural)
        elif plural in normalized and singular in normalized:
            # Both exist - prefer singular, remove plural
            del normalized[plural]

    return normalized


def _normalize_trigger_keys(triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize trigger objects for round-trip compatibility.

    Home Assistant GET API returns triggers with 'trigger' key for the platform type,
    but the SET API expects 'platform' key. This function converts between formats.

    Args:
        triggers: List of trigger configuration dicts

    Returns:
        List of triggers with 'platform' key instead of 'trigger' key
    """
    normalized_triggers = []
    for trigger in triggers:
        normalized_trigger = trigger.copy()
        # Convert 'trigger' key to 'platform' if present and 'platform' is not
        if "trigger" in normalized_trigger and "platform" not in normalized_trigger:
            normalized_trigger["platform"] = normalized_trigger.pop("trigger")
        normalized_triggers.append(normalized_trigger)
    return normalized_triggers


def _normalize_config_for_roundtrip(config: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize automation config from GET response for direct use in SET.

    This ensures a config retrieved via ha_config_get_automation can be
    directly passed to ha_config_set_automation without modification.

    Transformations:
    1. Field names: triggers -> trigger, actions -> action, conditions -> condition
    2. Trigger keys: trigger -> platform (inside each trigger object)

    Args:
        config: Raw automation configuration from HA API

    Returns:
        Normalized configuration compatible with SET API
    """
    # First normalize field names (plural -> singular)
    normalized = _normalize_automation_config(config)

    # Then normalize trigger keys (trigger -> platform)
    if "trigger" in normalized and isinstance(normalized["trigger"], list):
        normalized["trigger"] = _normalize_trigger_keys(normalized["trigger"])

    return normalized


def _strip_empty_automation_fields(config: dict[str, Any]) -> dict[str, Any]:
    """
    Strip empty trigger/action/condition arrays from automation config.

    Blueprint-based automations should not have trigger/action/condition fields
    since these come from the blueprint itself. If empty arrays are present,
    they override the blueprint's configuration and break the automation.

    Args:
        config: Automation configuration dict

    Returns:
        Configuration with empty trigger/action/condition arrays removed
    """
    cleaned = config.copy()

    # Remove empty arrays for blueprint automations
    for field in ["trigger", "action", "condition"]:
        if field in cleaned and cleaned[field] == []:
            del cleaned[field]

    return cleaned


def register_config_automation_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant automation configuration tools."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["automation"], "title": "Get Automation Config"})
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
            # Normalize config for round-trip compatibility (GET → SET)
            normalized_config = _normalize_config_for_roundtrip(config_result)
            return {
                "success": True,
                "action": "get",
                "identifier": identifier,
                "config": normalized_config,
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
                error_response = create_resource_not_found_error(
                    "Automation",
                    identifier,
                    details=f"Automation '{identifier}' does not exist in Home Assistant",
                )
                error_response["action"] = "get"
                error_response["reason"] = "not_found"
                return error_response

            logger.error(f"Error getting automation: {e}")
            error_response = exception_to_structured_error(
                e,
                context={"identifier": identifier, "action": "get"},
            )
            # Add automation-specific suggestions
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Check Home Assistant connection",
                    "Use ha_get_domain_docs('automation') for configuration help",
                ]
            return error_response

    @mcp.tool(annotations={"destructiveHint": True, "tags": ["automation"], "title": "Create or Update Automation"})
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

        AUTOMATION TYPES:

        1. Regular Automations - Define triggers and actions directly
        2. Blueprint Automations - Use pre-built templates with customizable inputs

        REQUIRED FIELDS (Regular Automations):
        - alias: Human-readable automation name
        - trigger: List of trigger conditions (time, state, event, etc.)
        - action: List of actions to execute

        REQUIRED FIELDS (Blueprint Automations):
        - alias: Human-readable automation name
        - use_blueprint: Blueprint configuration
          - path: Blueprint file path (e.g., "motion_light.yaml")
          - input: Dictionary of input values for the blueprint

        OPTIONAL CONFIG FIELDS (Regular Automations):
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

        BLUEPRINT AUTOMATION EXAMPLES:

        Create automation from blueprint:
        ha_config_set_automation({
            "alias": "Motion Light Kitchen",
            "use_blueprint": {
                "path": "homeassistant/motion_light.yaml",
                "input": {
                    "motion_entity": "binary_sensor.kitchen_motion",
                    "light_target": {"entity_id": "light.kitchen"},
                    "no_motion_wait": 120
                }
            }
        })

        Update blueprint automation inputs:
        ha_config_set_automation(
            identifier="automation.motion_light_kitchen",
            config={
                "alias": "Motion Light Kitchen",
                "use_blueprint": {
                    "path": "homeassistant/motion_light.yaml",
                    "input": {
                        "motion_entity": "binary_sensor.kitchen_motion",
                        "light_target": {"entity_id": "light.kitchen"},
                        "no_motion_wait": 300
                    }
                }
            }
        })

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
                return create_validation_error(
                    f"Invalid config parameter: {e}",
                    parameter="config",
                    invalid_json=True,
                )

            # Ensure config is a dict
            if parsed_config is None or not isinstance(parsed_config, dict):
                return create_validation_error(
                    "Config parameter must be a JSON object",
                    parameter="config",
                    details=f"Received type: {type(parsed_config).__name__}",
                )

            config_dict = cast(dict[str, Any], parsed_config)

            # Normalize field names (triggers -> trigger, actions -> action, etc.)
            config_dict = _normalize_automation_config(config_dict)

            # Validate required fields based on automation type
            # Blueprint automations only need alias, regular automations need trigger and action
            if "use_blueprint" in config_dict:
                required_fields = ["alias"]
                # Strip empty trigger/action/condition arrays that would override blueprint
                config_dict = _strip_empty_automation_fields(config_dict)
            else:
                required_fields = ["alias", "trigger", "action"]

            missing_fields = [f for f in required_fields if f not in config_dict]
            if missing_fields:
                return create_config_error(
                    f"Missing required fields: {', '.join(missing_fields)}",
                    identifier=identifier,
                    missing_fields=missing_fields,
                )

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
            error_response = exception_to_structured_error(
                e,
                context={"identifier": identifier},
            )
            # Add automation-specific suggestions
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Check automation configuration format",
                    "Ensure required fields: alias, trigger, action",
                    "Use entity_id format: automation.morning_routine or unique_id",
                    "Use ha_search_entities(domain_filter='automation') to find automations",
                    "Use ha_get_domain_docs('automation') for comprehensive configuration help",
                ]
            return error_response

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "tags": ["automation"], "title": "Remove Automation"})
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
            error_str = str(e).lower()
            if "404" in error_str or "not found" in error_str:
                error_response = create_resource_not_found_error(
                    "Automation",
                    identifier,
                    details=f"Automation '{identifier}' does not exist",
                )
            else:
                error_response = exception_to_structured_error(
                    e,
                    context={"identifier": identifier},
                )
            error_response["action"] = "delete"
            # Add automation-specific suggestions
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Use entity_id format: automation.morning_routine or unique_id",
                    "Check Home Assistant connection",
                ]
            return error_response
