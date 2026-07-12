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

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import (
    ErrorCode,
    create_config_error,
    create_error_response,
    create_validation_error,
)
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
)
from .auto_backup import automation_backup_target, with_auto_backup
from .best_practice_checker import (
    BestPracticeCheckResult,
)
from .best_practice_checker import (
    check_automation_config as _check_best_practices,
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
    JSON_STRING_COERCION,
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    coerce_to_list,
    fetch_entity_category,
    merge_validation_meta,
    parse_json_param,
    wait_for_entity_registered,
    wait_for_entity_removed,
)

logger = logging.getLogger(__name__)


# Skill files attached to ha_config_set_automation responses when
# MandatoryBPS=True (default), plus auto-attached on best-practice
# warning hits regardless of MandatoryBPS. Paths are relative to the
# home-assistant-best-practices skill directory.
_AUTOMATION_SKILL_FILES: tuple[str, ...] = (
    "references/automation-patterns.md",
    "references/template-guidelines.md",
)


# Distinctive prefix of the soft-failure warning emitted by
# ``ha_config_set_automation`` when ``_poll_for_automation_entity``
# exhausts its budget (``_POLL_BUDGET_S``) without resolving the new
# automation's entity_id. Exported so future tests can detect a missed
# registration without hard-coding the literal — rewording the warning
# becomes a compile-time coupling rather than a silent test drift.
NOT_VERIFIED_WARNING_PREFIX = (
    "Automation was submitted to Home Assistant but the entity was not found"
)


def _normalize_automation_config(config: Any, is_root: bool = True) -> Any:
    """
    Recursively normalize automation config field names to HA's canonical form.

    Home Assistant's 2024.10+ canonical form uses the plural root list keys
    ('triggers', 'actions', 'conditions'); the singular forms ('trigger',
    'action', 'condition') remain fully accepted as silent aliases. This tool
    canonicalizes to the plural root forms so the config-API round-trip and the
    downstream validators / best-practice checker all see one stable, modern
    shape (HA accepts whichever we send).

    Only the ROOT list keys are pluralized. The singular keys that act as type
    discriminators or service calls inside trigger/condition/action items
    ('trigger:' = trigger type, 'condition:' = condition type, 'action:' =
    service call) are semantically different and are left untouched, as is the
    singular 'sequence' key inside choose/if options and scripts. The nested
    'conditions' lists required inside choose/if and compound (or/and/not)
    blocks are already plural and pass through unchanged (issue #498: never
    rewrite these deeper keys).

    Args:
        config: Automation configuration (dict, list, or primitive)
        is_root: Whether this is the root-level automation config dict. Only the
                 root level gets the singular -> plural list-key normalization.

    Returns:
        Normalized configuration with plural list field names at the root level.
    """
    # Handle lists - recursively process each item
    if isinstance(config, list):
        return [_normalize_automation_config(item, is_root=False) for item in config]

    # Handle primitives (strings, numbers, etc.)
    if not isinstance(config, dict):
        return config

    # Process dictionary
    normalized = config.copy()

    # Build field mappings (source alias -> canonical key).
    field_mappings: dict[str, str] = {}

    # Pluralize the root list keys to HA's 2024.10+ canonical form. ONLY at the
    # root level: deeper in the tree 'trigger'/'action'/'condition' are type
    # discriminators / service calls, not list keys, and must not be touched
    # (e.g., 'action' inside a delay object -- see issue #498).
    if is_root:
        field_mappings["trigger"] = "triggers"
        field_mappings["action"] = "actions"
        field_mappings["condition"] = "conditions"

    # 'sequences' -> 'sequence': the canonical key is singular at any level.
    field_mappings["sequences"] = "sequence"

    # Apply field mapping to current level, preferring the canonical key.
    for src, dst in field_mappings.items():
        if src in normalized and dst not in normalized:
            normalized[dst] = normalized.pop(src)
        elif src in normalized and dst in normalized:
            # Both present -- prefer the canonical key, drop the alias.
            del normalized[src]

    # Recursively process all values in the dictionary
    for key, value in normalized.items():
        normalized[key] = _normalize_automation_config(value, is_root=False)

    return normalized


