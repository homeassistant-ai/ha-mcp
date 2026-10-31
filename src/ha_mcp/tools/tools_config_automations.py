"""
Configuration management tools for Home Assistant automations.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant automation configurations. Core validates and stores each
config as written. The component's ``automation_config`` bridge
(``component_automation``) validates before a write and reads the registry
side; Core's REST and WebSocket API is the fallback without it.
"""

import logging
from typing import Annotated, Any, NamedTuple, NoReturn, cast

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ..errors import (
    ErrorCode,
    create_error_response,
    create_validation_error,
)
from ..strict_bps import BestPracticeKeyParam
from ..utils.config_hash import compute_config_hash
from ..utils.python_sandbox import (
    PythonSandboxError,
    format_sandbox_error,
    get_security_documentation,
    safe_execute,
)
from .auto_backup import automation_backup_target, with_auto_backup
from .automation_errors import (
    core_rejection_text,
    failed_section_from_core,
    raise_core_rejection,
    section_from_status,
)
from .best_practice_checker import (
    BestPracticeCheckResult,
)
from .best_practice_checker import (
    check_automation_config as _check_best_practices,
)
from .blueprint_substitute import (
    TakenControl,
    take_control_config,
    validate_write_modes,
)
from .coercion import JSON_STRING_COERCION, coerce_to_list, parse_json_param
from .component_automation import automation_lookup, automation_validate
from .config_helpers.registry import validate_registry_ids
from .config_write_errors import (
    reject_enabled_in_config,
    reject_sequence_misroute,
)
from .config_write_helpers import (
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
    config_reload_waiter,
    entry_category,
    fetch_registry_entry,
    merge_validation_meta,
    note_reload_outcome,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    reject_malformed_list_fields,
    validate_identifier_not_empty,
)
from .reference_validator import validate_config_references
from .tool_hints import read_only_hints, write_hints
from .util_helpers import list_visible_registry_ids
from .ws_waiters import (
    verify_entity_removed,
    wait_for_automation_entity_by_unique_id,
    wait_for_entity_registered,
    wait_for_state_change,
)

logger = logging.getLogger(__name__)


