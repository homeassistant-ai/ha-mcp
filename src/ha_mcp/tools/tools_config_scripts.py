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

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
)
from .auto_backup import with_auto_backup
from .best_practice_checker import (
    BestPracticeCheckResult,
)
from .best_practice_checker import (
    check_script_config as _check_best_practices,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .reference_validator import validate_config_references
from .util_helpers import (
    apply_entity_category,
    attach_skill_content,
    coerce_bool_param,
    fetch_entity_category,
    merge_validation_meta,
    parse_json_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


# Scripts share the automation skill mapping — both use
# action / condition / trigger templates and benefit from the same
# native-vs-template guidance.
_SCRIPT_SKILL_FILES: tuple[str, ...] = (
    "references/automation-patterns.md",
    "references/template-guidelines.md",
)


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
            str,
            Field(
                description="Script identifier — bare storage key ('morning_routine') or entity_id form ('script.morning_routine'); a leading 'script.' prefix is stripped before lookup."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Retrieve Home Assistant script configuration.

        Returns the complete configuration for a script, including sequence, mode, fields, and other settings.

        The returned `config_hash` is stable across consecutive reads of an unchanged config — `compute_config_hash` documents the underlying contract.

        The returned `script_id` is the canonical bare storage key resolved by the REST client (matching what `ha_config_set_script` / `ha_config_remove_script` expect), falling back to the input identifier on the rare path where the REST envelope omits it. A leading `script.` prefix on the input is stripped before lookup — behavioral parity with `ha_config_get_automation` (mechanism differs: automations resolve via state lookup; scripts strip the prefix).

        EXAMPLES:
        - Get script (bare form): ha_config_get_script("morning_routine")
        - Get script (entity_id form): ha_config_get_script("script.morning_routine")

        For detailed script configuration help, use ha_get_skill_guide.
        """
        try:
            # Strip BEFORE validate so a bare ``"script."`` (empty after
            # strip) is rejected as ``VALIDATION_INVALID_PARAMETER`` rather
            # than slipping through validate (non-empty pre-strip) and
            # 404-ing at ``get_script_config("")``. Accept entity_id form
            # (``script.foo``) and bare storage key (``foo``) — behavioral
            # parity with ``ha_config_get_automation`` (mechanism differs:
            # automations resolve via state lookup; scripts strip the
            # prefix). ``_raise_script_not_found`` suggests
            # ``ha_search_entities(domain_filter='script')`` which returns
            # entity_ids; without this strip, feeding that output back into
            # the GET tool fails and reseeds the wrong-tool spiral that
            # #1297 closes.
            script_id = script_id.removeprefix("script.")
            # Empty/whitespace script_id would propagate to
            # ``get_script_config`` and surface as a misleading
            # ``RESOURCE_NOT_FOUND``. Extension of the #1312
            # validate_identifier_not_empty pattern to the scripts
            # family per #1313.
            validate_identifier_not_empty(
                script_id,
                "script_id",
                suggestions=[
                    "Pass a script identifier (e.g. 'morning_routine')",
                    "Use ha_search_entities(domain_filter='script') to list scripts",
                ],
            )
            config_result = await self._fetch_script_config_envelope(script_id)
            # Extract actual script config body and compute hash before category injection
            actual_config = config_result.get("config", config_result)
            config_hash_value = compute_config_hash(actual_config)

            # Fetch category from entity registry (best-effort)
            # (injected after hash so transient registry failures don't affect the hash)
            entity_id = f"script.{script_id}"
            cat_id = await fetch_entity_category(self._client, entity_id, "script")
            if cat_id:
                config_result["category"] = cat_id

            # Issue #1334: return the canonical storage key from the
            # rest_client envelope so callers can thread the result into
            # subsequent ha_config_*_script calls without re-resolving.
            # Falls back to the input when the rest_client response omits
            # the key — a contract violation that we surface via warning
            # rather than mask silently.
            canonical_id = config_result.get("script_id")
            if canonical_id is None:
                logger.warning(
                    "get_script_config envelope missing 'script_id' for "
                    "input %r; falling back to caller input. This indicates "
                    "a rest_client contract violation.",
                    script_id,
                )
                canonical_id = script_id

            return {
                "success": True,
                "action": "get",
                "script_id": canonical_id,
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
                    "Use ha_get_skill_guide for help",
                ],
            )

    async def _list_script_entity_ids(self) -> list[str]:
        """Best-effort list of bare script IDs (up to 10) from the entity registry.

        Returns the bare storage keys (e.g. ``morning_routine``), stripping
        the ``script.`` entity_id prefix — ``ha_config_get_script`` /
        ``ha_config_set_script`` / ``ha_config_remove_script`` all take the
        bare form, so the entity_id prefix would force callers to strip it
        before retry. Returns an empty list on any failure — caller treats
        absence as "no IDs to report" rather than failing the structured
        error raise. The 10-entry cap lives here (not at the callers) so a
        new call site can't accidentally bloat the error payload.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
        except Exception as e:
            logger.debug("Failed to list script entity_ids from registry: %s", e)
            return []
        entries = result.get("result", []) if isinstance(result, dict) else result
        if not isinstance(entries, list):
            return []
        return [
            entry["entity_id"][len("script.") :]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("entity_id"), str)
            and entry["entity_id"].startswith("script.")
        ][:10]

    async def _raise_script_not_found(self, script_id: str) -> None:
        """Raise a structured RESOURCE_NOT_FOUND ToolError for a missing script.

        Single source of truth for the 404→RESOURCE_NOT_FOUND mapping used
        by the GET path (``_fetch_script_config_envelope``) and the
        mutation paths (``ha_config_set_script`` update branch,
        ``ha_config_remove_script``). Populates ``available_script_ids``
        (up to 10 bare IDs) from the entity registry.
        """
        available_ids = await self._list_script_entity_ids()
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Script not found: {script_id}",
                context={
                    "script_id": script_id,
                    "available_script_ids": available_ids,
                },
                suggestions=[
                    "Use ha_search_entities(domain_filter='script') to find existing scripts"
                ],
            )
        )

    async def _fetch_script_config_envelope(self, script_id: str) -> dict[str, Any]:
        """Fetch the raw REST envelope, mapping 404 to RESOURCE_NOT_FOUND.

        Returns the dict envelope from ``rest_client.get_script_config``
        (``success``/``script_id``/``config`` keys). Raises a structured
        ``RESOURCE_NOT_FOUND`` ToolError via ``_raise_script_not_found`` on
        404. Other ``HomeAssistantAPIError`` instances propagate unchanged
        to caller exception handlers.
        """
        try:
            return cast(dict[str, Any], await self._client.get_script_config(script_id))
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                await self._raise_script_not_found(script_id)
            raise

    async def _get_script_config_internal(
        self, script_id: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch script config without logging or category injection.

        Returns (actual_config, config_hash) tuple where actual_config is
        the inner script body (not the REST wrapper).
        Used internally by _fetch_and_verify_hash and ha_config_get_script.

        404 responses from the REST client are mapped to a structured
        ``RESOURCE_NOT_FOUND`` ToolError via ``_fetch_script_config_envelope``.
        """
        config_result = await self._fetch_script_config_envelope(script_id)
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
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON,
                    f"Invalid config parameter: {e}",
                    context={
                        "script_id": script_id,
                        "provided_config_type": type(config).__name__,
                    },
                )
            )

        # Ensure config is a dict
        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "Config parameter must be a JSON object",
                    context={
                        "script_id": script_id,
                        "provided_type": type(parsed_config).__name__,
                    },
                )
            )

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
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "config must include either 'sequence' field (for regular scripts) or 'use_blueprint' field (for blueprint-based scripts)",
                    context={
                        "script_id": script_id,
                        "required_fields": ["sequence OR use_blueprint"],
                    },
                )
            )

        return config_dict, effective_category

    @tool(
        name="ha_config_set_script",
        tags={"Scripts"},
        annotations={
            "destructiveHint": True,
            "title": "Create or Update Script",
        },
    )
    @with_auto_backup(domain="script", id_param="script_id")
    @log_tool_usage
    async def ha_config_set_script(
        self,
        script_id: Annotated[
            str,
            Field(
                description="Script identifier — bare storage key ('morning_routine') or entity_id form ('script.morning_routine'); a leading 'script.' prefix is stripped before lookup."
            ),
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
        *,
        MandatoryBPS: Annotated[
            bool,
            Field(),
        ],
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant script.

        PREFER NATIVE ACTIONS OVER TEMPLATES (read this before writing any `{{ ... }}`):
        Native actions are validated at config load, fail loudly, and do not bypass HA's
        schema. Templates in logic positions fail silently and obscure intent.
        - `choose` / `if/then/else` instead of template-based service names
        - `wait_for_trigger` instead of `wait_template`
        - `repeat` with `for_each` instead of template loops
        - Hardcode `target.entity_id` literals — never `{{ this.entity_id }}`.
        Templates are appropriate ONLY in `data.*` fields, notification message/title,
        `event_data`, and `variables`. The reactive best-practice checker on this tool
        will surface anything in a logic position that should be native; consult the
        `best_practice_warnings` field on the response and fix before re-submitting.
        The relevant skill section is auto-embedded under `skill_content` on warnings,
        and the full `automation-patterns.md` + `template-guidelines.md` references
        ship under `skill_content` proactively by default. For comprehensive
        guidance beyond that, call `ha_get_skill_guide`.

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

        Note: Scripts use Home Assistant's action syntax. Check the documentation for advanced
        features like conditions, variables, parallel execution, and service call options.
        """
        bp_warnings: BestPracticeCheckResult = BestPracticeCheckResult()
        try:
            # Strip BEFORE validate so a bare ``"script."`` (empty after
            # strip) is rejected as ``VALIDATION_INVALID_PARAMETER`` rather
            # than slipping through validate (non-empty pre-strip) and
            # writing a phantom ``script.foo`` storage key — HA keys writes
            # by the literal ``script_id``, so passing ``"script.foo"``
            # unchanged makes the row invisible to a later
            # ``ha_config_get_script("foo")``. Behavioral parity with
            # ``ha_config_get_script`` so an agent that received an
            # entity_id (``script.foo``) from
            # ``ha_search_entities(domain_filter='script')`` can update it
            # without a manual prefix-strip step.
            script_id = script_id.removeprefix("script.")
            # ``script_id`` is required (always non-None). Reject empty/
            # whitespace up-front so the caller gets a structured parameter
            # error instead of a misleading ``RESOURCE_NOT_FOUND`` from
            # the downstream upsert/fetch. Extension of the #1312
            # validate_identifier_not_empty pattern to the scripts family
            # per #1313.
            validate_identifier_not_empty(
                script_id,
                "script_id",
                suggestions=[
                    "Pass a script identifier (e.g. 'morning_routine')",
                    "Use ha_search_entities(domain_filter='script') to list scripts",
                ],
                context={"action": "set"},
            )
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
                            context={
                                "action": "python_transform",
                                "script_id": script_id,
                            },
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
                    message, suggestions = format_sandbox_error(e, python_transform)
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            message,
                            suggestions=suggestions,
                            context={
                                "action": "python_transform",
                                "script_id": script_id,
                            },
                        )
                    )

                # Validate transformed config
                if (
                    "sequence" not in transformed_config
                    and "use_blueprint" not in transformed_config
                ):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.VALIDATION_FAILED,
                            "Transformed config must include either 'sequence' or 'use_blueprint'",
                            suggestions=[
                                "The transform may have removed required fields",
                                "Ensure the config still has a 'sequence' or 'use_blueprint' key",
                            ],
                            context={
                                "action": "python_transform",
                                "script_id": script_id,
                            },
                        )
                    )
                bp_warnings = _check_best_practices(transformed_config)

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
                    response["best_practice_warnings"] = list(bp_warnings)
                attach_skill_content(
                    response,
                    MandatoryBPS=MandatoryBPS,
                    canonical_files=_SCRIPT_SKILL_FILES,
                    referenced_files=bp_warnings.referenced_files,
                )
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
                config,
                script_id,
                category,
            )

            # Optional hash check for full config updates
            if config_hash:
                await self._fetch_and_verify_hash(script_id, config_hash, "set")

            # Pre-check for best-practice issues.
            bp_warnings = _check_best_practices(config_dict)

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
                    registered = await wait_for_entity_registered(
                        self._client, entity_id
                    )
                    if not registered:
                        result.setdefault("warnings", []).append(
                            f"Script saved but {entity_id} not yet queryable. "
                            "It may take a moment to become available."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                    result.setdefault("warnings", []).append(
                        f"Script saved but verification failed: {e}"
                    )

            # Apply category to entity registry if provided
            if effective_category and entity_id:
                await apply_entity_category(
                    self._client,
                    entity_id,
                    effective_category,
                    "script",
                    result,
                    "script",
                )

            if bp_warnings:
                result["best_practice_warnings"] = list(bp_warnings)

            merge_validation_meta(result, validation_meta)

            attach_skill_content(
                result,
                MandatoryBPS=MandatoryBPS,
                canonical_files=_SCRIPT_SKILL_FILES,
                referenced_files=bp_warnings.referenced_files,
            )

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
                "Use ha_get_skill_guide for help",
            ]
            if bp_warnings:
                suggestions.append(
                    "Config had best-practice issues that may be related: "
                    + "; ".join(bp_warnings)
                )
            # 404 during update only — the create path raises on its own when
            # the upsert hits an unknown identifier server-side. The bare
            # script_id form is what callers pass and what the registry stores.
            if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
                await self._raise_script_not_found(script_id)
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
    @with_auto_backup(domain="script", id_param="script_id")
    @log_tool_usage
    async def ha_config_remove_script(
        self,
        script_id: Annotated[
            str,
            Field(
                description="Script identifier to delete — bare storage key ('old_script') or entity_id form ('script.old_script'); a leading 'script.' prefix is stripped before lookup."
            ),
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
            # Strip BEFORE validate so a bare ``"script."`` (empty after
            # strip) is rejected as ``VALIDATION_INVALID_PARAMETER`` rather
            # than slipping through validate (non-empty pre-strip) and
            # producing a ``script.script.foo`` entity_id for the
            # ``wait_for_entity_removed`` watcher below — that mis-formed
            # entity_id never registers so the watcher times out on a
            # phantom. Behavioral parity with ``ha_config_get_script``.
            script_id = script_id.removeprefix("script.")
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                script_id,
                "script_id",
                suggestions=[
                    "Use ha_search_entities(domain_filter='script') to find existing script_ids"
                ],
                context={"operation": "remove_script"},
            )
            result = await self._client.delete_script_config(script_id)

            # Wait for script to be removed
            wait_bool = coerce_bool_param(wait, "wait", default=True)
            entity_id = f"script.{script_id}"
            if wait_bool:
                try:
                    removed = await wait_for_entity_removed(self._client, entity_id)
                    if not removed:
                        result.setdefault("warnings", []).append(
                            f"Deletion confirmed by API but {entity_id} may still appear briefly."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                    result.setdefault("warnings", []).append(
                        f"Deletion confirmed but removal verification failed: {e}"
                    )

            return {"success": True, "action": "delete", **result}
        except ToolError:
            raise
        except Exception as e:
            if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
                await self._raise_script_not_found(script_id)
            exception_to_structured_error(
                e,
                context={"script_id": script_id},
                suggestions=[
                    "Verify script_id exists using ha_search_entities(domain_filter='script')",
                    "Check if script is being used by automations",
                    "Use ha_get_skill_guide for help",
                ],
            )


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""
    register_tool_methods(mcp, ConfigScriptTools(client))
