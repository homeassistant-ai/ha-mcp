"""
Configuration management tools for Home Assistant scripts.

This module provides tools for retrieving, creating, updating, and removing
Home Assistant script configurations.
"""

import logging
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from ..strict_bps import BestPracticeKeyParam
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
from .blueprint_substitute import (
    TakenControl,
    take_control_config,
    validate_write_modes,
)
from .entity_registration import resolve_entity_id_after_write
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .reference_validator import validate_config_references
from .tools_config_helpers import validate_registry_ids
from .util_helpers import (
    JSON_STRING_COERCION,
    apply_entity_category,
    attach_skill_content,
    augment_error_dict_with_skill_content,
    augment_tool_error_with_skill_content,
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


def _skip_script_run_backup(kwargs: dict[str, Any]) -> bool:
    """Skip config snapshots for a run start/stop (no config change)."""
    return kwargs.get("run") is not None


class ConfigScriptTools:
    """Script configuration management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_script",
        tags={"Scripts"},
        annotations={
            "openWorldHint": False,
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

        The returned `script_id` is the canonical bare storage key resolved by the REST client (matching what `ha_config_set_script` / `ha_config_remove_script` expect), falling back to the input identifier on the rare path where the REST envelope omits it. Prefix handling matches `ha_config_get_automation` in behavior (mechanism differs: automations resolve via state lookup; scripts strip the prefix).

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
            # ``ha_search(domain_filter='script')`` which returns
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
                    "Use ha_search(domain_filter='script') to list scripts",
                ],
            )

            # Script gets ALWAYS take the legacy path — the component's
            # in-process ``config_get`` was withdrawn. It served
            # ``entity.raw_config``, which is only the storage body as of the
            # last COMPLETED async reload, with no version marker to tell a
            # fresh body from a stale one. A get racing a reload returned the
            # pre-edit body and broke the get -> python_transform -> set
            # round-trip (caught live by the automation/script config e2e on the
            # arm/HAOS runners). The legacy REST config endpoint reads the config
            # FILE, which is fresh the instant a write lands, so it stays the
            # sole path. Scenes were already legacy-only (no storage body in
            # memory at all); scripts join them here for freshness. A
            # file-reading ``config_get`` may return later (issue #1813).
            return await self._legacy_get_script(script_id)
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"script_id": script_id},
                suggestions=[
                    "Verify script_id exists using ha_search(domain_filter='script')",
                    "Check Home Assistant connection",
                    "Use ha_get_skill_guide for help",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _legacy_get_script(self, script_id: str) -> dict[str, Any]:
        """Assemble the script-get response from the REST/WS pipeline.

        The multi-fetch path: id-resolution WS + per-id config REST +
        ``fetch_entity_category`` WS call. This is the ONLY script-get path —
        see ``ha_config_get_script`` for why script gets never route through the
        component's ``config_get`` (its ``raw_config`` freshness lags the config
        file between a write and the next completed reload).
        """
        config_result = await self._fetch_script_config_envelope(script_id)
        # Extract actual script config body and compute hash before category injection
        actual_config = config_result.get("config", config_result)
        config_hash_value = compute_config_hash(actual_config)

        # Fetch category from entity registry (best-effort)
        # (injected after hash so transient registry failures don't affect the hash)
        entity_id = f"script.{script_id}"
        cat_warnings: list[str] = []
        cat_id = await fetch_entity_category(
            self._client, entity_id, "script", cat_warnings
        )
        if cat_id:
            actual_config["category"] = cat_id

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

        response: dict[str, Any] = {
            "success": True,
            "action": "get",
            "script_id": canonical_id,
            # The script BODY, not the REST envelope. Returning the envelope
            # here put ``{success, script_id, config}`` under ``config``, so
            # ``response["config"]["sequence"]`` did not exist and every caller
            # had to unwrap twice -- the e2e carried a helper to do exactly
            # that. ``ha_config_get_automation`` returns the body, this
            # docstring promises the body, and the hash is already computed
            # over the body.
            "config": actual_config,
            "config_hash": config_hash_value,
        }
        # Top level, not inside ``config``: warnings are a top-level list[str]
        # by contract.
        if cat_warnings:
            response.setdefault("warnings", []).extend(cat_warnings)
        return response

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
                    "Use ha_search(domain_filter='script') to find existing scripts"
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
    ) -> tuple[dict[str, Any], str, str | None]:
        """Fetch script config without logging or category injection.

        Returns ``(actual_config, config_hash, resolved_id)`` where
        ``actual_config`` is the inner script body (not the REST wrapper) and
        ``resolved_id`` is the storage key the REST client resolved the input to
        (from the envelope's ``script_id``). Threading ``resolved_id`` lets the
        upsert call site skip the redundant re-resolve (issue #1813 Phase 0).
        Used internally by _fetch_and_verify_hash and ha_config_get_script.

        ``resolved_id`` is ``None`` when the envelope omits ``script_id`` — the
        write target must then be re-resolved from the caller's input rather
        than defaulting to the (unresolved) caller slug, which for a renamed
        script would target the wrong storage key. This is a structural
        invariant, not a live path: ``get_script_config`` always sets the key.

        404 responses from the REST client are mapped to a structured
        ``RESOURCE_NOT_FOUND`` ToolError via ``_fetch_script_config_envelope``.
        """
        config_result = await self._fetch_script_config_envelope(script_id)
        actual_config = config_result.get("config", config_result)
        config_hash_value = compute_config_hash(actual_config)
        resolved_id = config_result.get("script_id")
        return actual_config, config_hash_value, resolved_id

    async def _fetch_and_verify_hash(
        self, script_id: str, config_hash: str, action: str
    ) -> tuple[dict[str, Any], str | None]:
        """Fetch current script config and verify config_hash for optimistic locking.

        Returns ``(actual_config, resolved_id)`` — the inner script body and the
        storage key the REST client resolved the input to. Threading
        ``resolved_id`` lets the upsert call site skip the redundant re-resolve
        (issue #1813 Phase 0); it is ``None`` when the envelope omitted the key,
        so the upsert re-resolves rather than writing to the caller slug. Raises
        ToolError if the hash does not match (conflict).
        """
        (
            actual_config,
            current_hash,
            resolved_id,
        ) = await self._get_script_config_internal(script_id)
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
        return actual_config, resolved_id

    async def _upsert_script(
        self, config: dict[str, Any], script_id: str, resolved_id: str | None
    ) -> dict[str, Any]:
        """Upsert, threading a pre-resolved storage key when available.

        ``script_id`` is always the CALLER's identifier (used to default a
        missing alias). ``resolved_id`` — set only when ``_fetch_and_verify_hash``
        already resolved the storage key — is passed as the write target so the
        REST client skips the redundant entity-registry lookup (issue #1813
        Phase 0); None lets the REST client resolve. Passing the caller id
        separately keeps a renamed script's alias caller-facing (#1935).
        """
        result: dict[str, Any] = await self._client.upsert_script_config(
            config, script_id, resolved_id=resolved_id
        )
        return result

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
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Create or Update Script",
        },
    )
    @with_auto_backup(
        domain="script", id_param="script_id", skip_fn=_skip_script_run_backup
    )
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
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Script configuration dictionary. "
                "Mutually exclusive with python_transform.",
                default=None,
            ),
        ] = None,
        python_transform: Annotated[
            str | None,
            Field(
                description="Python expression to transform existing script config. "
                "Mutually exclusive with config. "
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
                "Optional for config updates (validates before full replacement if provided).",
            ),
        ] = None,
        take_control_of_blueprint: Annotated[
            bool,
            Field(
                description="Convert a blueprint-backed script into an editable "
                'standalone one -- the UI\'s "Take control". Renders the blueprint '
                "with its current inputs and saves the result over the same script, "
                "which keeps its script_id, alias and description and then has its "
                "own sequence and no 'use_blueprint'. Mutually exclusive with config "
                "and python_transform.",
                default=False,
            ),
        ] = False,
        category: Annotated[
            str | None,
            Field(
                description="Category ID to assign to this script. Use ha_config_get_category(scope='script') to list available categories, or ha_config_set_category() to create one.",
                default=None,
            ),
        ] = None,
        wait: Annotated[
            bool,
            Field(
                description="Wait for script to be queryable before returning. Set to False for "
                "bulk operations.",
                default=True,
            ),
        ] = True,
        run: Annotated[
            Literal["start", "stop"] | None,
            Field(
                description=(
                    "Run control, used alone with script_id (no config, "
                    "python_transform or take_control_of_blueprint): 'start' runs "
                    "the script (script.turn_on; returns once it has started, "
                    "without waiting for it to finish), 'stop' stops its running "
                    "executions (script.turn_off). Stopping does not disable the "
                    "script; Home Assistant has no script enable/disable."
                ),
                default=None,
            ),
        ] = None,
        variables: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "With run='start': values for the script's fields, passed to "
                    "the run as its variables."
                ),
                default=None,
            ),
        ] = None,
        MandatoryBPS: Annotated[
            bool,
            Field(default=True),
        ] = True,
        # BestPracticeKey (#1779): consumed by StrictBpsMiddleware, never read
        # here — see strict_bps.py for the declaration contract.
        BestPracticeKey: BestPracticeKeyParam = None,
    ) -> dict[str, Any]:
        """Create or update a Home Assistant script.

        MUST call ha_get_skill_guide OR refer to your locally installed skills first.

        Prefer native actions (`choose` / `if`, `wait_for_trigger`, `repeat`,
        `for:`) over templates in logic positions; templates belong only in
        `data.*`, notification text, `event_data` and `variables`. The
        best-practice checker reports violations under `best_practice_warnings`.
        `automation-patterns.md` and `template-guidelines.md` ship under
        `skill_content` by default.

        Scripts use 'sequence', NOT 'trigger' or 'action'; for trigger-based
        execution use ha_config_set_automation.

        MODES (pick one):
        - python_transform: surgical edits to an existing script. Requires
          config_hash from ha_config_get_script(), e.g.
          python_transform="config['sequence'].append({'delay': {'seconds': 5}})"
        - config: new scripts or full restructures. Needs 'sequence' (regular) or
          'use_blueprint' {path, input} (blueprint-based).
        - take_control_of_blueprint: convert a blueprint-backed script into a
          standalone one. Takes no config of its own.
        - run (alone with script_id): 'start' runs the script (optional
          variables), 'stop' stops its running executions. Write config changes
          in a separate call first: Home Assistant signals no completion of the
          script reload a write triggers.

        EXAMPLES:
        - Create: ha_config_set_script(script_id="blink_light", config={"alias": "Light Blink", "sequence": [{"action": "light.turn_on", "target": {"entity_id": "light.living_room"}}, {"delay": {"seconds": 2}}, {"action": "light.turn_off", "target": {"entity_id": "light.living_room"}}]})
        - From a blueprint: ha_config_set_script(script_id="notification_script", config={"alias": "My Notification Script", "use_blueprint": {"path": "notification_script.yaml", "input": {"message": "Hello World"}}})
        - Take control: ha_config_set_script(script_id="notification_script", take_control_of_blueprint=True)

        TAKE CONTROL is one-way: later blueprint edits stop reaching the script;
        to change an input value, update 'use_blueprint.input' instead. It does
        NOT free the blueprint — Home Assistant keeps counting the converted
        script as a user, so deleting that blueprint stays refused until the
        script is removed. To preview the rendering without writing anything,
        use ha_manage_blueprints(action="substitute", domain="script", path=..., input=...).
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
            # ``ha_search(domain_filter='script')`` can update it
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
                    "Use ha_search(domain_filter='script') to list scripts",
                ],
                context={"action": "set"},
            )
            if run is not None or variables is not None:
                return await self._run_script_control(
                    script_id,
                    run,
                    variables,
                    has_write=config is not None
                    or python_transform is not None
                    or take_control_of_blueprint,
                    category=category,
                    MandatoryBPS=MandatoryBPS,
                )
            validate_write_modes(
                "script",
                "script_id",
                script_id,
                config,
                python_transform,
                take_control_of_blueprint,
            )

            detached_blueprint: str | None = None
            if take_control_of_blueprint:
                taken = await self._take_control_config(script_id)
                config = taken.config
                detached_blueprint = taken.blueprint_path
                # Take control is a read-modify-write the TOOL performs, so it
                # owns the consistency guarantee the caller could not supply:
                # without this, an edit landing between that read and this
                # write is silently overwritten. An explicit caller hash wins.
                config_hash = config_hash or taken.config_hash

            # Handle python_transform mode
            if python_transform is not None:
                transformed_config, resolved_id = await self._prepare_script_transform(
                    script_id, config_hash, python_transform
                )
                # Mirror the automations transform path (issue #2159): honor
                # the category param and a transform-set "category" key (the
                # REST API rejects unknown keys), validate before the write,
                # and apply post-upsert.
                transform_category = transformed_config.pop("category", None)
                effective_category = (
                    category if category is not None else transform_category
                )
                await validate_registry_ids(
                    self._client,
                    None,
                    None,
                    {"script": effective_category},
                    fail_closed=True,
                )
                bp_warnings = _check_best_practices(transformed_config)
                return await self._commit_script_transform(
                    script_id,
                    transformed_config,
                    resolved_id,
                    python_transform,
                    bp_warnings,
                    MandatoryBPS,
                    wait,
                    effective_category,
                )

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

            # Optional hash check for full config updates. When it runs it
            # resolves ``script_id`` to the storage key — thread that through so
            # the upsert doesn't re-resolve (issue #1813 Phase 0). Stays None on
            # the no-hash path (raw script_id resolved once, inside upsert).
            resolved_key: str | None = None
            if config_hash:
                _, resolved_key = await self._fetch_and_verify_hash(
                    script_id, config_hash, "set"
                )

            # Pre-check for best-practice issues.
            bp_warnings = _check_best_practices(config_dict)

            # Issue #2159: the category is applied post-upsert via
            # ``apply_entity_category``, which HA accepts unchecked. Reject an
            # unknown one here so no script is created under it.
            await validate_registry_ids(
                self._client,
                None,
                None,
                {"script": effective_category},
                fail_closed=True,
            )

            return await self._commit_script_config(
                config_dict,
                script_id,
                effective_category,
                resolved_key,
                wait,
                bp_warnings,
                MandatoryBPS,
                detached_blueprint,
            )

        except ToolError as te:
            raise augment_tool_error_with_skill_content(te, bp_warnings) from None
        except Exception as e:
            suggestions = [
                "Ensure config includes either 'sequence' field (regular scripts) or 'use_blueprint' field (blueprint-based scripts)",
                "For blueprint scripts, use ha_manage_blueprints(action='list', domain='script') to list available blueprints",
                "Validate sequence actions syntax for regular scripts",
                "Check entity_ids exist if using service calls",
                "Use ha_search(domain_filter='script') to find scripts",
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
            error = exception_to_structured_error(
                e,
                context={"script_id": script_id},
                suggestions=suggestions,
                raise_error=False,
            )
            augment_error_dict_with_skill_content(error, bp_warnings)
            raise_tool_error(error)

    async def _run_script_control(
        self,
        script_id: str,
        run: str | None,
        variables: dict[str, Any] | None,
        *,
        has_write: bool,
        category: str | None,
        MandatoryBPS: bool,
    ) -> dict[str, Any]:
        """Start or stop a script without touching its config."""
        problem = None
        if run is None:
            problem = "variables only apply with run='start'"
        elif has_write:
            problem = (
                "run cannot be combined with config, python_transform or "
                "take_control_of_blueprint"
            )
        elif category is not None:
            problem = "category requires a config update"
        elif variables is not None and run != "start":
            problem = "variables only apply with run='start'"
        if problem:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    problem,
                    suggestions=[
                        "Write the script config in one call, then start or stop "
                        + "it in a second call with only script_id and run",
                        "Pass variables together with run='start'",
                    ],
                    context={"action": "run", "script_id": script_id, "run": run},
                )
            )
        # An existing script needs one registry lookup, not a registration poll.
        entity_id = await resolve_entity_id_after_write(
            self._client, script_id, "script", timeout=0
        )
        # HA's script services are a silent no-op on an unknown entity.
        try:
            await self._client.get_entity_state(entity_id)
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                await self._raise_script_not_found(script_id)
            raise
        data: dict[str, Any] = {"entity_id": entity_id}
        if variables:
            data["variables"] = variables
        service = "turn_on" if run == "start" else "turn_off"
        try:
            await self._client.call_service("script", service, data)
        except (
            HomeAssistantAPIError,
            HomeAssistantAuthError,
            HomeAssistantConnectionError,
        ) as exc:
            exception_to_structured_error(
                exc,
                context={"action": run, "script_id": script_id, "entity_id": entity_id},
            )
        response: dict[str, Any] = {
            "success": True,
            "action": run,
            "script_id": script_id,
            "entity_id": entity_id,
        }
        attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCRIPT_SKILL_FILES,
            referenced_files=None,
        )
        return response

    async def _prepare_script_transform(
        self, script_id: str, config_hash: str | None, python_transform: str
    ) -> tuple[dict[str, Any], str | None]:
        """Validate + run a script python_transform.

        Returns ``(transformed_config, resolved_id)``. Extracted verbatim from
        ``ha_config_set_script``'s python_transform branch (the pre-
        ``_check_best_practices`` head): enforces config_hash, fetches +
        hash-verifies, executes the sandboxed transform, and validates the
        result still has a sequence or blueprint. Raises a structured ToolError
        on any violation.
        """
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
        actual_config, resolved_id = await self._fetch_and_verify_hash(
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
        return transformed_config, resolved_id

    async def _finalize_script_write(
        self,
        result: dict[str, Any],
        script_id: str,
        resolved_key: str | None,
        wait: bool,
        category: str | None,
    ) -> None:
        """Resolve, wait, and apply a category for either script write mode.

        Match the upsert's storage key, which can differ from the caller's
        entity ID after a registry rename. Keep the caller's ID as fallback.
        """
        if not wait and not category:
            return
        storage_key = result.get("script_id") or resolved_key or script_id
        entity_id = await resolve_entity_id_after_write(
            self._client,
            storage_key,
            "script",
            fallback_entity_id=f"script.{script_id.removeprefix('script.')}",
        )
        if wait:
            try:
                registered = await wait_for_entity_registered(self._client, entity_id)
                if not registered:
                    result.setdefault("warnings", []).append(
                        f"Script saved but {entity_id} not yet queryable. "
                        "It may take a moment to become available."
                    )
            except (HomeAssistantConnectionError, HomeAssistantAuthError) as e:
                result.setdefault("warnings", []).append(
                    f"Script saved but verification failed: {e}"
                )
        if category:
            await apply_entity_category(
                self._client, entity_id, category, "script", result, "script"
            )

    async def _commit_script_transform(
        self,
        script_id: str,
        transformed_config: dict[str, Any],
        resolved_id: str | None,
        python_transform: str,
        bp_warnings: BestPracticeCheckResult,
        MandatoryBPS: bool,
        wait: bool,
        effective_category: str | None = None,
    ) -> dict[str, Any]:
        """Upsert, refresh the hash, finalize wait/category, and build the response."""
        # Save transformed config. ``_fetch_and_verify_hash`` already
        # resolved the storage key; pass it as the write target so the
        # upsert skips the redundant re-resolve (issue #1813 Phase 0).
        # ``script_id`` stays the caller id (the fetched config already
        # carries an alias here, so the default is a no-op, but keep the
        # contract consistent — #1935).
        result = await self._client.upsert_script_config(
            transformed_config, script_id, resolved_id=resolved_id
        )

        # Re-fetch to get authoritative hash (HA may normalize after save)
        _, new_config_hash, _ = await self._get_script_config_internal(script_id)

        await self._finalize_script_write(
            result, script_id, resolved_id, wait, effective_category
        )

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

    async def _take_control_config(self, script_id: str) -> TakenControl:
        """Render a blueprint script into the config that replaces it.

        The hash is of the config this read saw, so the write locks against it. Produces a
        config for the ordinary replacement path rather than
        writing it here, so the rendering goes through the same validation,
        best-practice checks and skill-content attachment as every other
        script write.
        """
        current_config, fetched_hash, _ = await self._get_script_config_internal(
            script_id
        )
        taken, blueprint_path = await take_control_config(
            self._client, "script", script_id, current_config
        )
        return TakenControl(taken, blueprint_path, fetched_hash)

    async def _commit_script_config(
        self,
        config_dict: dict[str, Any],
        script_id: str,
        effective_category: str | None,
        resolved_key: str | None,
        wait: bool,
        bp_warnings: BestPracticeCheckResult,
        MandatoryBPS: bool,
        detached_blueprint: str | None = None,
    ) -> dict[str, Any]:
        """Validate references, upsert, finalize wait/category, and build the response."""
        # Cross-check literal service and entity references against
        # the live registries. Soft warnings only — the write still
        # happens, even when references don't resolve (#940).
        validation_meta = await validate_config_references(self._client, config_dict)

        result = await self._upsert_script(config_dict, script_id, resolved_key)

        await self._finalize_script_write(
            result, script_id, resolved_key, wait, effective_category
        )

        if bp_warnings:
            result["best_practice_warnings"] = list(bp_warnings)

        merge_validation_meta(result, validation_meta)

        response = {
            "success": True,
            **result,
        }
        if detached_blueprint:
            response["took_control_of_blueprint"] = detached_blueprint
        # attach AFTER the outer dict is built so hint lands at
        # position 0 of the FINAL response (see BAT history in
        # util_helpers._SKILL_CONTENT_OPTOUT_HINT).
        attach_skill_content(
            response,
            MandatoryBPS=MandatoryBPS,
            canonical_files=_SCRIPT_SKILL_FILES,
            referenced_files=bp_warnings.referenced_files,
        )
        return response

    @tool(
        name="ha_config_remove_script",
        tags={"Scripts"},
        annotations={
            "openWorldHint": False,
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
            bool,
            Field(
                description="Wait for script to be fully removed before returning.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Delete a Home Assistant script.

        EXAMPLE: ha_config_remove_script("old_script")

        Only scripts created via the Home Assistant UI can be deleted. Scripts
        defined in YAML configuration files (scripts.yaml or configuration.yaml)
        cannot be deleted through the API and return a 405 Method Not Allowed
        error; edit the configuration file directly instead.

        WARNING: Deleting a script that is used by automations may cause those automations to fail.
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
                    "Use ha_search(domain_filter='script') to find existing script_ids"
                ],
                context={"operation": "remove_script"},
            )
            result = await self._client.delete_script_config(script_id)

            # Wait for script to be removed
            entity_id = f"script.{script_id}"
            if wait:
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
                    "Verify script_id exists using ha_search(domain_filter='script')",
                    "Check if script is being used by automations",
                    "Use ha_get_skill_guide for help",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_config_script_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant script configuration tools."""
    register_tool_methods(mcp, ConfigScriptTools(client))
