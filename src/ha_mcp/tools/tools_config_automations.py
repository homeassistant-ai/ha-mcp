"""
Configuration management tools for Home Assistant automations.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant automation configurations.
"""

import logging
from typing import Annotated, Any, cast

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import (
    ErrorCode,
    create_config_error,
    create_error_response,
    create_resource_not_found_error,
    create_validation_error,
)
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    get_security_documentation,
    safe_execute,
)
from .best_practice_checker import (
    check_automation_config as _check_best_practices,
)
from .best_practice_checker import (
    get_skill_prefix as _get_skill_prefix,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .reference_validator import validate_config_references
from .util_helpers import (
    apply_entity_category,
    coerce_bool_param,
    coerce_to_list,
    fetch_entity_category,
    merge_validation_meta,
    parse_json_param,
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

    IMPORTANT: 'triggers' -> 'trigger' and 'actions' -> 'action' normalization
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
                 Only root level gets 'triggers'->'trigger' and
                 'actions'->'action' normalization.

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

    # 'triggers' -> 'trigger' and 'actions' -> 'action' ONLY at root level.
    # Deeper in the tree these keys are invalid and normalizing them produces
    # keys HA rejects (e.g., 'action' inside a delay object -- see issue #498).
    if is_root:
        field_mappings["triggers"] = "trigger"
        field_mappings["actions"] = "action"

    # 'sequences' -> 'sequence' is safe at any level (only meaningful in choose options)
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

    return cast(dict[str, Any], normalized)


class AutomationConfigTools:
    """Configuration management tools for Home Assistant automations."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _resolve_automation_entity_id(self, identifier: str) -> str | None:
        """Resolve an automation identifier to its entity_id.

        If identifier is already an entity_id (starts with "automation."),
        returns it directly. Otherwise, searches states to find the entity
        whose unique_id matches the identifier.
        """
        if identifier.startswith("automation."):
            return identifier
        try:
            states = await self._client.get_states()
            for state in states:
                if (
                    state.get("entity_id", "").startswith("automation.")
                    and state.get("attributes", {}).get("id") == identifier
                ):
                    return str(state["entity_id"])
        except Exception as e:
            logger.debug(f"Failed to resolve entity_id for automation {identifier}: {e}")
        return None

    @tool(
        name="ha_config_get_automation",
        tags={"Automations"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Automation Config",
        },
    )
    @log_tool_usage
    async def ha_config_get_automation(
        self,
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

        The returned `config_hash` is stable across consecutive reads of an unchanged config — `compute_config_hash` documents the underlying contract.

        EXAMPLES:
        - Get automation: ha_config_get_automation("automation.morning_routine")
        - Get by unique_id: ha_config_get_automation("my_unique_automation_id")

        For comprehensive automation documentation, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            normalized_config, config_hash = await self._get_automation_config_internal(identifier)

            # Resolve entity_id and fetch category from entity registry
            # (injected after hash so transient registry failures don't affect the hash)
            entity_id = await self._resolve_automation_entity_id(identifier)
            if entity_id:
                cat_id = await fetch_entity_category(self._client, entity_id, "automation")
                if cat_id:
                    normalized_config["category"] = cat_id

            return {
                "success": True,
                "action": "get",
                "identifier": identifier,
                "config": normalized_config,
                "config_hash": config_hash,
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
            exception_to_structured_error(
                e,
                context={"identifier": identifier, "action": "get"},
                suggestions=[
                    "Verify automation exists using ha_search_entities(domain_filter='automation')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_home_assistant_best_practices for help",
                ],
            )

    @tool(
        name="ha_config_set_automation",
        tags={"Automations"},
        annotations={
            "destructiveHint": True,
            "title": "Create or Update Automation",
        },
    )
    @log_tool_usage
    async def ha_config_set_automation(
        self,
        config: Annotated[
            str | dict[str, Any] | None,
            Field(
                description="Complete automation configuration with required fields: 'alias', 'trigger', 'action'. "
                "Optional: 'description', 'condition', 'mode', 'max', 'initial_state', 'variables'. "
                "Mutually exclusive with python_transform.",
                default=None,
            ),
        ] = None,
        identifier: Annotated[
            str | None,
            Field(
                description="Automation entity_id or unique_id for updates. "
                "Required for python_transform. Omit to create new automation with generated unique_id.",
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing automation config. "
                "Mutually exclusive with config. "
                "Requires identifier and config_hash for validation. "
                "WARNING: Expressions with infinite loops will hang the server. "
                "Examples: "
                "Simple: python_transform=\"config['action'][0]['data']['brightness'] = 255\" "
                "Pattern: python_transform=\"for a in config['action']: "
                "if a.get('alias') == 'My Step': a['data']['value'] = 100\" "
                "\n\n" + get_security_documentation(),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="Config hash from ha_config_get_automation for optimistic locking. "
                "REQUIRED for python_transform (validates automation unchanged). "
                "Optional for config updates (validates before full replacement if provided).",
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this automation. Use ha_config_get_category(scope='automation') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for automation to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant automation.

        Supports two modes: full config replacement OR Python transformation.

        WHEN TO USE WHICH MODE:
        - python_transform: RECOMMENDED for edits to existing automations. Surgical updates.
        - config: Use for creating new automations or full restructures.

        IMPORTANT: python_transform requires 'identifier' and 'config_hash' from ha_config_get_automation().

        PYTHON TRANSFORM EXAMPLES:
        - Update action: python_transform="config['action'][0]['data']['brightness'] = 255"
        - Add trigger: python_transform="config['trigger'].append({'platform': 'state', 'entity_id': 'binary_sensor.motion', 'to': 'on'})"
        - Remove last action: python_transform="config['action'].pop()"

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
        - category: Category ID for organization (use ha_config_get_category to list, ha_config_set_category to create)
        - condition: Additional conditions that must be met
        - mode: 'single' (default), 'restart', 'queued', 'parallel'
        - max: Maximum concurrent executions (for queued/parallel modes)
        - initial_state: Whether automation starts enabled (true/false)
        - variables: Variables for use in automation

        BASIC EXAMPLES:

        Simple time-based automation:
        ha_config_set_automation(config={
            "alias": "Morning Lights",
            "description": "Turn on bedroom lights at 7 AM to help wake up",
            "trigger": [{"platform": "time", "at": "07:00:00"}],
            "action": [{"service": "light.turn_on", "target": {"area_id": "bedroom"}}]
        })

        Motion-activated lighting with condition:
        ha_config_set_automation(config={
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
        ha_config_set_automation(config={
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
        )

        PREFER NATIVE SOLUTIONS OVER TEMPLATES:
        Before using template triggers/conditions/actions, check if a native option exists:
        - Use `condition: state` with `state: [list]` instead of template for multiple states
        - Use `condition: state` with `attribute:` instead of template for attribute checks
        - Use `condition: numeric_state` instead of template for number comparisons
        - Use `wait_for_trigger` instead of `wait_template` when waiting for state changes
        - Use `choose` action instead of template-based service names

        TRIGGER TYPES: time, time_pattern, sun, state, numeric_state, event, device, zone, template, and more
        CONDITION TYPES: state, numeric_state, time, sun, template, device, zone, and more
        ACTION TYPES: service calls, delays, wait_for_trigger, wait_template, if/then/else, choose, repeat, parallel

        For comprehensive automation documentation with all trigger/condition/action types and advanced examples:
        - Use: ha_get_skill_home_assistant_best_practices
        - Or visit: https://www.home-assistant.io/docs/automation/

        TROUBLESHOOTING:
        - Use ha_get_state() to verify entity_ids exist
        - Use ha_search_entities() to find correct entity_ids
        - Use ha_eval_template() to test Jinja2 templates before using in automations
        - Use ha_search_entities(domain_filter='automation') to find existing automations
        """
        bp_warnings: list[str] = []
        try:
            # Validate mutual exclusivity of config and python_transform
            if config is not None and python_transform is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Cannot use both config and python_transform simultaneously",
                        suggestions=[
                            "Use only ONE of: config or python_transform",
                            "config: Full replacement",
                            "python_transform: Python-based edits (recommended for existing automations)",
                        ],
                        context={"action": "set", "identifier": identifier},
                    )
                )

            # Handle python_transform mode
            if python_transform is not None:
                if not identifier:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "identifier is required for python_transform",
                            suggestions=[
                                "Provide the automation entity_id or unique_id",
                                "Use ha_search_entities(domain_filter='automation') to find automations",
                            ],
                            context={"action": "python_transform", "identifier": identifier},
                        )
                    )
                if config_hash is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "config_hash is required for python_transform",
                            suggestions=[
                                "Call ha_config_get_automation() first",
                                "Use the config_hash from that response",
                            ],
                            context={"action": "python_transform", "identifier": identifier},
                        )
                    )

                # Fetch current config and verify hash
                current_config = await self._fetch_and_verify_hash(
                    identifier, config_hash, "python_transform"
                )

                # Apply Python transformation
                try:
                    transformed_config = safe_execute(python_transform, current_config)
                except PythonSandboxError as e:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            str(e),
                            suggestions=[
                                "Check expression syntax",
                                "Ensure only allowed operations are used",
                                "See tool description for allowed operations",
                                f"Expression: {python_transform[:100]}{'...' if len(python_transform) > 100 else ''}",
                            ],
                            context={"action": "python_transform", "identifier": identifier},
                        )
                    )

                # Pop category before sending to HA REST API (rejects unknown keys)
                transform_category = transformed_config.pop("category", None)

                # Normalize and validate the transformed config
                transformed_config = _normalize_automation_config(transformed_config)
                self._validate_required_fields(transformed_config, identifier)
                bp_warnings = _check_best_practices(
                    transformed_config, skill_prefix=_get_skill_prefix()
                )

                # Save transformed config
                result = await self._client.upsert_automation_config(
                    transformed_config, identifier
                )

                # Re-fetch to get authoritative hash (HA may normalize after save)
                refetched = await self._get_automation_config_internal(identifier)
                new_config_hash = refetched[1]  # (config, hash) tuple

                # Re-apply category if present
                entity_id = result.get("entity_id")
                if not entity_id and identifier and identifier.startswith("automation."):
                    entity_id = identifier
                if transform_category and entity_id:
                    await apply_entity_category(
                        self._client, entity_id, transform_category, "automation", result, "automation"
                    )

                response: dict[str, Any] = {
                    "success": True,
                    "action": "python_transform",
                    "identifier": identifier,
                    "config_hash": new_config_hash,
                    "python_expression": python_transform,
                    "message": f"Automation {identifier} updated via Python transform",
                    # Merge upsert result, excluding "success" (we set it ourselves)
                    **{k: v for k, v in result.items() if k != "success"},
                }
                if bp_warnings:
                    response["best_practice_warnings"] = bp_warnings
                return response

            if config is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Either config or python_transform must be provided",
                        suggestions=[
                            "config: Full automation configuration for create/replace",
                            "python_transform: Python expression for surgical edits",
                        ],
                        context={"action": "set", "identifier": identifier},
                    )
                )

            config_dict = self._parse_and_validate_config(config)

            # Extract category before sending to HA REST API (which rejects unknown keys).
            # Parameter takes precedence over config dict value.
            config_category = config_dict.pop("category", None)
            effective_category = category if category is not None else config_category

            # Normalize field names (triggers -> trigger, actions -> action, etc.)
            config_dict = _normalize_automation_config(config_dict)

            # Optional hash check for full config updates
            if identifier and config_hash:
                await self._fetch_and_verify_hash(identifier, config_hash, "set")

            # Validate required fields based on automation type
            self._validate_required_fields(config_dict, identifier)

            # Pre-check for best-practice issues.
            bp_warnings = _check_best_practices(
                config_dict, skill_prefix=_get_skill_prefix()
            )

            # Cross-check literal service and entity references against
            # the live registries. Soft warnings only — the write still
            # happens, even when references don't resolve (#940).
            validation_meta = await validate_config_references(
                self._client, config_dict
            )

            result = await self._client.upsert_automation_config(config_dict, identifier)

            # If the client could not verify the entity was registered, warn but don't hard-fail.
            if result.get("entity_not_verified"):
                result["warning"] = (
                    "Automation was submitted to Home Assistant but the entity was not found "
                    "after polling. The automation may still have been created -- check Home "
                    "Assistant logs and try reloading automations. Common causes: "
                    "automations.yaml vs automation.yaml filename mismatch, invalid config "
                    "that HA accepted but failed to load, or slow hardware."
                )
                result.pop("entity_not_verified", None)

            # Wait for automation to be queryable
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = result.get("entity_id")
            # On updates, entity_id may not be in the result -- derive from identifier
            if not entity_id and identifier and identifier.startswith("automation."):
                entity_id = identifier
            if wait_bool and entity_id:
                try:
                    registered = await wait_for_entity_registered(self._client, entity_id)
                    if not registered:
                        result["warning"] = f"Automation created but {entity_id} not yet queryable. It may take a moment to become available."
                except Exception as e:
                    result["warning"] = f"Automation created but verification failed: {e}"

            # Apply category to entity registry if provided
            if effective_category and entity_id:
                await apply_entity_category(
                    self._client, entity_id, effective_category, "automation", result, "automation"
                )

            if bp_warnings:
                result["best_practice_warnings"] = bp_warnings

            merge_validation_meta(result, validation_meta)

            return {
                "success": True,
                **result,
            }

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error upserting automation: {e}")
            suggestions = [
                "Check automation configuration format",
                "Ensure required fields: alias, trigger, action",
                "Use entity_id format: automation.morning_routine or unique_id",
                "Use ha_search_entities(domain_filter='automation') to find automations",
                "Use ha_get_skill_home_assistant_best_practices for help",
            ]
            if bp_warnings:
                suggestions.append(
                    "Config had best-practice issues that may be related: "
                    + "; ".join(bp_warnings)
                )
            exception_to_structured_error(
                e,
                context={"identifier": identifier},
                suggestions=suggestions,
            )

    async def _get_automation_config_internal(
        self, identifier: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch and normalize automation config without logging or category injection.

        Returns (normalized_config, config_hash) tuple.
        Used internally by _fetch_and_verify_hash and ha_config_get_automation.
        """
        config_result = await self._client.get_automation_config(identifier)
        normalized_config = _normalize_config_for_roundtrip(config_result)
        config_hash_value = compute_config_hash(normalized_config)
        return normalized_config, config_hash_value

    async def _fetch_and_verify_hash(
        self, identifier: str, config_hash: str, action: str
    ) -> dict[str, Any]:
        """Fetch current automation config and verify config_hash for optimistic locking.

        Returns the current normalized config dict.
        Raises ToolError if the hash does not match (conflict).
        """
        current_config, current_hash = await self._get_automation_config_internal(identifier)
        if current_hash != config_hash:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Automation modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_automation() again",
                        "Use the fresh config_hash from that response",
                    ],
                    context={"action": action, "identifier": identifier},
                )
            )
        return current_config

    @staticmethod
    def _parse_and_validate_config(config: str | dict[str, Any]) -> dict[str, Any]:
        """Parse JSON config and validate it is a dict."""
        try:
            parsed_config = parse_json_param(config, "config")
        except ValueError as e:
            raise_tool_error(create_error_response(
                code=ErrorCode.VALIDATION_INVALID_JSON,
                message=f"Invalid config parameter: {e}",
                suggestions=[
                    "Pass 'config' as a dict, not a JSON string, to avoid escaping issues.",
                    "Check for JSON syntax errors: unquoted keys, trailing commas, or invalid escape sequences.",
                ],
                context={"parameter": "config"},
            ))

        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(create_validation_error(
                "Config parameter must be a JSON object",
                parameter="config",
                details=f"Received type: {type(parsed_config).__name__}",
            ))

        return cast(dict[str, Any], parsed_config)

    @staticmethod
    def _validate_required_fields(
        config_dict: dict[str, Any], identifier: str | None
    ) -> None:
        """Validate required fields and prevent duplicate creation."""
        if "use_blueprint" in config_dict:
            required_fields = ["alias"]
            # Strip empty trigger/action/condition arrays that would override blueprint
            for field in ["trigger", "action", "condition"]:
                if field in config_dict and config_dict[field] == []:
                    del config_dict[field]
        else:
            required_fields = ["alias", "trigger", "action"]

        missing_fields = [f for f in required_fields if f not in config_dict]
        if missing_fields:
            # If the caller supplied a 'sequence' key, the config looks like a
            # script — point them at ha_config_set_script instead of the generic
            # missing-fields error.
            if "sequence" in config_dict and (
                "trigger" in missing_fields or "action" in missing_fields
            ):
                context: dict[str, Any] = {"missing_fields": missing_fields}
                if identifier:
                    context["identifier"] = identifier
                raise_tool_error(create_error_response(
                    code=ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                    message=f"Missing required fields: {', '.join(missing_fields)}",
                    details=(
                        "Config contains 'sequence', which belongs to scripts. "
                        "Automations use 'trigger' and 'action'; scripts use 'sequence'."
                    ),
                    suggestions=[
                        "Did you mean ha_config_set_script? Scripts use 'sequence' directly.",
                        "For an automation, replace 'sequence' with 'action' and add a 'trigger'.",
                    ],
                    context=context,
                ))
            raise_tool_error(create_config_error(
                f"Missing required fields: {', '.join(missing_fields)}",
                identifier=identifier,
                missing_fields=missing_fields,
            ))

        # HA accepts conditions with 'platform' (trigger syntax) but then crashes
        # with an unhelpful 500 rather than a 400 validation error.
        for idx, cond in enumerate(coerce_to_list(config_dict.get("condition"))):
            if not isinstance(cond, dict):
                continue
            if "platform" in cond and "condition" not in cond:
                raise_tool_error(create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message=(
                        f"Condition at index {idx} uses 'platform' (trigger syntax). "
                        "Conditions use 'condition', not 'platform'."
                    ),
                    suggestions=[
                        f"Replace 'platform' with 'condition': "
                        f"{{'condition': '{cond['platform']}', ...}}",
                        "Triggers use 'platform'; conditions use 'condition'.",
                    ],
                    context={"condition_index": idx, "found_key": "platform"},
                ))

        # Prevent duplicate creation when config contains an existing automation id
        if identifier is None and "id" in config_dict:
            existing_id = config_dict["id"]
            raise_tool_error(create_validation_error(
                f"Config contains 'id' field ('{existing_id}') but no identifier was provided. "
                "This would create a duplicate automation instead of updating the existing one.",
                parameter="identifier",
                details=f"To update, pass identifier='{existing_id}' (or the automation's entity_id). "
                "To create a genuinely new automation, remove the 'id' field from the config.",
            ))

    @tool(
        name="ha_config_remove_automation",
        tags={"Automations"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Automation",
        },
    )
    @log_tool_usage
    async def ha_config_remove_automation(
        self,
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
            entity_id_for_wait = await self._resolve_automation_entity_id(identifier)
            if not entity_id_for_wait:
                logger.warning(
                    f"Could not resolve unique_id '{identifier}' to entity_id -- wait verification will be skipped"
                )

            result = await self._client.delete_automation_config(identifier)

            # Wait for entity to be removed
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            if wait_bool and entity_id_for_wait:
                try:
                    removed = await wait_for_entity_removed(self._client, entity_id_for_wait)
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


def register_config_automation_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant automation configuration tools."""
    register_tool_methods(mcp, AutomationConfigTools(client))