# Skill files attached to ha_config_set_automation responses when
# MandatoryBPS=True (default), plus auto-attached on best-practice
# warning hits regardless of MandatoryBPS. Paths are relative to the
# home-assistant-best-practices skill directory.
_AUTOMATION_SKILL_FILES: tuple[str, ...] = (
    "references/triggers-and-conditions.md",
    "references/automation-actions.md",
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

# Both root forms Core accepts for the three lists; the config is read as the
# caller wrote it.
_ROOT_LIST_KEYS = (
    "triggers",
    "trigger",
    "conditions",
    "condition",
    "actions",
    "action",
)

_REFERENCES_NEED_COMPONENT = (
    "references omitted: they are read through the ha_mcp_tools component, "
    "which did not answer"
)


class _Target(NamedTuple):
    """An automation resolved for a read or a write."""

    config_id: str
    entity_id: str | None
    # The component's lookup reply; None on the fallback path.
    lookup: dict[str, Any] | None


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


def _validate_automation_entity_id(entity_id: str | None) -> None:
    """Validate an optional automation entity_id before dispatch."""
    if entity_id is not None:
        validate_identifier_not_empty(
            entity_id,
            "entity_id",
            suggestions=[
                "Omit entity_id to create a new automation",
                "Or pass an automation entity_id or config id to update",
            ],
            context={"action": "set"},
        )


def _skip_automation_runtime_backup(kwargs: dict[str, Any]) -> bool:
    """Skip config snapshots for runtime-only calls (enabled / run_actions)."""
    return (
        (kwargs.get("enabled") is not None or bool(kwargs.get("run_actions")))
        and kwargs.get("config") is None
        and kwargs.get("python_transform") is None
        and not kwargs.get("take_control_of_blueprint")
    )


async def _resolve_post_write_automation_entity(
    client: Any,
    config_id: str | None,
    entity_id: str | None,
    wait: bool,
    response: dict[str, Any],
) -> str | None:
    """Resolve a config id's entity after a write when registration may lag."""
    if wait and not entity_id and config_id and not config_id.startswith("automation."):
        try:
            return await wait_for_automation_entity_by_unique_id(client, config_id)
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
            response.setdefault("warnings", []).append(
                f"Automation registration verification failed: {e}"
            )
    return entity_id


async def _resolve_runtime_target(
    client: Any, target: str, response: dict[str, Any]
) -> str | None:
    """Resolve the entity to receive a runtime change (enabled / run_actions)."""
    if target.startswith("automation."):
        return target
    # A config write reloads the automation and replaces its entity when the
    # config changed (or creates it), so poll even when the caller passed
    # wait=False.
    return await _resolve_post_write_automation_entity(
        client, target, None, True, response
    )


def _sync_post_write_automation_result(
    response: dict[str, Any], entity_id: str | None
) -> None:
    """Record a successfully resolved entity and clear stale poll state."""
    if entity_id:
        response["entity_id"] = entity_id
        response.pop("entity_not_verified", None)


def _run_actions_once_reloaded(
    result: dict[str, Any],
    reloaded: bool | None,
    run_actions: bool,
    target: str | None,
) -> bool:
    """Keep run_actions after a write only once the reload is confirmed.

    Unlike ``enabled``, a run cannot be corrected afterwards: before the reload
    it would run the previous actions, or hit no entity at all.
    """
    if not run_actions or reloaded:
        return run_actions
    result["actions_triggered"] = False
    result.setdefault("warnings", []).append(
        "Automation was written, but its actions were not run: the reload the "
        "write scheduled could not be confirmed, so they could have run the "
        "previous version. Run them with ha_config_set_automation("
        f"entity_id='{target or '<automation id>'}', run_actions=True)."
    )
    return False


# Standalone runtime calls raise on a service failure; after a config write the
# same failure is a partial-success warning because the write already landed.
_STANDALONE_RUNTIME_ACTIONS = ("set_enabled", "run_actions")


async def _set_automation_enabled(client: Any, entity_id: str, enabled: bool) -> Any:
    """Set an automation's runtime enabled state through Home Assistant."""
    service = "turn_on" if enabled else "turn_off"
    return await client.call_service("automation", service, {"entity_id": entity_id})


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

    async def _entity_id_from_states(self, config_id: str) -> str | None:
        """The automation whose state carries ``config_id``; component-absent path."""
        try:
            states = await self._client.get_states()
        except HomeAssistantError as e:
            logger.debug(f"Failed to resolve entity_id for automation {config_id}: {e}")
            return None
        for state in states:
            if (
                state.get("entity_id", "").startswith("automation.")
                and state.get("attributes", {}).get("id") == config_id
            ):
                return str(state["entity_id"])
        return None

    async def _resolve_target(
        self, entity_id: str, *, with_entity: bool = True
    ) -> _Target:
        """Resolve ``entity_id`` (an entity_id or a config id) to its config id.

        An entity_id resolves through the registry (the component's lookup,
        else the REST client's registry-then-state resolver), so a disabled
        automation resolves too; one nothing knows is RESOURCE_NOT_FOUND, never
        a creation. A config id is used as given; ``with_entity`` also looks up
        its entity.
        """
        if entity_id.startswith("automation."):
            lookup = await automation_lookup(self._client, entity_id=entity_id)
            if lookup is None:
                try:
                    config_id = await self._client._resolve_automation_id(entity_id)
                except HomeAssistantAPIError as e:
                    if e.status_code == 404:
                        await self._raise_automation_not_found(entity_id)
                    raise
                return _Target(config_id, entity_id, None)
            unique_id = (lookup.get("entity") or {}).get("unique_id")
            if not lookup["found"] or not unique_id:
                await self._raise_automation_not_found(entity_id)
            return _Target(str(unique_id), entity_id, lookup)
        if not with_entity:
            return _Target(entity_id, None, None)
        lookup = await automation_lookup(self._client, unique_id=entity_id)
        if lookup is None:
            return _Target(
                entity_id, await self._entity_id_from_states(entity_id), None
            )
        row = lookup.get("entity") if lookup["found"] else None
        return _Target(entity_id, (row or {}).get("entity_id"), lookup)

    async def _read_stored(self, config_id: str) -> dict[str, Any] | None:
        """The body Core stores under ``config_id``, verbatim; None when absent."""
        try:
            stored: dict[str, Any] = await self._client.get_automation_config(config_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                return None
            raise
        return stored

    async def _registry_details(
        self, target: _Target, warnings: list[str]
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        """The automation's registry row, Core's validation verdict and references."""
        if target.lookup is not None:
            return (
                target.lookup.get("entity"),
                target.lookup.get("validation"),
                target.lookup.get("references"),
            )
        warnings.append(_REFERENCES_NEED_COMPONENT)
        if target.entity_id is None:
            return None, None, None
        entity = await fetch_registry_entry(self._client, target.entity_id, warnings)
        return entity, await self._repair_validation(target.entity_id, warnings), None

    async def _repair_validation(
        self, entity_id: str, warnings: list[str]
    ) -> dict[str, Any] | None:
        """Core's validation verdict from the repair issue it raises for a failed load."""
        try:
            result = await self._client.send_websocket_message(
                {"type": "repairs/list_issues"}
            )
        except HomeAssistantError as e:
            warnings.append(f"validation status unavailable: {e}")
            return None
        if not result.get("success"):
            reason = result.get("error") or "request failed"
            warnings.append(f"validation status unavailable: {reason}")
            return None
        prefix = f"{entity_id}_validation_"
        for issue in (result.get("result") or {}).get("issues") or []:
            issue_id = issue.get("issue_id")
            if (
                issue.get("domain") == "automation"
                and isinstance(issue_id, str)
                and issue_id.startswith(prefix)
            ):
                placeholders = issue.get("translation_placeholders") or {}
                return {
                    "status": issue_id.removeprefix(prefix),
                    "error": placeholders.get("error"),
                }
        return None

    @tool(
        name="ha_config_get_automation",
        tags={"Automations"},
        annotations=read_only_hints("Get Automation Config", open_world=False),
    )
    @log_tool_usage
    async def ha_config_get_automation(
        self,
        entity_id: Annotated[
            str,
            Field(
                description="The automation's entity_id (e.g. 'automation.morning_routine') "
                "or its config id"
            ),
        ],
    ) -> dict[str, Any]:
        """
        Get a Home Assistant automation's stored configuration.

        `config` is the body Home Assistant stores, as it stores it; its
        `config_hash` stays the same across reads of an unchanged automation.
        `entity` is the automation's entity registry row, `validation` holds
        Home Assistant's status and error when the automation failed to load
        (None when it loaded), and `references` lists the entities, devices,
        areas, floors, labels and blueprint it uses (None without the
        ha_mcp_tools component). `automation_id` is the resolved entity_id,
        falling back to the input.

        EXAMPLES:
        - Get automation: ha_config_get_automation("automation.morning_routine")
        - Get by config id: ha_config_get_automation("1712345678901")

        For comprehensive automation documentation, use ha_get_skill_guide.
        """
        try:
            # Empty/whitespace would surface as a misleading RESOURCE_NOT_FOUND
            # from the lookup (#1313).
            validate_identifier_not_empty(
                entity_id,
                "entity_id",
                suggestions=[
                    "Pass an automation entity_id (e.g. 'automation.morning_routine')",
                    "Or pass the config id of an existing automation",
                    "Use ha_search(domain_filter='automation') to list automations",
                ],
            )
            # The body always comes from Core's REST config endpoint, which
            # reads the file under its mutation lock; the in-memory raw_config
            # lags it until the reload after a write completes.
            config, config_hash, target = await self._get_automation_config_internal(
                entity_id
            )
            warnings: list[str] = []
            entity, validation, references = await self._registry_details(
                target, warnings
            )
            # Injected after hashing, for the set round-trip.
            cat_id = entry_category(entity, "automation")
            if cat_id:
                config["category"] = cat_id
            response: dict[str, Any] = {
                "success": True,
                "action": "get",
                "automation_id": target.entity_id or entity_id,
                "config": config,
                "config_hash": config_hash,
                "entity": entity,
                "validation": validation,
                "references": references,
            }
            if warnings:
                response["warnings"] = warnings
            return response
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"automation_id": entity_id, "action": "get"},
                suggestions=[
                    "Verify automation exists using ha_search(domain_filter='automation')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_guide for help",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_config_set_automation",
        tags={"Automations"},
        annotations=write_hints(
            "Create or Update Automation",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @with_auto_backup(
        domain="automation",
        id_fn=automation_backup_target,
        skip_fn=_skip_automation_runtime_backup,
    )
    @log_tool_usage
    async def ha_config_set_automation(
        self,
        config: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Complete automation configuration, written as given. "
                "Purpose-specific triggers/conditions (HA 2026.7+ default: 'trigger': '<domain>.<name>' "
                "with 'target'/'options') are valid config. "
                "Mutually exclusive with python_transform.",
                default=None,
            ),
        ] = None,
        entity_id: Annotated[
            str | None,
            Field(
                description="The automation's entity_id (automation.x) or its config id; "
                "omit to create. An unused config id creates the automation under that id; "
                "values such as 'new' are literal ids, not placeholders.",
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing automation config. "
                "Mutually exclusive with config. "
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
                "Required for python_transform and when a config update changes an "
                "existing automation's alias; otherwise optional for config updates "
                "(validates before full replacement if provided).",
            ),
        ] = None,
        take_control_of_blueprint: Annotated[
            bool,
            Field(
                description="Convert a blueprint-backed automation into an editable "
                'standalone one -- the UI\'s "Take control". Renders the blueprint '
                "with its current inputs and saves the result over the same "
                "automation, which keeps its entity_id, alias and description and "
                "then has its own triggers/conditions/actions and no "
                "'use_blueprint'. Requires entity_id; mutually exclusive with "
                "config and python_transform.",
                default=False,
            ),
        ] = False,
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
                description="Wait for automation to be queryable before returning. Set to False for"
                " bulk operations.",
                default=True,
            ),
        ] = True,
        enabled: Annotated[
            bool | None,
            Field(
                description=(
                    "Turn the automation on (True) or off (False) after an optional config "
                    "update; None leaves it unchanged. Not written into the stored config: Home Assistant keeps "
                    "the state across restarts, but a config 'initial_state' overrides it "
                    "whenever the automation is reloaded or HA starts."
                ),
                default=None,
            ),
        ] = None,
        run_actions: Annotated[
            bool,
            Field(
                description=(
                    "Run the automation's actions now, skipping its triggers and "
                    "conditions (automation.trigger, the UI's Run actions). Applied "
                    "after `enabled` and after any config write. Can be used "
                    "standalone with entity_id and no config."
                ),
                default=False,
            ),
        ] = False,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant automation.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.

        Prefer native triggers/conditions/actions over templates in logic
        positions; templates belong only in `data.*`, notification text,
        `event_data` and `variables`. The best-practice checker reports
        violations under `best_practice_warnings` — fix them before
        re-submitting. `triggers-and-conditions.md`, `automation-actions.md` and
        `template-guidelines.md` ship under `skill_content` by default. Test any unavoidable template
        with ha_eval_template first.

        Consider a dedicated tool first: a state snapshot with no trigger ->
        ha_config_set_scene; a value derived from other entities ->
        ha_config_set_helper(helper_type='template'); a counter / timer /
        schedule / boolean -> ha_config_set_helper.

        MODES (pick one; each can also take `enabled`, which alone with
        `entity_id` turns the automation on or off without touching config):
        - python_transform: surgical edits to an existing automation. Requires
          entity_id and config_hash from ha_config_get_automation(). Operates on
          the stored config exactly as ha_config_get_automation returns it, e.g.
          python_transform="config['triggers'].append({'trigger': 'state', 'entity_id': 'binary_sensor.motion', 'to': 'on'})"
        - config: new automations or full restructures. Regular automations have
          triggers and actions; blueprint automations have use_blueprint
          {path, input}. Home Assistant validates the config, and a rejection
          carries its own error text.
        - take_control_of_blueprint: convert a blueprint-backed automation into a
          standalone one. Takes no config of its own.

        IDENTITY: omit entity_id and config['id'] to create with a generated ID.
        Reusing an entity_id targets the same automation even if the alias
        changes; to rename or replace it, read it first and pass its
        config_hash — a changed alias without that hash is rejected before
        writing. `operation` says whether the write created or updated it.

        EXAMPLES:
        - Create: ha_config_set_automation(config={"alias": "Morning Lights", "triggers": [{"trigger": "time", "at": "07:00:00"}], "actions": [{"action": "light.turn_on", "target": {"area_id": "bedroom"}}]})
        - From a blueprint: ha_config_set_automation(config={"alias": "Motion Light Kitchen", "use_blueprint": {"path": "homeassistant/motion_light.yaml", "input": {"motion_entity": "binary_sensor.kitchen_motion", "light_target": {"entity_id": "light.kitchen"}}}})
        - Update: current = ha_config_get_automation(entity_id="automation.x"); ha_config_set_automation(entity_id="automation.x", config_hash=current["config_hash"], config={...})
        - Disable: ha_config_set_automation(entity_id="automation.x", enabled=False)
        - Run now: ha_config_set_automation(entity_id="automation.x", run_actions=True)
        - Take control: ha_config_set_automation(entity_id="automation.x", take_control_of_blueprint=True)

        TAKE CONTROL is one-way: later blueprint edits stop reaching the
        automation; to change an input value, update 'use_blueprint.input'
        instead. It does NOT free the blueprint — Home Assistant keeps counting
        the converted automation as a user, so deleting that blueprint stays
        refused until the automation is removed (an automation reload does not
        clear it). The response names the blueprint in
        `took_control_of_blueprint`. To preview the rendering without writing
        anything, use ha_manage_blueprints(action="substitute", path=..., input=...).
        """
        bp_warnings: BestPracticeCheckResult = BestPracticeCheckResult()
        try:
            # Reject an empty/whitespace entity_id up-front: the lookup would
            # otherwise answer with a misleading RESOURCE_NOT_FOUND.
            _validate_automation_entity_id(entity_id)
            validate_write_modes(
                "automation",
                "entity_id",
                entity_id,
                config,
                python_transform,
                take_control_of_blueprint,
            )

            detached_blueprint: str | None = None
            if take_control_of_blueprint:
                taken = await self._take_control_config(entity_id)
                config = taken.config
                detached_blueprint = taken.blueprint_path
                # Take control is a read-modify-write the TOOL performs, so it
                # owns the consistency guarantee the caller could not supply:
                # without this, an edit landing between that read and this
                # write is silently overwritten. An explicit caller hash still
                # wins, so it can still lock against a config it read itself.
                config_hash = config_hash or taken.config_hash

            runtime_only_response = await self._maybe_set_runtime_only(
                entity_id,
                config,
                python_transform,
                category,
                enabled,
                run_actions,
                wait,
            )
            if runtime_only_response is not None:
                attach_skill_content(
                    runtime_only_response,
                    MandatoryBPS=MandatoryBPS,
                    canonical_files=_AUTOMATION_SKILL_FILES,
                    referenced_files=bp_warnings.referenced_files,
                )
                return runtime_only_response

            if python_transform is not None:
                response, bp_warnings = await self._run_python_transform(
                    entity_id,
                    config_hash,
                    python_transform,
                    category,
                    MandatoryBPS,
                    enabled,
                    wait,
                    run_actions=run_actions,
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
                        context={"action": "set", "entity_id": entity_id},
                    )
                )

            config_dict = self._parse_and_validate_config(config)

            # Extract category before sending to HA REST API (which rejects unknown keys).
            # Parameter takes precedence over config dict value.
            config_category = config_dict.pop("category", None)
            effective_category = category if category is not None else config_category

            # Purely local input checks run before the tool's own reads, so
            # a malformed config rejects without spending them. (The
            # auto-backup wrapper may take its pre-write snapshot first on
            # updates; that read is outside this tool's control.)
            self._check_input_guards(config_dict, entity_id)

            target: _Target | None = None
            operation = "created"
            if entity_id:
                target, stored = await self._read_write_target(
                    entity_id, config_hash, config_dict
                )
                operation = "created" if stored is None else "updated"

            bp_warnings = _check_best_practices(config_dict)
            validation_meta = await validate_config_references(
                self._client, config_dict
            )

            # Issue #2159: the category is applied post-upsert via
            # ``apply_entity_category``, which HA accepts unchecked. Reject an
            # unknown one here so no automation is created under it.
            await validate_registry_ids(
                self._client,
                None,
                None,
                {"automation": effective_category},
                fail_closed=True,
            )
            await self._validate_with_core(config_dict, entity_id, bp_warnings)

            return await self._run_config_update(
                config_dict,
                entity_id,
                target,
                operation,
                effective_category,
                wait,
                bp_warnings,
                validation_meta,
                MandatoryBPS,
                detached_blueprint,
                enabled,
                run_actions=run_actions,
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings) from None
        except Exception as e:  # noqa: BLE001
            suggestions = [
                "Check automation configuration format",
                "Use ha_search(domain_filter='automation') to find automations",
                "Use ha_get_skill_guide for automation examples",
            ]
            if bp_warnings:
                suggestions.append(
                    "Config had best-practice issues that may be related: "
                    + "; ".join(bp_warnings)
                )
            error = exception_to_structured_error(
                e,
                context={"automation_id": entity_id},
                suggestions=suggestions,
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings)
            raise_tool_error(error)

    async def _validate_with_core(
        self,
        config: dict[str, Any],
        entity_id: str | None,
        bp_warnings: BestPracticeCheckResult,
    ) -> None:
        """Raise Core's own validation failure before writing, when the component runs it."""
        verdict = await automation_validate(self._client, config)
        if verdict is None or verdict.get("status") == "ok":
            return
        status = verdict.get("status")
        await raise_core_rejection(
            self._client,
            str(verdict.get("error") or status),
            config,
            failed_section=section_from_status(status),
            context={"entity_id": entity_id},
            bp_warnings=bp_warnings,
        )

    async def _upsert_automation(
        self,
        config: dict[str, Any],
        entity_id: str | None,
        target: _Target | None,
        operation: str,
        bp_warnings: BestPracticeCheckResult,
    ) -> dict[str, Any]:
        """Write ``config`` to the resolved target, or create one without a target.

        A 400 Core answered becomes VALIDATION_FAILED with Core's text; the
        section it names comes from Core's ``validate_config``.
        """
        try:
            result: dict[str, Any] = await self._client.upsert_automation_config(
                config,
                target.config_id if target is not None else None,
                _resolved=True,
                operation=operation,
            )
        except HomeAssistantAPIError as e:
            core_text = core_rejection_text(e)
            if core_text is None:
                raise
            await raise_core_rejection(
                self._client,
                core_text,
                config,
                failed_section=await failed_section_from_core(self._client, config),
                context={"entity_id": entity_id},
                bp_warnings=bp_warnings,
            )
        return result

    async def _take_control_config(self, entity_id: str | None) -> TakenControl:
        """Render a blueprint automation into the config that replaces it.

        Returns ``(config, blueprint_path, config_hash)``. The hash is of the
        config this read saw, so the caller can lock the write against it.
        Deliberately produces a config
        for the ordinary replacement path rather than writing it here: the
        rendering is not exempt from the validation, best-practice checks or
        skill-content attachment every other automation write goes through,
        and routing it through one write tail is what keeps those guarantees
        from drifting apart.
        """
        if not entity_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id is required for take_control_of_blueprint",
                    suggestions=[
                        "Provide the automation entity_id or config id to convert",
                        "Use ha_search(domain_filter='automation') to find automations",
                    ],
                    context={"action": "take_control", "entity_id": entity_id},
                )
            )

        # The caller need not supply a hash (unlike python_transform, whose
        # expression is written against a config the caller already read), so
        # this read's own hash is returned for the write to lock against.
        current_config, fetched_hash, _ = await self._get_automation_config_internal(
            entity_id
        )

        taken, blueprint_path = await take_control_config(
            self._client, "automation", entity_id, current_config
        )
        return TakenControl(taken, blueprint_path, fetched_hash)

    async def _maybe_set_runtime_only(
        self,
        target: str | None,
        config: Any,
        python_transform: str | None,
        category: str | None,
        enabled: bool | None,
        run_actions: bool,
        wait: bool,
    ) -> dict[str, Any] | None:
        """Handle a standalone runtime request (enabled / run_actions), if any."""
        if (enabled is None and not run_actions) or (
            config is not None or python_transform is not None
        ):
            return None
        if category is not None:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "category requires a config update",
                    suggestions=[
                        "Pass config or python_transform when assigning a category",
                        "Omit category for a standalone enabled or run_actions call",
                    ],
                    context={"action": "set_runtime", "category": category},
                )
            )
        return await self._set_runtime_only(
            target, enabled, wait, run_actions=run_actions
        )

    async def _set_runtime_only(
        self,
        target: str | None,
        enabled: bool | None,
        wait: bool,
        *,
        run_actions: bool = False,
    ) -> dict[str, Any]:
        """Apply enabled and/or run_actions to an existing automation, no config."""
        action = "set_enabled" if enabled is not None else "run_actions"
        if not target:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id is required when setting enabled or run_actions "
                    "without config",
                    suggestions=[
                        "Pass an automation entity_id or unique_id",
                        "Use ha_search(domain_filter='automation') to find automations",
                    ],
                    context={"action": action, "enabled": enabled},
                )
            )
        entity_id = await self._resolve_automation_entity_id_strict(target)
        if not entity_id:
            await self._raise_automation_not_found(target)
        response: dict[str, Any] = {
            "success": True,
            "action": action,
            "automation_id": entity_id,
        }
        await self._apply_runtime_state(
            response,
            entity_id,
            enabled,
            wait,
            target=target,
            run_actions=run_actions,
        )
        return response

    async def _apply_runtime_state(
        self,
        response: dict[str, Any],
        entity_id: str | None,
        enabled: bool | None,
        wait: bool,
        *,
        target: str | None = None,
        run_actions: bool = False,
    ) -> str | None:
        """Apply enabled and then run_actions without touching the stored config.

        Resolves ``entity_id`` from ``target`` when it is None. On a
        standalone call a service failure raises a ToolError; after a config
        write it is reported as a partial-success warning, since the write
        itself already landed. Returns the resolved entity_id.
        """
        if enabled is None and not run_actions:
            return entity_id
        if entity_id is None and target:
            entity_id = await _resolve_runtime_target(self._client, target, response)
        if entity_id is None:
            retry_args: list[str] = []
            if enabled is not None:
                response["enabled_requested"] = enabled
                response["enabled_applied"] = False
                retry_args.append(f"enabled={enabled}")
            if run_actions:
                response["actions_triggered"] = False
                retry_args.append("run_actions=True")
            retry_id = target or response.get("unique_id") or "<automation id>"
            response.setdefault("warnings", []).append(
                "Automation was written, but its entity_id could not be resolved; "
                "the requested runtime change was not applied. Retry with "
                f"ha_config_set_automation(entity_id='{retry_id}', "
                f"{', '.join(retry_args)}) once the automation is loaded."
            )
            return None
        if enabled is not None:
            await self._apply_enabled_state(
                response, entity_id, enabled, wait, target=target
            )
        if run_actions:
            await self._run_automation_actions(response, entity_id, target=target)
        return entity_id

    async def _apply_enabled_state(
        self,
        response: dict[str, Any],
        entity_id: str,
        enabled: bool,
        wait: bool,
        *,
        target: str | None = None,
    ) -> None:
        """Turn the resolved automation on or off, then optionally verify it."""
        try:
            await _set_automation_enabled(self._client, entity_id, enabled)
        except (
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            HomeAssistantConnectionError,
        ) as exc:
            if response.get("action") in _STANDALONE_RUNTIME_ACTIONS:
                exception_to_structured_error(
                    exc,
                    context={
                        "action": "set_enabled",
                        "automation_id": target,
                        "entity_id": entity_id,
                        "enabled": enabled,
                    },
                )
            logger.warning(
                "Enabled state not applied to %s after config write: %s",
                entity_id,
                exc,
            )
            response["enabled_requested"] = enabled
            response["enabled_applied"] = False
            response.setdefault("warnings", []).append(
                "Automation config was written, but the requested enabled state "
                f"could not be applied: {exc}"
            )
            return
        response["enabled"] = enabled
        response["enabled_applied"] = True
        if wait:
            await self._verify_enabled_state(response, entity_id, enabled)

    async def _run_automation_actions(
        self,
        response: dict[str, Any],
        entity_id: str,
        *,
        target: str | None = None,
    ) -> None:
        """Run the automation's actions now via automation.trigger."""
        try:
            await self._client.call_service(
                "automation", "trigger", {"entity_id": entity_id}
            )
        except (
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            HomeAssistantConnectionError,
        ) as exc:
            # With enabled in the same standalone call, the state change has
            # already landed, so a failed run is a partial success, not an error.
            if response.get("action") == "run_actions":
                exception_to_structured_error(
                    exc,
                    context={
                        "action": "run_actions",
                        "automation_id": target,
                        "entity_id": entity_id,
                    },
                )
            logger.warning(
                "Actions not run for %s after config write: %s", entity_id, exc
            )
            response["actions_triggered"] = False
            response.setdefault("warnings", []).append(
                f"The requested change was applied, but the actions could not be run: {exc}"
            )
            return
        response["actions_triggered"] = True

    async def _verify_enabled_state(
        self, response: dict[str, Any], entity_id: str, enabled: bool
    ) -> None:
        """Wait for the automation to report the requested state."""
        expected_state = "on" if enabled else "off"
        try:
            verified = await wait_for_state_change(
                self._client,
                entity_id,
                expected_state=expected_state,
            )
        except (HomeAssistantConnectionError, HomeAssistantAuthError) as exc:
            response.setdefault("warnings", []).append(
                f"Automation {entity_id} was sent {expected_state}, but state "
                f"verification failed: {exc}"
            )
            return
        if verified is None:
            response.setdefault("warnings", []).append(
                f"Automation {entity_id} was sent {expected_state} but its state "
                "could not be verified before the timeout."
            )

    async def _run_python_transform(
        self,
        entity_id: str | None,
        config_hash: str | None,
        python_transform: str,
        category: str | None,
        MandatoryBPS: bool,
        enabled: bool | None,
        wait: bool,
        *,
        run_actions: bool = False,
    ) -> tuple[dict[str, Any], BestPracticeCheckResult]:
        """Execute python_transform mode and return (response, bp_warnings)."""
        if not entity_id:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "entity_id is required for python_transform",
                    suggestions=[
                        "Provide the automation entity_id or config id",
                        "Use ha_search(domain_filter='automation') to find automations",
                    ],
                    context={"action": "python_transform", "entity_id": entity_id},
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
                    context={"action": "python_transform", "entity_id": entity_id},
                )
            )

        current, current_hash, target = await self._get_automation_config_internal(
            entity_id
        )
        self._verify_hash(current_hash, config_hash, entity_id, "python_transform")

        try:
            transformed_config = safe_execute(python_transform, current)
        except PythonSandboxError as e:
            message, suggestions = format_sandbox_error(e, python_transform)
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_FAILED,
                    message,
                    suggestions=suggestions,
                    context={"action": "python_transform", "entity_id": entity_id},
                )
            )

        reject_malformed_list_fields(
            transformed_config,
            _ROOT_LIST_KEYS,
            {"action": "python_transform", "entity_id": entity_id},
            source="python_transform",
        )
        # Pop category before sending to HA REST API (rejects unknown keys)
        transform_category = transformed_config.pop("category", None)
        effective_category = category if category is not None else transform_category

        self._check_input_guards(
            transformed_config, entity_id, source="python_transform"
        )
        bp_warnings = _check_best_practices(transformed_config)

        # Issue #2159: reject an unknown category before the write, so a
        # transform never lands under a category that does not exist.
        await validate_registry_ids(
            self._client,
            None,
            None,
            {"automation": effective_category},
            fail_closed=True,
        )
        await self._validate_with_core(transformed_config, entity_id, bp_warnings)

        runtime_requested = enabled is not None or run_actions
        async with config_reload_waiter(
            self._client, "automation_reloaded", enabled=runtime_requested
        ) as wait_for_reload:
            result = await self._upsert_automation(
                transformed_config, entity_id, target, "updated", bp_warnings
            )
            reloaded = await wait_for_reload()
            note_reload_outcome(
                result, reloaded, domain="automation", requested=enabled is not None
            )
        run_actions = _run_actions_once_reloaded(
            result, reloaded, run_actions, entity_id
        )
        new_config_hash = await self._read_back_hash(target.config_id, result)

        resolved_entity_id = result.get("entity_id") or target.entity_id
        if effective_category and resolved_entity_id:
            await apply_entity_category(
                self._client,
                resolved_entity_id,
                effective_category,
                "automation",
                result,
                "automation",
            )

        resolved_entity_id = await self._apply_runtime_state(
            result,
            resolved_entity_id,
            enabled,
            wait,
            target=entity_id,
            run_actions=run_actions,
        )
        _sync_post_write_automation_result(result, resolved_entity_id)

        response: dict[str, Any] = {
            "success": True,
            "action": "python_transform",
            "automation_id": resolved_entity_id or entity_id,
            "config_hash": new_config_hash,
            "python_expression": python_transform,
            "message": f"Automation {entity_id} updated via Python transform",
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

    async def _read_back_hash(
        self, config_id: str, result: dict[str, Any]
    ) -> str | None:
        """Hash of the body Core stored; the write is saved even if the read fails."""
        try:
            stored = await self._read_stored(config_id)
        except (HomeAssistantAPIError, HomeAssistantConnectionError) as e:
            result.setdefault("warnings", []).append(
                f"Saved; reading it back failed ({e}), re-read it for a config_hash"
            )
            return None
        return compute_config_hash(stored) if stored is not None else None

    async def _run_config_update(
        self,
        config_dict: dict[str, Any],
        entity_id: str | None,
        target: _Target | None,
        operation: str,
        effective_category: str | None,
        wait: bool,
        bp_warnings: BestPracticeCheckResult,
        validation_meta: dict[str, Any],
        MandatoryBPS: bool,
        detached_blueprint: str | None = None,
        enabled: bool | None = None,
        *,
        run_actions: bool = False,
    ) -> dict[str, Any]:
        """Execute config-replacement mode and return the tool response.

        ``target`` is the resolved automation for an ``entity_id`` write (None
        creates under a generated id) and ``operation`` is what reading it
        found. ``detached_blueprint`` is set by take-control mode and names the
        blueprint the automation's config no longer references. It is
        reported on the response as ``took_control_of_blueprint`` and nothing
        more: Home Assistant goes on counting the automation as a user of that
        blueprint until it is removed, so there is no release to wait for.
        """
        runtime_requested = enabled is not None or run_actions
        async with config_reload_waiter(
            self._client, "automation_reloaded", enabled=runtime_requested
        ) as wait_for_reload:
            result = await self._upsert_automation(
                config_dict, entity_id, target, operation, bp_warnings
            )
            reloaded = await wait_for_reload()
            note_reload_outcome(
                result, reloaded, domain="automation", requested=enabled is not None
            )
        config_id = str(result.get("unique_id") or (target.config_id if target else ""))
        run_actions = _run_actions_once_reloaded(
            result, reloaded, run_actions, entity_id or config_id
        )
        result["operation"] = operation
        result["config_hash"] = await self._read_back_hash(config_id, result)

        # A create already polled for the entity inside the upsert; with wait=True
        # the resolver below polls again. Either way, _apply_runtime_state must
        # not start a further poll of its own.
        entity_already_polled = wait or bool(result.get("entity_not_verified"))
        resolved_entity_id = await _resolve_post_write_automation_entity(
            self._client,
            config_id,
            result.get("entity_id") or (target.entity_id if target else None),
            wait,
            result,
        )
        _sync_post_write_automation_result(result, resolved_entity_id)

        if result.get("entity_not_verified"):
            result.setdefault("warnings", []).append(
                f"{NOT_VERIFIED_WARNING_PREFIX} "
                "after polling. The automation may still have been created -- check Home "
                "Assistant logs and try reloading automations. Common causes: "
                "automations.yaml vs automation.yaml filename mismatch, invalid config "
                "that HA accepted but failed to load, or slow hardware."
            )
            result.pop("entity_not_verified", None)

        if wait and resolved_entity_id:
            try:
                registered = await wait_for_entity_registered(
                    self._client, resolved_entity_id
                )
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Automation {operation} but {resolved_entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                result.setdefault("warnings", []).append(
                    f"Automation {operation} but verification failed: {e}"
                )

        if effective_category and resolved_entity_id:
            await apply_entity_category(
                self._client,
                resolved_entity_id,
                effective_category,
                "automation",
                result,
                "automation",
            )

        resolved_entity_id = await self._apply_runtime_state(
            result,
            resolved_entity_id,
            enabled,
            wait,
            target=None if entity_already_polled else config_id,
            run_actions=run_actions,
        )
        _sync_post_write_automation_result(result, resolved_entity_id)

        if bp_warnings:
            result["best_practice_warnings"] = list(bp_warnings)

        merge_validation_meta(result, validation_meta)

        automation_id = resolved_entity_id or entity_id or result.get("unique_id")
        response = {
            "success": True,
            # automation_id omitted when all three fallbacks are falsy —
            # surfacing automation_id=None would lie about resolvability.
            # Defensive: HA's upsert normally returns a usable id, so this
            # fallback rarely triggers.
            **({"automation_id": automation_id} if automation_id else {}),
            **_strip_redundant_identifier_echo(result),
        }
        if detached_blueprint:
            response["took_control_of_blueprint"] = detached_blueprint
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

    async def _read_write_target(
        self, entity_id: str, config_hash: str | None, config: dict[str, Any]
    ) -> tuple[_Target, dict[str, Any] | None]:
        """Read a write's target: its stored body, or None for an unused config id.

        The hash check and the alias-replacement guard both run on the stored
        body. An entity_id, or a hash, naming nothing stored is
        RESOURCE_NOT_FOUND: only an unused config id creates.
        """
        target = await self._resolve_target(entity_id, with_entity=False)
        stored = await self._read_stored(target.config_id)
        if stored is None:
            if config_hash or entity_id.startswith("automation."):
                await self._raise_automation_not_found(entity_id)
            return target, None
        if config_hash:
            self._verify_hash(
                compute_config_hash(stored), config_hash, entity_id, "set"
            )
        else:
            self._guard_alias_replacement(entity_id, stored, config)
        return target, stored

    @staticmethod
    def _guard_alias_replacement(
        entity_id: str, current: dict[str, Any], config: dict[str, Any]
    ) -> None:
        """Require a prior read before replacing a differently named automation.

        This catches sequential ID reuse. The REST read and write are separate
        requests, so it does not provide atomic protection against other writers.
        """
        if current.get("alias") == config.get("alias"):
            return
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Automation {entity_id!r} already exists as {current.get('alias')!r}. "
                f"This write would replace it with {config.get('alias')!r}. Nothing was written.",
                context={"entity_id": entity_id, "parameter": "config_hash"},
                suggestions=[
                    "Omit entity_id and remove config['id'] to create a separate automation.",
                    (
                        "For an intentional rename or replacement, call ha_config_get_automation "
                        "for this entity_id, inspect its config, and resubmit with its config_hash."
                    ),
                ],
            )
        )

    @staticmethod
    def _verify_hash(
        current_hash: str, config_hash: str, entity_id: str, action: str
    ) -> None:
        """Raise a conflict when the stored body changed since the caller read it."""
        if current_hash == config_hash:
            return
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                "Automation modified since last read (conflict)",
                suggestions=[
                    "Call ha_config_get_automation() again",
                    "Use the fresh config_hash from that response",
                ],
                context={"action": action, "entity_id": entity_id},
            )
        )

    async def _raise_automation_not_found(self, entity_id: str) -> NoReturn:
        """Raise a structured RESOURCE_NOT_FOUND ToolError for a missing automation.

        Populates ``available_automation_ids`` (up to 10) from the entity
        registry.
        """
        available_ids = await list_visible_registry_ids(self._client, "automation")
        raise_tool_error(
            create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                f"Automation not found: {entity_id}",
                context={
                    "automation_id": entity_id,
                    "available_automation_ids": available_ids,
                },
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
            )
        )

    async def _get_automation_config_internal(
        self, entity_id: str
    ) -> tuple[dict[str, Any], str, _Target]:
        """Resolve and read an existing automation: (stored body, its hash, target).

        Raises RESOURCE_NOT_FOUND when nothing is stored under it.
        """
        target = await self._resolve_target(entity_id)
        stored = await self._read_stored(target.config_id)
        if stored is None:
            await self._raise_automation_not_found(entity_id)
        return stored, compute_config_hash(stored), target

    @staticmethod
    def _check_input_guards(
        config_dict: dict[str, Any], entity_id: str | None, source: str = "config"
    ) -> None:
        """Reject the misroutes and duplicate creation HA-MCP guards against.

        Core validates the automation itself; these are HA-MCP's own checks.
        """
        if "use_blueprint" in config_dict:
            # HA-MCP convenience: Core merges the instance config over the
            # rendered blueprint, so an empty list here would replace the
            # blueprint's own triggers/conditions/actions.
            for key in _ROOT_LIST_KEYS:
                if config_dict.get(key) == []:
                    del config_dict[key]

        reject_sequence_misroute(config_dict, entity_id, source)
        reject_enabled_in_config(config_dict)

        # Issue #1169: see _check_scene_create_misroute
        AutomationConfigTools._check_scene_create_misroute(config_dict, entity_id)

        # Prevent duplicate creation when config contains an existing automation id
        if entity_id is None and "id" in config_dict:
            existing_id = config_dict["id"]
            raise_tool_error(
                create_validation_error(
                    f"Config contains 'id' field ('{existing_id}') but no entity_id was provided. "
                    "This would create a duplicate automation instead of updating the existing one.",
                    parameter="entity_id",
                    details=f"To update, pass entity_id='{existing_id}' (or the automation's entity_id). "
                    "To create a genuinely new automation, remove the 'id' field from the config.",
                )
            )

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
        reject_malformed_list_fields(
            parsed_config,
            _ROOT_LIST_KEYS,
            {"parameter": "config"},
        )

        return cast(dict[str, Any], parsed_config)

    @staticmethod
    def _check_scene_create_misroute(
        config_dict: dict[str, Any], entity_id: str | None
    ) -> None:
        """Raise if an empty-trigger config wraps scene.create (common model misroute)."""
        trigger_value = config_dict.get("triggers", config_dict.get("trigger"))
        trigger_empty = trigger_value is None or (
            isinstance(trigger_value, list) and not trigger_value
        )
        if not trigger_empty:
            return
        actions_list = coerce_to_list(
            config_dict.get("actions", config_dict.get("action"))
        )
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
                        "entity_id": entity_id,
                    },
                )
            )

    async def _resolve_automation_entity_id_strict(self, target: str) -> str | None:
        """Resolve a target without hiding transport/authentication errors."""
        states = await self._client.get_states()
        for state in states:
            state_entity_id = state.get("entity_id", "")
            if state_entity_id.startswith("automation.") and (
                state_entity_id == target
                or state.get("attributes", {}).get("id") == target
            ):
                return str(state_entity_id)
        return None

    @tool(
        name="ha_config_remove_automation",
        tags={"Automations"},
        annotations=write_hints(
            "Remove Automation", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(domain="automation", id_param="entity_id")
    @log_tool_usage
    async def ha_config_remove_automation(
        self,
        entity_id: Annotated[
            str,
            Field(
                description="The automation's entity_id (e.g. 'automation.old_automation') "
                "or its config id"
            ),
        ],
        wait: Annotated[
            bool,
            Field(
                description="Wait for automation to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant automation permanently.

        A disabled automation is removed too. The returned `automation_id` is
        the resolved entity_id, falling back to the input.

        EXAMPLES:
        - Delete automation: ha_config_remove_automation("automation.old_automation")
        - Delete by config id: ha_config_remove_automation("1712345678901")
        """
        try:
            # Empty/whitespace would surface as a misleading HA delete-failure.
            validate_identifier_not_empty(
                entity_id,
                "entity_id",
                suggestions=[
                    "Use ha_search(domain_filter='automation') to find existing automations"
                ],
                context={"operation": "remove_automation"},
            )
            target = await self._resolve_target(entity_id)
            if not target.entity_id:
                logger.warning(
                    f"Could not resolve '{entity_id}' to an entity_id -- wait verification will be skipped"
                )

            try:
                result = await self._client.delete_automation_config(target.config_id)
            except HomeAssistantAPIError as e:
                if e.status_code == 404:
                    await self._raise_automation_not_found(entity_id)
                raise

            if wait and target.entity_id:
                await verify_entity_removed(self._client, target.entity_id, result)

            return {
                "success": True,
                "action": "delete",
                "automation_id": target.entity_id or entity_id,
                **_strip_redundant_identifier_echo(result),
            }
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={"automation_id": entity_id, "action": "delete"},
                suggestions=[
                    "Verify automation exists using ha_search(domain_filter='automation')",
                    "Pass an automation entity_id (automation.morning_routine) or config id",
                    "Check Home Assistant connection",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_config_automation_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant automation configuration tools."""
    register_tool_methods(mcp, AutomationConfigTools(client))
