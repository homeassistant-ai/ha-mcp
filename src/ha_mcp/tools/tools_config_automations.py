"""
Configuration management tools for Home Assistant automations.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant automation configurations.
"""

import logging
from typing import Annotated, Any, cast

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..errors import (
    create_config_error,
    create_resource_not_found_error,
    create_validation_error,
)
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import (
    coerce_bool_param,
    parse_json_param,
    validate_guide_response,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


def _normalize_automation_config(
    config: Any,
    parent_key: str | None = None,
    in_choose_or_if: bool = False,
    is_root: bool = True,
) -> Any:
    """
    Recursively normalize automation config field names to HA API format.

    Home Assistant accepts both singular ('trigger', 'action', 'condition')
    and plural ('triggers', 'actions', 'conditions') field names in YAML,
    but the API expects singular forms at the root level.

    IMPORTANT: 'triggers' → 'trigger' and 'actions' → 'action' normalization
    is ONLY applied at the root level. Deeper in the tree these keys are either
    invalid or semantically different, and normalizing them can produce keys
    that Home Assistant rejects (e.g., 'action' inside a delay object).

    IMPORTANT: Inside 'choose' and 'if' action blocks, the 'conditions' key
    (plural) is required by the HA schema and should NOT be normalized to
    'condition' (singular).

    IMPORTANT: Inside compound condition blocks ('or', 'and', 'not'), the
    'conditions' key (plural) is required and should NOT be normalized to
    'condition' (singular).

    Args:
        config: Automation configuration (dict, list, or primitive)
        parent_key: The parent dictionary key (for context tracking)
        in_choose_or_if: Whether we're inside a choose/if option that requires
                         'conditions' (plural) to remain unchanged
        is_root: Whether this is the root-level automation config dict.
                 Only root level gets 'triggers'→'trigger' and
                 'actions'→'action' normalization.

    Returns:
        Normalized configuration with singular field names at root level,
        but preserving 'conditions' (plural) inside choose/if blocks and
        compound condition blocks (or/and/not)
    """
    # Handle lists - recursively process each item
    if isinstance(config, list):
        # If parent is 'choose' or 'if', items are options that need 'conditions' preserved
        is_option_list = parent_key in ("choose", "if")
        return [
            _normalize_automation_config(
                item, parent_key, is_option_list, is_root=False
            )
            for item in config
        ]

    # Handle primitives (strings, numbers, etc.)
    if not isinstance(config, dict):
        return config

    # Process dictionary
    normalized = config.copy()

    # Check if this dict is a compound condition block (or/and/not)
    # that needs its nested 'conditions' key preserved
    is_compound_condition_block = normalized.get("condition") in ("or", "and", "not")

    # Build field mappings based on context
    field_mappings: dict[str, str] = {}

    # 'triggers' → 'trigger' and 'actions' → 'action' ONLY at root level.
    # Deeper in the tree these keys are invalid and normalizing them produces
    # keys HA rejects (e.g., 'action' inside a delay object — see issue #498).
    if is_root:
        field_mappings["triggers"] = "trigger"
        field_mappings["actions"] = "action"

    # 'sequences' → 'sequence' is safe at any level (only meaningful in choose options)
    field_mappings["sequences"] = "sequence"

    # Only add 'conditions' mapping if NOT inside a choose/if option
    # AND NOT a compound condition block (or/and/not)
    if not in_choose_or_if and not is_compound_condition_block:
        field_mappings["conditions"] = "condition"

    # Apply field mapping to current level
    for plural, singular in field_mappings.items():
        if plural in normalized and singular not in normalized:
            normalized[singular] = normalized.pop(plural)
        elif plural in normalized and singular in normalized:
            # Both exist - prefer singular, remove plural
            del normalized[plural]

    # Recursively process all values in the dictionary
    for key, value in normalized.items():
        normalized[key] = _normalize_automation_config(value, key, is_root=False)

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

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["automation"],
            "title": "Get Automation Config",
        }
    )
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
                raise_tool_error(error_response)

            logger.error(f"Error getting automation: {e}")
            error_response = exception_to_structured_error(
                e,
                context={"identifier": identifier, "action": "get"},
                raise_error=False,
            )
            # Add automation-specific suggestions
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Check Home Assistant connection",
                    "Use ha_get_domain_docs('automation') for configuration help",
                ]
            raise_tool_error(error_response)

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["automation"],
            "title": "Create or Update Automation",
        }
    )
    @log_tool_usage
    async def ha_config_set_automation(
        config: str | dict[str, Any],
        guide_response: Annotated[
            str | dict[str, Any],
            Field(
                description="REQUIRED: Output from ha_get_tool_guide('automation')"
            ),
        ],
        identifier: Annotated[
            str | None,
            Field(
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool | str,
            Field(
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Create or update an automation. Omit identifier to create new.

        The guide_response parameter enforces that ha_get_tool_guide('automation')
        was called first. The guide contains required field schemas, examples, and
        critical warnings about native vs template usage.
        Required fields: alias, trigger, action (or use_blueprint with path+input).
        Optional: description, condition, mode (single/restart/queued/parallel), max, variables.
        Also: ha_get_domain_docs("automation") for HA trigger/condition/action reference."""
        try:
            # Validate guide_response - enforces ha_get_tool_guide() was called first
            try:
                validate_guide_response(guide_response, "automation")
            except ValueError as e:
                raise_tool_error(create_validation_error(str(e), parameter="guide_response"))

            # Parse JSON config if provided as string
            try:
                parsed_config = parse_json_param(config, "config")
            except ValueError as e:
                raise_tool_error(create_validation_error(
                    f"Invalid config parameter: {e}",
                    parameter="config",
                    invalid_json=True,
                ))

            # Ensure config is a dict
            if parsed_config is None or not isinstance(parsed_config, dict):
                raise_tool_error(create_validation_error(
                    "Config parameter must be a JSON object",
                    parameter="config",
                    details=f"Received type: {type(parsed_config).__name__}",
                ))

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
                raise_tool_error(create_config_error(
                    f"Missing required fields: {', '.join(missing_fields)}",
                    identifier=identifier,
                    missing_fields=missing_fields,
                ))

            result = await client.upsert_automation_config(config_dict, identifier)

            # Wait for automation to be queryable
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = result.get("entity_id")
            if wait_bool and entity_id:
                try:
                    registered = await wait_for_entity_registered(client, entity_id)
                    if not registered:
                        result["warning"] = f"Automation created but {entity_id} not yet queryable. It may take a moment to become available."
                except Exception as e:
                    result["warning"] = f"Automation created but verification failed: {e}"

            return {
                "success": True,
                **result,
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error upserting automation: {e}")
            error_response = exception_to_structured_error(
                e,
                context={"identifier": identifier},
                raise_error=False,
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
            raise_tool_error(error_response)

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["automation"],
            "title": "Remove Automation",
        }
    )
    @log_tool_usage
    async def ha_config_remove_automation(
        identifier: Annotated[
            str,
            Field(
                description="Automation entity_id (e.g., 'automation.old_automation') or unique_id to delete"
            ),
        ],
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for automation to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant automation.

        EXAMPLES:
        - Delete automation: ha_config_remove_automation("automation.old_automation")
        - Delete by unique_id: ha_config_remove_automation("my_unique_id")

        **WARNING:** Deleting an automation removes it permanently from your Home Assistant configuration.
        """
        try:
            # Resolve entity_id for wait verification (identifier may be a unique_id)
            entity_id_for_wait: str | None = None
            if identifier.startswith("automation."):
                entity_id_for_wait = identifier
            else:
                # Try to find entity_id by matching unique_id in automation states
                try:
                    states = await client.get_states()
                    for state in states:
                        eid = state.get("entity_id", "")
                        if eid.startswith("automation.") and state.get("attributes", {}).get("id") == identifier:
                            entity_id_for_wait = eid
                            break
                except Exception as e:
                    logger.warning(f"Could not resolve unique_id '{identifier}' to entity_id: {e} — wait verification will be skipped")

            result = await client.delete_automation_config(identifier)

            # Wait for entity to be removed
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            if wait_bool and entity_id_for_wait:
                try:
                    removed = await wait_for_entity_removed(client, entity_id_for_wait)
                    if not removed:
                        result["warning"] = f"Deletion confirmed by API but {entity_id_for_wait} may still appear briefly."
                except Exception as e:
                    result["warning"] = f"Deletion confirmed but removal verification failed: {e}"

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
                    raise_error=False,
                )
            error_response["action"] = "delete"
            # Add automation-specific suggestions
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Use entity_id format: automation.morning_routine or unique_id",
                    "Check Home Assistant connection",
                ]
            raise_tool_error(error_response)
