"""
Configuration management tools for Home Assistant scripts.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant script configurations.
"""

import logging
from typing import Annotated, Any, cast

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    get_security_documentation,
    safe_execute,
)
from .best_practice_checker import (
    check_script_config as _check_best_practices,
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
    fetch_entity_category,
    merge_validation_meta,
    parse_json_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

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


class ConfigScriptTools:
    """Script configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_script",
        tags={"Scripts"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Script Config",
        },
    )
    @log_tool_usage
    async def ha_config_get_script(
        self,
        script_id: Annotated[
            str, Field(description="Script identifier (e.g., 'morning_routine')")
        ],
    ) -> dict[str, Any]:
        """
        Retrieve Home Assistant script configuration.

        Returns the complete configuration for a script, including sequence, mode, fields, and other settings.

        The returned `config_hash` is stable across consecutive reads of an unchanged config — `compute_config_hash` documents the underlying contract.

        EXAMPLES:
        - Get script: ha_config_get_script("morning_routine")
        - Get script: ha_config_get_script("backup_script")

        For detailed script configuration help, use ha_get_skill_home_assistant_best_practices.
        """
        try:
            config_result = await self._client.get_script_config(script_id)
            # Extract actual script config body and compute hash before category injection
            actual_config = config_result.get("config", config_result)
            config_hash_value = compute_config_hash(actual_config)

            # Fetch category from entity registry (best-effort)
            # (injected after hash so transient registry failures don't affect the hash)
            entity_id = f"script.{script_id}"
            cat_id = await fetch_entity_category(self._client, entity_id, "script")
            if cat_id:
                config_result["category"] = cat_id

            return {
                "success": True,
                "action": "get",
                "script_id": script_id,
                "config": config_result,
                "config_hash": config_hash_value,
            }
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"script_id": script_id},
                suggestions=[
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_home_assistant_best_practices for help",
                ],
            )

    async def _get_script_config_internal(
        self, script_id: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch script config without logging or category injection.

        Returns (actual_config, config_hash) tuple where actual_config is
        the inner script body (not the REST wrapper).
        Used internally by _fetch_and_verify_hash and ha_config_get_script.
        """
        config_result = await self._client.get_script_config(script_id)
        actual_config = config_result.get("config", config_result)
        config_hash_value = compute_config_hash(actual_config)
        return actual_config, config_hash_value

    async def _fetch_and_verify_hash(
        self, script_id: str, config_hash: str, action: str
    ) -> dict[str, Any]:
        """Fetch current script config and verify config_hash for optimistic locking.

        Returns the actual script config dict (inner body).
        Raises ToolError if the hash does not match (conflict).
        """
        actual_config, current_hash = await self._get_script_config_internal(script_id)
        if current_hash != config_hash:
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    "Script modified since last read (conflict)",
                    suggestions=[
                        "Call ha_config_get_script() again",
                        "Use the fresh config_hash from that response",
                    ],
                    context={"action": action, "script_id": script_id},
                )
            )
        return actual_config

    @staticmethod
    def _validate_script_config(
        config: str | dict[str, Any],
        script_id: str,
        category: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Parse and validate script config, returning (config_dict, effective_category).

        Parses JSON string config, validates it is a dict, checks for required
        fields (sequence or use_blueprint), extracts category, and strips empty
        blueprint fields.
        """
        # Parse JSON config if provided as string
        try:
            parsed_config = parse_json_param(config, "config")
        except ValueError as e:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_JSON,
                f"Invalid config parameter: {e}",
                context={"script_id": script_id, "provided_config_type": type(config).__name__},
            ))

        # Ensure config is a dict
        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Config parameter must be a JSON object",
                context={"script_id": script_id, "provided_type": type(parsed_config).__name__},
            ))

        config_dict = cast(dict[str, Any], parsed_config)

        # Extract category before sending to HA REST API (which rejects unknown keys).
        # Parameter takes precedence over config dict value.
        config_category = config_dict.pop("category", None)
        effective_category = category if category is not None else config_category

        # Validate required fields based on script type
        # Blueprint scripts only need use_blueprint, regular scripts need sequence
        if "use_blueprint" in config_dict:
            # Strip empty sequence array that would override blueprint
            config_dict = _strip_empty_script_fields(config_dict)
        elif "sequence" not in config_dict:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "config must include either 'sequence' field (for regular scripts) or 'use_blueprint' field (for blueprint-based scripts)",
                context={"script_id": script_id, "required_fields": ["sequence OR use_blueprint"]},
            ))

        return config_dict, effective_category

    @tool(
        name="ha_config_set_script",
        tags={"Scripts"},
        annotations={
            "destructiveHint": True,
            "title": "Create or Update Script",
        },
    )
    @log_tool_usage
    async def ha_config_set_script(
        self,
        script_id: Annotated[
            str, Field(description="Script identifier (e.g., 'morning_routine')")
        ],
        config: Annotated[
            str | dict[str, Any] | None,
            Field(
                description="Script configuration dictionary. Must include EITHER 'sequence' (for regular scripts) OR 'use_blueprint' (for blueprint-based scripts). "
                "Optional fields: 'alias', 'description', 'icon', 'mode', 'max', 'fields'. "
                "Mutually exclusive with python_transform.",
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing script config. "
                "Mutually exclusive with config. "
                "Requires config_hash for validation. "
                "WARNING: Expressions with infinite loops will hang the server. "
                "Examples: "
                "Simple: python_transform=\"config['sequence'][0]['data']['message'] = 'Hello'\" "
                "Pattern: python_transform=\"for step in config['sequence']: "
                "if step.get('alias') == 'My Step': step['data']['value'] = 100\" "
                "\n\n" + get_security_documentation(),
            ),
        ] = None,
        config_hash: Annotated[
            str | None,
            Field(
                description="Config hash from ha_config_get_script for optimistic locking. "
                "REQUIRED for python_transform (validates script unchanged). "
                "Optional for config updates (validates before full replacement if provided).",
            ),
        ] = None,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this script. Use ha_config_get_category(scope='script') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for script to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant script.

        Supports two modes: full config replacement OR Python transformation.

        WHEN TO USE WHICH MODE:
        - python_transform: RECOMMENDED for edits to existing scripts. Surgical updates.
        - config: Use for creating new scripts or full restructures.

        IMPORTANT: python_transform requires 'config_hash' from ha_config_get_script().

        PYTHON TRANSFORM EXAMPLES:
        - Update step: python_transform="config['sequence'][0]['data']['message'] = 'Hello'"
        - Add step: python_transform="config['sequence'].append({'delay': {'seconds': 5}})"
        - Remove last step: python_transform="config['sequence'].pop()"

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

        SCRIPTS vs AUTOMATIONS: Scripts use 'sequence', NOT 'trigger' or 'action'.
        If you need trigger-based execution, use ha_config_set_automation instead.

        EXAMPLES:

        Create basic delay script:
        ha_config_set_script(script_id="wait_script", config={
            "sequence": [{"delay": {"seconds": 5}}],
            "alias": "Wait 5 Seconds",
            "description": "Simple delay script"
        })

        Create service call script:
        ha_config_set_script(script_id="blink_light", config={
            "sequence": [
                {"service": "light.turn_on", "target": {"entity_id": "light.living_room"}},
                {"delay": {"seconds": 2}},
                {"service": "light.turn_off", "target": {"entity_id": "light.living_room"}}
            ],
            "alias": "Light Blink",
            "mode": "single"
        })

        Create script with parameters:
        ha_config_set_script(script_id="backup_script", config={
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
        ha_config_set_script(script_id="morning_routine", config={
            "sequence": [
                {"service": "light.turn_on", "target": {"area_id": "bedroom"}},
                {"service": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}}
            ],
            "alias": "Updated Morning Routine"
        })

        Create blueprint-based script:
        ha_config_set_script(script_id="notification_script", config={
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
        ha_config_set_script(script_id="notification_script", config={
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

        For detailed script configuration help, use ha_get_skill_home_assistant_best_practices.

        Note: Scripts use Home Assistant's action syntax. Check the documentation for advanced
        features like conditions, variables, parallel execution, and service call options.
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
                            "python_transform: Python-based edits (recommended for existing scripts)",
                        ],
                        context={"action": "set", "script_id": script_id},
                    )
                )

            # Handle python_transform mode
            if python_transform is not None:
                if config_hash is None:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_PARAMETER,
                            "config_hash is required for python_transform",
                            suggestions=[
                                "Call ha_config_get_script() first",
                                "Use the config_hash from that response",
                            ],
                            context={"action": "python_transform", "script_id": script_id},
                        )
                    )

                # Fetch current config and verify hash
                actual_config = await self._fetch_and_verify_hash(
                    script_id, config_hash, "python_transform"
                )

                # Apply Python transformation on the actual script config
                try:
                    transformed_config = safe_execute(python_transform, actual_config)
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
                            context={"action": "python_transform", "script_id": script_id},
                        )
                    )

                # Validate transformed config
                if "sequence" not in transformed_config and "use_blueprint" not in transformed_config:
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transformed config must include either 'sequence' or 'use_blueprint'",
                            suggestions=[
                                "The transform may have removed required fields",
                                "Ensure the config still has a 'sequence' or 'use_blueprint' key",
                            ],
                            context={"action": "python_transform", "script_id": script_id},
                        )
                    )
                bp_warnings = _check_best_practices(
                    transformed_config, skill_prefix=_get_skill_prefix()
                )

                # Save transformed config
                result = await self._client.upsert_script_config(
                    transformed_config, script_id
                )

                # Re-fetch to get authoritative hash (HA may normalize after save)
                _, new_config_hash = await self._get_script_config_internal(script_id)

                response: dict[str, Any] = {
                    "success": True,
                    "action": "python_transform",
                    "script_id": script_id,
                    "config_hash": new_config_hash,
                    "python_expression": python_transform,
                    "message": f"Script {script_id} updated via Python transform",
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
                            "config: Full script configuration for create/replace",
                            "python_transform: Python expression for surgical edits",
                        ],
                        context={"action": "set", "script_id": script_id},
                    )
                )

            config_dict, effective_category = self._validate_script_config(
                config, script_id, category,
            )

            # Optional hash check for full config updates
            if config_hash:
                await self._fetch_and_verify_hash(script_id, config_hash, "set")

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

            result = await self._client.upsert_script_config(config_dict, script_id)

            # Wait for script to be queryable
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = f"script.{script_id}"
            if wait_bool:
                try:
                    registered = await wait_for_entity_registered(self._client, entity_id)
                    if not registered:
                        result["warning"] = f"Script created but {entity_id} not yet queryable. It may take a moment to become available."
                except Exception as e:
                    result["warning"] = f"Script created but verification failed: {e}"

            # Apply category to entity registry if provided
            if effective_category and entity_id:
                await apply_entity_category(
                    self._client, entity_id, effective_category, "script", result, "script"
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
            suggestions = [
                "Ensure config includes either 'sequence' field (regular scripts) or 'use_blueprint' field (blueprint-based scripts)",
                "For blueprint scripts, use ha_get_blueprint(domain='script') to list available blueprints",
                "Validate sequence actions syntax for regular scripts",
                "Check entity_ids exist if using service calls",
                "Use ha_search_entities(domain_filter='script') to find scripts",
                "Use ha_get_skill_home_assistant_best_practices for help",
            ]
            if bp_warnings:
                suggestions.append(
                    "Config had best-practice issues that may be related: "
                    + "; ".join(bp_warnings)
                )
            exception_to_structured_error(
                e,
                context={"script_id": script_id},
                suggestions=suggestions,
            )

    @tool(
        name="ha_config_remove_script",
        tags={"Scripts"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Script",
        },
    )
    @log_tool_usage
    async def ha_config_remove_script(
        self,
        script_id: Annotated[
            str, Field(description="Script identifier to delete (e.g., 'old_script')")
        ],
        wait: Annotated[
            bool | str,
            Field(
                description="Wait for script to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
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
            result = await self._client.delete_script_config(script_id)

            # Wait for script to be removed
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = f"script.{script_id}"
            if wait_bool:
                try:
                    removed = await wait_for_entity_removed(self._client, entity_id)
                    if not removed:
                        result["warning"] = f"Deletion confirmed by API but {entity_id} may still appear briefly."
                except Exception as e:
                    result["warning"] = f"Deletion confirmed but removal verification failed: {e}"

            return {"success": True, "action": "delete", **result}
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"script_id": script_id},
                suggestions=[
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check if script is being used by automations",
                    "Use ha_get_skill_home_assistant_best_practices for help",
                ],
            )


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""
    register_tool_methods(mcp, ConfigScriptTools(client))