def _normalize_trigger_keys(triggers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Normalize trigger objects for round-trip compatibility.

    Older Home Assistant configs (and some integrations) still emit triggers
    keyed by the legacy 'platform'. This tool canonicalizes each trigger to the
    modern 'trigger' key (HA 2024.10+) so its pipeline and round-trip output use
    one stable, current shape; HA accepts either form on the SET side.

    Args:
        triggers: List of trigger configuration dicts

    Returns:
        List of triggers with 'trigger' key instead of 'platform' key
    """
    normalized_triggers = []
    for trigger in triggers:
        # Defensive: a malformed (e.g. LLM-generated) item may not be a dict.
        if not isinstance(trigger, dict):
            normalized_triggers.append(trigger)
            continue
        normalized_trigger = trigger.copy()
        # Convert legacy 'platform' to modern 'trigger'. If both are present,
        # drop the legacy alias so HA's strict schema doesn't reject the config
        # with "extra keys not allowed" (mirrors _normalize_automation_config).
        if "platform" in normalized_trigger:
            if "trigger" not in normalized_trigger:
                normalized_trigger["trigger"] = normalized_trigger.pop("platform")
            else:
                del normalized_trigger["platform"]
        normalized_triggers.append(normalized_trigger)
    return normalized_triggers


def _scene_create_in_choose(action: dict[str, Any]) -> bool:
    """True if any ``choose`` option's sequence contains scene.create."""
    opts = action.get("choose")
    if not isinstance(opts, list):
        return False
    for opt in opts:
        if isinstance(opt, dict) and isinstance(opt.get("sequence"), list):
            for sub in opt["sequence"]:
                if _action_contains_scene_create(sub):
                    return True
    return False


def _action_contains_scene_create(action: Any) -> bool:
    """True if the action — or any nested action under HA's wrapper keys —
    invokes ``scene.create``.

    Walks the standard wrappers: ``sequence``, ``parallel``, ``choose``
    (with options' inner ``sequence``), ``default``, and the ``then``/
    ``else`` siblings of ``if``. Returns True on the first hit, so a deep
    misroute is caught before the upsert reaches HA.
    """
    if not isinstance(action, dict):
        return False
    # 'service:' is the legacy key, 'action:' the modern HA service-call
    # key (HA 2024.8+). Both reach scene.create.
    if "scene.create" in (action.get("service"), action.get("action")):
        return True
    # Wrappers whose value is a list of nested actions.
    for nested_key in ("sequence", "parallel", "default", "then", "else"):
        nested = action.get(nested_key)
        if isinstance(nested, list):
            for sub in nested:
                if _action_contains_scene_create(sub):
                    return True
    return _scene_create_in_choose(action)


def _normalize_config_for_roundtrip(config: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize automation config from GET response for direct use in SET.

    This ensures a config retrieved via ha_config_get_automation can be
    directly passed to ha_config_set_automation without modification.

    Transformations:
    1. Field names: canonicalized to plural root keys (triggers/actions/conditions);
       a stray `sequences` key is normalized to `sequence`.
    2. Trigger keys: platform -> trigger (inside each trigger object)

    Args:
        config: Raw automation configuration from HA API

    Returns:
        Normalized configuration compatible with SET API
    """
    # First normalize field names (singular -> plural at the root level)
    normalized = _normalize_automation_config(config)

    # Then normalize trigger keys (legacy 'platform' -> modern 'trigger')
    if "triggers" in normalized and isinstance(normalized["triggers"], list):
        normalized["triggers"] = _normalize_trigger_keys(normalized["triggers"])

    return cast(dict[str, Any], normalized)


def _detect_conflicting_root_keys(config: Any) -> list[str]:
    """Warn when a config carries BOTH a singular alias and its canonical plural
    root key with *different* values.

    ``_normalize_automation_config`` keeps the canonical plural and silently drops
    the singular alias, so a caller that set, say, ``config['trigger']`` on a
    config that already has ``triggers`` would have that change discarded. The
    config is malformed (a caller should send one form), but surfacing the
    conflict beats dropping data silently.
    """
    if not isinstance(config, dict):
        return []
    warnings: list[str] = []
    for singular, plural in (
        ("trigger", "triggers"),
        ("action", "actions"),
        ("condition", "conditions"),
    ):
        if (
            singular in config
            and plural in config
            and config[singular] != config[plural]
        ):
            warnings.append(
                f"Config contains both '{singular}' and '{plural}' with different "
                f"values; using the canonical '{plural}' and ignoring '{singular}'."
            )
    return warnings


def _strip_redundant_identifier_echo(
    result: dict[str, Any],
    *,
    extra_excludes: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Strip the redundant ``identifier`` echo from an upsert/delete response.

    The canonical ``automation_id`` key (resolved entity_id, falling back to
    input identifier or ``unique_id``) makes re-echoing the raw ``identifier``
    redundant noise.

    ``unique_id`` is intentionally retained — it's HA's internal identifier,
    distinct from ``entity_id``/``automation_id``, and callers track it for
    cleanup. Do not extend ``extra_excludes`` to ``"unique_id"``: that
    regression broke E2E ``test_duplicate_automation_prevention`` at 5fe5338.

    ``extra_excludes`` lets a call site drop additional internal keys the
    spread shouldn't surface (e.g. ``"success"`` on the python_transform
    branch, where the caller manages that key directly).
    """
    excluded = {"identifier", *extra_excludes}
    return {k: v for k, v in result.items() if k not in excluded}


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
            logger.debug(
                f"Failed to resolve entity_id for automation {identifier}: {e}"
            )
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

        The returned `automation_id` is the resolved entity_id (canonical
        form, e.g. `automation.morning_routine`) when the registry lookup
        succeeds, falling back to the input `identifier` otherwise.

        EXAMPLES:
        - Get automation: ha_config_get_automation("automation.morning_routine")
        - Get by unique_id: ha_config_get_automation("my_unique_automation_id")

        For comprehensive automation documentation, use ha_get_skill_guide.
        """
        try:
            # Empty/whitespace identifier would propagate to the internal
            # config lookup and surface as a misleading
            # ``RESOURCE_NOT_FOUND``. Structured ``VALIDATION_INVALID_PARAMETER``
            # naming the parameter is cleaner — extension of the #1312
            # validate_identifier_not_empty pattern to the automations
            # family per #1313.
            validate_identifier_not_empty(
                identifier,
                "identifier",
                suggestions=[
                    "Pass an automation entity_id (e.g. 'automation.morning_routine')",
                    "Or pass the unique_id of an existing automation",
                    "Use ha_search(domain_filter='automation') to list automations",
                ],
            )

            # Automation gets ALWAYS take the legacy path — the component's
            # in-process ``config_get`` was withdrawn. It served
            # ``entity.raw_config``, which is only the storage body as of the
            # last COMPLETED async reload, with no version marker to tell a
            # fresh body from a stale one. A get racing a reload returned the
            # pre-edit body and broke the get -> python_transform -> set
            # round-trip (caught live by the automation python_transform e2e on
            # the arm/HAOS runners). The legacy REST config endpoint reads the
            # config FILE, which is fresh the instant a write lands, so it stays
            # the sole path. Scenes were already legacy-only (no storage body in
            # memory at all); automations join them here for freshness. A
            # file-reading ``config_get`` may return later (issue #1813).
            return await self._legacy_get_automation(identifier)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"identifier": identifier, "action": "get"},
                suggestions=[
                    "Verify automation exists using ha_search(domain_filter='automation')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_guide for help",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _legacy_get_automation(self, identifier: str) -> dict[str, Any]:
        """Assemble the automation-get response from the REST/WS pipeline.

        The multi-fetch path: per-id config REST + state-lookup entity_id
        resolution + ``fetch_entity_category`` WS call. This is the ONLY
        automation-get path — see ``ha_config_get_automation`` for why
        automation gets never route through the component's ``config_get``
        (its ``raw_config`` freshness lags the config file between a write and
        the next completed reload).
        """
        normalized_config, config_hash = await self._get_automation_config_internal(
            identifier
        )

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
            "automation_id": entity_id or identifier,
            "config": normalized_config,
            "config_hash": config_hash,
        }

    @tool(
        name="ha_config_set_automation",
        tags={"Automations"},
        annotations={
            "destructiveHint": True,
            "title": "Create or Update Automation",
        },
    )
    @with_auto_backup(domain="automation", id_fn=automation_backup_target)
    @log_tool_usage
    async def ha_config_set_automation(
        self,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Complete automation configuration with required fields: 'alias', 'triggers', 'actions'. "
                "Optional: 'description', 'conditions', 'mode', 'max', 'initial_state', 'variables'. "
                "Purpose-specific triggers/conditions (HA 2026.7+ default: 'trigger': '<domain>.<name>' "
                "with 'target'/'options') are valid config. "
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
                "Simple: python_transform=\"config['actions'][0]['data']['brightness'] = 255\" "
                "Pattern: python_transform=\"for a in config['actions']: "
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
            bool,
            Field(
                description="Wait for automation to be queryable before returning. Default: True. Set to False for bulk operations.",
                default=True,
            ),
        ] = True,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant automation. MUST call ha_get_skill_guide first.

        PREFER NATIVE SOLUTIONS OVER TEMPLATES (read this before writing any `{{ ... }}`):
        Native triggers/conditions/actions are validated at config load, fail loudly, and
        do not bypass HA's schema. Templates fail silently at runtime and obscure intent.
        - `condition: numeric_state` instead of `{{ states('x') | float > N }}`
        - `condition: state` (with `state:` list) instead of `{{ is_state(...) }}` /
          `{{ states(x) in [...] }}`
        - `condition: time` instead of `{{ now().hour ... }}` or `{{ now().weekday() ... }}`
        - `condition: sun` instead of `{{ is_state('sun.sun', ...) }}`
        - Native `for:` field on `state`/`numeric_state` triggers and `state`
          conditions over `{{ now() - X.last_changed > timedelta(...) }}` duration math.
        - `wait_for_trigger` instead of `wait_template`
        - `choose` action instead of template-based service names
        - For one-shot date firing, use a `time` trigger plus `automation.turn_off` on a
          hardcoded entity_id — not `{{ now().date() ... }}`.
        - Hardcode `target.entity_id` literals — never `{{ this.entity_id }}`.
        Templates are appropriate ONLY in `data.*` fields, notification message/title,
        `event_data`, and `variables`. The reactive best-practice checker on this tool
        will surface anything in a logic position that should be native; consult the
        `best_practice_warnings` field on the response and fix before re-submitting.
        The relevant skill section is auto-embedded under `skill_content` on warnings,
        and the full `automation-patterns.md` + `template-guidelines.md` references
        ship under `skill_content` proactively by default. For comprehensive
        guidance beyond that, call `ha_get_skill_guide`.

        The returned `automation_id` is the resolved entity_id (canonical
        form, e.g. `automation.morning_routine`) when entity registration
        succeeds, falling back to the input `identifier` (update path) or
        the generated `unique_id` from the upsert response (fresh create
        when no identifier was passed).

        Before reaching for ``ha_config_set_automation``, consider whether a
        dedicated tool fits the use case better:

        - State snapshot of one or more entities (capture-then-replay,
          no trigger needed) -> ha_config_set_scene
        - State-derived value that recomputes when its inputs change
          (template sensor / binary sensor / number / select)
          -> ha_config_set_helper(helper_type='template')
        - Stateful counter / timer / schedule / boolean / etc.
          -> ha_config_set_helper(helper_type='counter' | 'timer' | ...)

        Supports two modes: full config replacement OR Python transformation.

        WHEN TO USE WHICH MODE:
        - python_transform: RECOMMENDED for edits to existing automations. Surgical updates.
        - config: Use for creating new automations or full restructures.

        IMPORTANT: python_transform requires 'identifier' and 'config_hash' from ha_config_get_automation().

        PYTHON TRANSFORM EXAMPLES (operate on the fetched config, which uses HA's
        canonical plural root keys 'triggers'/'actions'/'conditions'):
        - Update action: python_transform="config['actions'][0]['data']['brightness'] = 255"
        - Add trigger: python_transform="config['triggers'].append({'trigger': 'state', 'entity_id': 'binary_sensor.motion', 'to': 'on'})"
        - Remove last action: python_transform="config['actions'].pop()"

        Creates a new automation (if identifier omitted) or updates existing automation with provided configuration.

        AUTOMATION TYPES:

        1. Regular Automations - Define triggers and actions directly
        2. Blueprint Automations - Use pre-built templates with customizable inputs

        REQUIRED FIELDS (Regular Automations):
        - alias: Human-readable automation name
        - triggers: List of triggers (time, state, event, etc.)
        - actions: List of actions to execute

        REQUIRED FIELDS (Blueprint Automations):
        - alias: Human-readable automation name
        - use_blueprint: Blueprint configuration
          - path: Blueprint file path (e.g., "motion_light.yaml")
          - input: Dictionary of input values for the blueprint

        OPTIONAL CONFIG FIELDS (Regular Automations):
        - description: Detailed description of the user's intent (RECOMMENDED: helps safely modify implementation later)
        - category: Category ID for organization (use ha_config_get_category to list, ha_config_set_category to create)
        - conditions: Additional conditions that must be met
        - mode: 'single' (default), 'restart', 'queued', 'parallel'
        - max: Maximum concurrent executions (for queued/parallel modes)
        - initial_state: Whether automation starts enabled (true/false)
        - variables: Variables for use in automation

        BASIC EXAMPLES:

        Simple time-based automation:
        ha_config_set_automation(config={
            "alias": "Morning Lights",
            "description": "Turn on bedroom lights at 7 AM to help wake up",
            "triggers": [{"trigger": "time", "at": "07:00:00"}],
            "actions": [{"action": "light.turn_on", "target": {"area_id": "bedroom"}}]
        })

        Motion-activated lighting — `for:` on the off-transition replaces action-delay:
        ha_config_set_automation(config={
            "alias": "Motion Light",
            "triggers": [
                {"trigger": "state", "entity_id": "binary_sensor.motion", "to": "on", "id": "motion_on"},
                {"trigger": "state", "entity_id": "binary_sensor.motion", "to": "off",
                 "for": {"minutes": 5}, "id": "motion_off"}
            ],
            "actions": [
                {"choose": [
                    {"conditions": [
                        {"condition": "trigger", "id": "motion_on"},
                        {"condition": "sun", "after": "sunset"}
                    ],
                     "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.hallway"}}]},
                    {"conditions": [{"condition": "trigger", "id": "motion_off"}],
                     "sequence": [{"action": "light.turn_off", "target": {"entity_id": "light.hallway"}}]}
                ]}
            ]
        })

        Update existing automation:
        ha_config_set_automation(
            identifier="automation.morning_routine",
            config={
                "alias": "Updated Morning Routine",
                "triggers": [{"trigger": "time", "at": "06:30:00"}],
                "actions": [
                    {"action": "light.turn_on", "target": {"area_id": "bedroom"}},
                    {"action": "climate.set_temperature", "target": {"entity_id": "climate.bedroom"}, "data": {"temperature": 22}}
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

        TRIGGER TYPES: time, time_pattern, sun, state, numeric_state, event, device, zone, template, and more
        CONDITION TYPES: state, numeric_state, time, sun, template, device, zone, and more
        ACTION TYPES: action calls, delays, wait_for_trigger, wait_template, if/then/else, choose, repeat, parallel

        For comprehensive automation documentation with all trigger/condition/action types and advanced examples:
        - Use: ha_get_skill_guide
        - Or visit: https://www.home-assistant.io/docs/automation/

        TROUBLESHOOTING:
        - Use ha_get_state() to verify entity_ids exist
        - Use ha_search() to find correct entity_ids
        - IF you must use Jinja2 and have no native alternative, test it first with
          ha_eval_template() before embedding it in the automation config — catches
          syntax errors and unresolved entity_ids before they fail silently at runtime
        - Use ha_search(domain_filter='automation') to find existing automations
        """
        bp_warnings: BestPracticeCheckResult = BestPracticeCheckResult()
        try:
            # ``identifier`` is optional (omit → create new with generated
            # unique_id; pass → update existing). When provided, reject
            # empty/whitespace up-front so the caller gets a structured
            # parameter error instead of a misleading ``RESOURCE_NOT_FOUND``
            # from the downstream lookup. The ``not identifier`` check
            # further down the python_transform branch still handles the
            # explicit ``identifier is None`` case for that mode.
            if identifier is not None:
                validate_identifier_not_empty(
                    identifier,
                    "identifier",
                    suggestions=[
                        "Omit identifier to create a new automation",
                        "Or pass a valid automation entity_id / unique_id to update",
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
                            "python_transform: Python-based edits (recommended for existing automations)",
                        ],
                        context={"action": "set", "identifier": identifier},
                    )
                )

            if python_transform is not None:
                response, bp_warnings = await self._run_python_transform(
                    identifier,
                    config_hash,
                    python_transform,
                    category,
                    MandatoryBPS,
                )
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

            # Detect conflicting singular+plural root keys BEFORE normalization
            # drops the singular alias (surface rather than silently discard).
            conflict_warnings = _detect_conflicting_root_keys(config_dict)

            # Normalize field names to HA's canonical plural root keys
            # (trigger -> triggers, action -> actions, condition -> conditions).
            config_dict = _normalize_automation_config(config_dict)

            # Optional hash check for full config updates
            if identifier and config_hash:
                await self._fetch_and_verify_hash(identifier, config_hash, "set")

            self._validate_required_fields(config_dict, identifier)
            bp_warnings = _check_best_practices(config_dict)
            validation_meta = await validate_config_references(
                self._client, config_dict
            )

            return await self._run_config_update(
                config_dict,
                identifier,
                effective_category,
                wait,
                bp_warnings,
                validation_meta,
                MandatoryBPS,
                conflict_warnings,
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings) from None
        except Exception as e:
            # 404 during update only — create (identifier=None) never hits this branch.
            if (
                identifier
                and isinstance(e, HomeAssistantAPIError)
                and e.status_code == 404
            ):
                await self._raise_automation_not_found(identifier)
            error_text = str(e)
            suggestions = [
                "Check automation configuration format",
                "Ensure required fields: alias, triggers, actions",
                "Use entity_id format: automation.morning_routine or unique_id",
                "Use ha_search(domain_filter='automation') to find automations",
                "Use ha_get_skill_guide for automation examples",
            ]
            if isinstance(e, HomeAssistantAPIError):
                if "'service'" in error_text and "not allowed" in error_text:
                    suggestions.insert(
                        0,
                        "Use 'action:' not 'service:' for service calls in action steps "
                        "(renamed in HA 2024.8).",
                    )
                elif "unexpected keyword argument" in error_text.lower():
                    suggestions.insert(
                        0,
                        "An action step contains a field that belongs at the automation root "
                        "(e.g. alias, trigger, condition). Each action step should only contain "
                        "action/target/data/delay/choose/if/repeat/parallel keys.",
                    )
                elif "'variables'" in error_text and "dictionary" in error_text:
                    suggestions.insert(
                        0,
                        "variables must be a dict mapping names to values, "
                        'e.g. {"variables": {"my_var": 42}}',
                    )
            if bp_warnings:
                suggestions.append(
                    "Config had best-practice issues that may be related: "
                    + "; ".join(bp_warnings)
                )
            error = exception_to_structured_error(
                e,
                context={"identifier": identifier},
                suggestions=suggestions,
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings)
            raise_tool_error(error)

    async def _run_python_transform(
        self,
        identifier: str | None,
        config_hash: str | None,
        python_transform: str,
        category: str | None,
        MandatoryBPS: bool,
    ) -> tuple[dict[str, Any], BestPracticeCheckResult]:
        """Execute python_transform mode and return (response, bp_warnings)."""
        if not identifier:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "identifier is required for python_transform",
                    suggestions=[
                        "Provide the automation entity_id or unique_id",
                        "Use ha_search(domain_filter='automation') to find automations",
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

        current_config = await self._fetch_and_verify_hash(
            identifier, config_hash, "python_transform"
        )

        try:
            transformed_config = safe_execute(python_transform, current_config)
        except PythonSandboxError as e:
            message, suggestions = format_sandbox_error(e, python_transform)
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    message,
                    suggestions=suggestions,
                    context={"action": "python_transform", "identifier": identifier},
                )
            )

        # Pop category before sending to HA REST API (rejects unknown keys)
        transform_category = transformed_config.pop("category", None)
        effective_category = category if category is not None else transform_category

        # Detect conflicting singular+plural root keys (e.g. a transform that set
        # the singular 'trigger' on a fetched plural config) before normalization
        # drops the singular alias.
        conflict_warnings = _detect_conflicting_root_keys(transformed_config)

        transformed_config = _normalize_automation_config(transformed_config)
        self._validate_required_fields(transformed_config, identifier)
        bp_warnings = _check_best_practices(transformed_config)

        result = await self._client.upsert_automation_config(
            transformed_config, identifier
        )
        for warning in conflict_warnings:
            result.setdefault("warnings", []).append(warning)
        refetched = await self._get_automation_config_internal(identifier)
        new_config_hash = refetched[1]

        entity_id = result.get("entity_id")
        if not entity_id and identifier and identifier.startswith("automation."):
            entity_id = identifier
        if effective_category and entity_id:
            await apply_entity_category(
                self._client,
                entity_id,
                effective_category,
                "automation",
                result,
                "automation",
            )

        response: dict[str, Any] = {
            "success": True,
            "action": "python_transform",
            "automation_id": entity_id or identifier or result.get("unique_id"),
            "config_hash": new_config_hash,
            "python_expression": python_transform,
            "message": f"Automation {identifier} updated via Python transform",
            **_strip_redundant_identifier_echo(result, extra_excludes=("success",)),
        }
        if bp_warnings:
            response["best_practice_warnings"] = list(bp_warnings)
        attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_AUTOMATION_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response, bp_warnings

    async def _run_config_update(
        self,
        config_dict: dict[str, Any],
        identifier: str | None,
        effective_category: str | None,
        wait: bool,
        bp_warnings: BestPracticeCheckResult,
        validation_meta: dict[str, Any],
        MandatoryBPS: bool,
        conflict_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute config-replacement mode and return the tool response."""
        result = await self._client.upsert_automation_config(config_dict, identifier)

        for warning in conflict_warnings or []:
            result.setdefault("warnings", []).append(warning)

        if result.get("entity_not_verified"):
            result.setdefault("warnings", []).append(
                f"{NOT_VERIFIED_WARNING_PREFIX} "
                "after polling. The automation may still have been created -- check Home "
                "Assistant logs and try reloading automations. Common causes: "
                "automations.yaml vs automation.yaml filename mismatch, invalid config "
                "that HA accepted but failed to load, or slow hardware."
            )
            result.pop("entity_not_verified", None)

        entity_id = result.get("entity_id")
        if not entity_id and identifier and identifier.startswith("automation."):
            entity_id = identifier
        if wait and entity_id:
            action_word = "created" if identifier is None else "updated"
            try:
                registered = await wait_for_entity_registered(self._client, entity_id)
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Automation {action_word} but {entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                result.setdefault("warnings", []).append(
                    f"Automation {action_word} but verification failed: {e}"
                )

        if effective_category and entity_id:
            await apply_entity_category(
                self._client,
                entity_id,
                effective_category,
                "automation",
                result,
                "automation",
            )

        if bp_warnings:
            result["best_practice_warnings"] = list(bp_warnings)

        merge_validation_meta(result, validation_meta)

        automation_id = entity_id or identifier or result.get("unique_id")
        response = {
            "success": True,
            # automation_id omitted when all three fallbacks are falsy —
            # the create path is unguarded by validate_identifier_not_empty,
            # and surfacing automation_id=None would lie about resolvability.
            # Defensive: HA's upsert normally returns a usable id, so this
            # fallback rarely triggers.
            **({"automation_id": automation_id} if automation_id else {}),
            **_strip_redundant_identifier_echo(result),
        }
        # attach AFTER the outer dict is built so attach_skill_content's
        # reorder puts skill_content_hint at position 0 of the FINAL
        # response — building the outer dict via spread otherwise pushes
        # the hint to position 2-3 behind success/automation_id, which
        # is the exact position BAT showed small models can't find.
        attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_AUTOMATION_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response

    async def _list_automation_entity_ids(self) -> list[str]:
        """Best-effort list of automation entity_ids (up to 10) from the entity registry.

        Used to populate ``available_automation_ids`` in RESOURCE_NOT_FOUND
        error context. Returns an empty list on any failure — caller treats
        absence as "no IDs to report" rather than failing the structured
        error raise. The 10-entry cap lives here (not at the callers) so a
        new call site can't accidentally bloat the error payload.
        """
        try:
            result = await self._client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
        except Exception as e:
            logger.debug("Failed to list automation entity_ids from registry: %s", e)
            return []
        entries = result.get("result", []) if isinstance(result, dict) else result
        if not isinstance(entries, list):
            return []
        return [
            entry["entity_id"]
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("entity_id"), str)
            and entry["entity_id"].startswith("automation.")
        ][:10]

    async def _raise_automation_not_found(self, identifier: str) -> None:
        """Raise a structured RESOURCE_NOT_FOUND ToolError for a missing automation.

        Single source of truth for the 404→RESOURCE_NOT_FOUND mapping used
        by both the GET path (``_get_automation_config_internal``) and the
        mutation paths (``ha_config_set_automation`` update branch,
        ``ha_config_remove_automation``). Populates
        ``available_automation_ids`` (up to 10) from the entity registry.
        """
        available_ids = await self._list_automation_entity_ids()
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Automation not found: {identifier}",
                context={
                    "automation_id": identifier,
                    "available_automation_ids": available_ids,
                },
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
            )
        )

    async def _get_automation_config_internal(
        self, identifier: str
    ) -> tuple[dict[str, Any], str]:
        """Fetch and normalize automation config without logging or category injection.

        Returns (normalized_config, config_hash) tuple.
        Used internally by _fetch_and_verify_hash and ha_config_get_automation.

        Raises a structured ``RESOURCE_NOT_FOUND`` ToolError via
        ``_raise_automation_not_found`` when the REST client returns 404.
        Other ``HomeAssistantAPIError`` instances propagate unchanged to
        caller exception handlers (``exception_to_structured_error`` route).
        """
        try:
            config_result = await self._client.get_automation_config(identifier)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                await self._raise_automation_not_found(identifier)
            raise
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
        current_config, current_hash = await self._get_automation_config_internal(
            identifier
        )
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
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_JSON,
                    message=f"Invalid config parameter: {e}",
                    suggestions=[
                        "Pass 'config' as a dict, not a JSON string, to avoid escaping issues.",
                        "Check for JSON syntax errors: unquoted keys, trailing commas, or invalid escape sequences.",
                    ],
                    context={"parameter": "config"},
                )
            )

        if parsed_config is None or not isinstance(parsed_config, dict):
            raise_tool_error(
                create_validation_error(
                    "Config parameter must be a JSON object",
                    parameter="config",
                    details=f"Received type: {type(parsed_config).__name__}",
                )
            )

        return cast(dict[str, Any], parsed_config)

    @staticmethod
    def _check_scene_create_misroute(
        config_dict: dict[str, Any], identifier: str | None
    ) -> None:
        """Raise if an empty-trigger config wraps scene.create (common model misroute)."""
        trigger_value = config_dict.get("triggers")
        trigger_empty = trigger_value is None or (
            isinstance(trigger_value, list) and not trigger_value
        )
        if not trigger_empty:
            return
        actions_list = coerce_to_list(config_dict.get("actions"))
        scene_create_indices = [
            i for i, a in enumerate(actions_list) if _action_contains_scene_create(a)
        ]
        if scene_create_indices:
            raise_tool_error(
                create_error_response(
                    code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                    message=(
                        "Empty trigger paired with a scene.create action — "
                        "this automation can never fire. For a state snapshot "
                        "of one or more entities, use ha_config_set_scene "
                        "directly instead of wrapping scene.create in an "
                        "automation."
                    ),
                    suggestions=[
                        "ha_config_set_scene(scene_id='...', config={'name': '...', 'entities': {'<entity_id>': {...}}}) creates a scene without a trigger.",
                        "If the snapshot really should be the result of an event, add the trigger that should fire it and keep the automation.",
                        "For a state-derived value that recomputes when its inputs change, use ha_config_set_helper(helper_type='template') instead.",
                    ],
                    context={
                        "scene_create_action_indices": scene_create_indices,
                        "identifier": identifier,
                    },
                )
            )

    @staticmethod
    def _validate_condition_platform(config_dict: dict[str, Any]) -> None:
        """Raise if any condition uses 'platform' (trigger syntax) instead of 'condition'."""
        for idx, cond in enumerate(coerce_to_list(config_dict.get("conditions"))):
            if not isinstance(cond, dict):
                continue
            if "platform" in cond and "condition" not in cond:
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.VALIDATION_INVALID_PARAMETER,
                        message=(
                            f"Condition at index {idx} uses 'platform' (trigger syntax). "
                            "Conditions use 'condition', not 'platform'."
                        ),
                        suggestions=[
                            f"Replace 'platform' with 'condition': "
                            f"{{'condition': '{cond['platform']}', ...}}",
                            "Triggers use 'trigger'; conditions use 'condition'.",
                        ],
                        context={"condition_index": idx, "found_key": "platform"},
                    )
                )

    @staticmethod
    def _validate_required_fields(
        config_dict: dict[str, Any], identifier: str | None
    ) -> None:
        """Validate required fields and prevent duplicate creation."""
        if "use_blueprint" in config_dict:
            required_fields = ["alias"]
            # Strip empty triggers/actions/conditions arrays that would override blueprint
            for field in ["triggers", "actions", "conditions"]:
                if field in config_dict and config_dict[field] == []:
                    del config_dict[field]
        else:
            required_fields = ["alias", "triggers", "actions"]

        missing_fields = [f for f in required_fields if f not in config_dict]
        if missing_fields:
            # If the caller supplied a 'sequence' key, the config looks like a
            # script — point them at ha_config_set_script instead of the generic
            # missing-fields error.
            if "sequence" in config_dict and (
                "triggers" in missing_fields or "actions" in missing_fields
            ):
                context: dict[str, Any] = {"missing_fields": missing_fields}
                if identifier:
                    context["identifier"] = identifier
                raise_tool_error(
                    create_error_response(
                        code=ErrorCode.CONFIG_MISSING_REQUIRED_FIELDS,
                        message=f"Missing required fields: {', '.join(missing_fields)}",
                        details=(
                            "Config contains 'sequence', which belongs to scripts. "
                            "Automations use 'triggers' and 'actions'; scripts use 'sequence'."
                        ),
                        suggestions=[
                            "Did you mean ha_config_set_script? Scripts use 'sequence' directly.",
                            "For an automation, replace 'sequence' with 'actions' and add 'triggers'.",
                        ],
                        context=context,
                    )
                )
            raise_tool_error(
                create_config_error(
                    f"Missing required fields: {', '.join(missing_fields)}",
                    identifier=identifier,
                    missing_fields=missing_fields,
                )
            )

        # Issue #1169: see _check_scene_create_misroute
        AutomationConfigTools._check_scene_create_misroute(config_dict, identifier)

        # HA accepts conditions with 'platform' (trigger syntax) but then crashes
        # with an unhelpful 500 rather than a 400 validation error.
        AutomationConfigTools._validate_condition_platform(config_dict)

        # Prevent duplicate creation when config contains an existing automation id
        if identifier is None and "id" in config_dict:
            existing_id = config_dict["id"]
            raise_tool_error(
                create_validation_error(
                    f"Config contains 'id' field ('{existing_id}') but no identifier was provided. "
                    "This would create a duplicate automation instead of updating the existing one.",
                    parameter="identifier",
                    details=f"To update, pass identifier='{existing_id}' (or the automation's entity_id). "
                    "To create a genuinely new automation, remove the 'id' field from the config.",
                )
            )

    @tool(
        name="ha_config_remove_automation",
        tags={"Automations"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Automation",
        },
    )
    @with_auto_backup(domain="automation", id_param="identifier")
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
            bool,
            Field(
                description="Wait for automation to be fully removed before returning. Default: True.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """
        Delete a Home Assistant automation.

        The returned `automation_id` is the resolved entity_id (canonical
        form, e.g. `automation.morning_routine`) when the registry lookup
        succeeded before the delete, falling back to the input
        `identifier` otherwise.

        EXAMPLES:
        - Delete automation: ha_config_remove_automation("automation.old_automation")
        - Delete by unique_id: ha_config_remove_automation("my_unique_id")

        **WARNING:** Deleting an automation removes it permanently from your Home Assistant configuration.
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                identifier,
                "identifier",
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
                context={"operation": "remove_automation"},
            )
            # Resolve entity_id for wait verification (identifier may be a unique_id)
            entity_id_for_wait = await self._resolve_automation_entity_id(identifier)
            if not entity_id_for_wait:
                logger.warning(
                    f"Could not resolve unique_id '{identifier}' to entity_id -- wait verification will be skipped"
                )

            result = await self._client.delete_automation_config(identifier)

            # Wait for entity to be removed
            if wait and entity_id_for_wait:
                try:
                    removed = await wait_for_entity_removed(
                        self._client, entity_id_for_wait
                    )
                    if not removed:
                        result.setdefault("warnings", []).append(
                            f"Deletion confirmed by API but {entity_id_for_wait} may still appear briefly."
                        )
                except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                    result.setdefault("warnings", []).append(
                        f"Deletion confirmed but removal verification failed: {e}"
                    )

            return {
                "success": True,
                "action": "delete",
                "automation_id": (
                    entity_id_for_wait or identifier or result.get("unique_id")
                ),
                **_strip_redundant_identifier_echo(result),
            }
        except ToolError:
            raise
        except Exception as e:
            if isinstance(e, HomeAssistantAPIError) and e.status_code == 404:
                await self._raise_automation_not_found(identifier)
            exception_to_structured_error(
                e,
                context={"identifier": identifier, "action": "delete"},
                suggestions=[
                    "Verify automation exists using ha_search(domain_filter='automation')",
                    "Use entity_id format: automation.morning_routine or unique_id",
                    "Check Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_config_automation_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant automation configuration tools."""
    register_tool_methods(mcp, AutomationConfigTools(client))
