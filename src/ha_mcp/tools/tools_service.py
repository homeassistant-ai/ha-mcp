"""
Service call and device operation tools for Home Assistant MCP server.

This module provides service execution and WebSocket-enabled operation monitoring tools.
"""

import logging
from typing import Annotated, Any, NamedTuple, NoReturn, NotRequired, TypedDict, cast

import httpx
from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import ConfigDict, Field, SkipValidation, TypeAdapter, ValidationError

from ..client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..errors import (
    ErrorCode,
    create_connection_error,
    create_error_response,
    create_validation_error,
)
from ..utils.entity_membership import normalize_member_entity_ids
from .bulk_selector import (
    _NON_AGGREGATE_ROOT_DOMAINS,
    BulkControlSelector,
    BulkSelectorInfrastructureError,
    BulkSelectorResolution,
    BulkSelectorValidationError,
    InfrastructureErrorCause,
    resolve_bulk_selector,
)
from .component_api import (
    component_supports,
    get_component_caps,
    invalidate_caps,
    is_unknown_command,
)
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import (
    _SERVICE_TO_STATE,
    BLOCKED_WS_WRITE_COMMANDS,
    JSON_STRING_COERCION,
    compact_service_result,
    parse_json_param,
    parse_string_list_param,
    project_entity_record,
    wait_for_state_change,
)

# The ha_mcp_tools/call_service WS command: the first WRITE capability (Phase 3,
# issue #1813). When the component advertises ``call_service`` the consumer routes a
# single service call through this one in-process frame, which fires exactly one
# ``async_call`` and returns the REAL pre->post transition, replacing the legacy
# REST POST + hardcoded ``_SERVICE_TO_STATE`` guess + WS-subscribe verification.
# Named once so the routing helper and its tests agree on the wire string.
WS_CALL_SERVICE = "ha_mcp_tools/call_service"


class BulkControlOperation(TypedDict):
    """One entity action in a ha_bulk_control request."""

    entity_id: Annotated[
        str,
        Field(
            min_length=1,
            description="Exact Home Assistant entity ID, e.g. 'light.kitchen'.",
        ),
    ]
    action: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Device action such as 'on', 'off', or 'toggle'. For lights, use "
                "'off' instead of the ha_call_service form 'turn_off'."
            ),
        ),
    ]
    parameters: NotRequired[
        Annotated[
            dict[str, Any],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional action parameters, e.g. {'brightness_pct': 30} "
                    "when action='on'. Each domain has a fixed allowlist of "
                    "supported keys; keys outside it are ignored rather than "
                    "rejected. Use ha_call_service for parameters this tool "
                    "does not carry."
                )
            ),
        ]
    ]
    timeout_seconds: NotRequired[
        Annotated[
            float,
            Field(
                ge=0,
                allow_inf_nan=False,
                # ``strict`` for the same reason validate_first carries it:
                # lax mode coerces ``true`` to 1.0, so a bool would land
                # downstream as a silent one-second timeout.
                strict=True,
                description=(
                    "Optional confirmation timeout. On the component path, all "
                    "operations share the maximum requested wait (default 10s, "
                    "capped at 60s); 0 disables confirmation waiting."
                ),
            ),
        ]
    ]
    validate_first: NotRequired[
        Annotated[
            bool,
            Field(
                strict=True,
                description=(
                    "Report an ENTITY_NOT_FOUND failure when the target entity "
                    "does not exist; default true. On the component batch path "
                    "this is detected from the captured pre-state rather than by "
                    "preventing dispatch. The action is always validated."
                ),
            ),
        ]
    ]


# Pydantic reads this runtime config when generating the TypedDict schema. Keep it
# outside the class body because mypy permits only field declarations there.
BulkControlOperation.__pydantic_config__ = ConfigDict(extra="forbid")  # type: ignore[attr-defined]
_BULK_CONTROL_OPERATION_ADAPTER = TypeAdapter(BulkControlOperation)


def _parse_bulk_operations(operations: Any) -> list[Any]:
    """Parse explicit bulk rows while preserving per-row runtime failures."""
    try:
        parsed_operations = parse_json_param(operations, "operations")
    except ValueError as exc:
        raise_tool_error(
            create_validation_error(
                f"Invalid operations parameter: {exc}",
                parameter="operations",
                invalid_json=True,
            )
        )
    if not isinstance(parsed_operations, list):
        raise_tool_error(
            create_validation_error(
                "Operations parameter must be a list",
                parameter="operations",
                details=f"Received type: {type(parsed_operations).__name__}",
            )
        )
    operations_list: list[Any] = []
    for index, operation in enumerate(parsed_operations):
        try:
            operations_list.append(
                _BULK_CONTROL_OPERATION_ADAPTER.validate_python(operation)
            )
        except ValidationError as exc:
            # include_input=False: a malformed row can carry a lock or alarm
            # code in its `parameters`, and str(exc) would otherwise write
            # that value to persistent logs.
            logger.warning(
                "ha_bulk_control operation %d failed schema validation: %s",
                index,
                exc.errors(include_url=False, include_input=False),
            )
            # The malformed row is deliberately preserved (not dropped) so
            # the runtime batch validator downstream still reports it as a
            # per-operation failure with its own diagnostics, instead of it
            # silently vanishing from the batch.
            operations_list.append(operation)
    return operations_list


def _expand_membership_transitively(
    entity_id: str, states_by_id: dict[str, Any], *, visited: set[str] | None = None
) -> set[str]:
    """Return every entity reachable from ``entity_id`` via nested
    group/aggregate membership -- direct members and, recursively, THEIR
    members too (an outer Zone containing an inner Room group, say).

    ``visited`` guards against a membership cycle; an entity already seen
    on this walk is treated as having no further members instead of
    recursing forever. Keyword-only and defaulted to ``None`` (allocated
    internally on first call) rather than a plain mutable positional
    parameter: today every top-level caller happens to pass a fresh
    ``set()``, but a positional mutable default is the kind of thing a
    later "optimization" hoists out of a loop -- which would silently
    start returning empty member sets (everything already "visited") and
    lose conflicts instead of erroring. Best-effort, not authoritative: an
    unknown or stateless entity simply contributes no members, mirroring
    ``_find_group_member_conflicts``'s own tolerance for a batch that
    references something ``states`` doesn't have -- this is a safety-net
    scan over the small set of entities in one batch, not the selector
    resolver's own graph-integrity validation (``bulk_selector._expand_entity``),
    which is right to raise on exactly those cases when it is actually
    resolving a dispatch set.
    """
    if visited is None:
        visited = set()
    if entity_id in visited:
        return set()
    visited.add(entity_id)
    state = states_by_id.get(entity_id)
    if state is None:
        return set()
    members = normalize_member_entity_ids(state.get("attributes"))
    if not members:
        return set()
    expanded = set(members)
    for member_id in members:
        expanded.update(
            _expand_membership_transitively(member_id, states_by_id, visited=visited)
        )
    return expanded


class _GroupConflict(NamedTuple):
    """One group/aggregate entity's conflict with an operations batch.

    ``unlisted_members``: this group's members that were NOT separately
    listed in the batch -- the entities the group's own cascade will
    change WITHOUT the caller having asked for them. This is the
    actionable half of the report: the caller needs to know which
    entities their attempt to exclude something actually failed to
    protect. Preferred over ``redundant_members`` whenever non-empty.
    ``redundant_members``: this group's members that WERE also listed
    separately -- harmless (each ends up in the requested state either
    way via the group's own fan-out), but still flagged as a conflict
    since a differing per-row action or parameters races that fan-out
    rather than safely overriding it. Reported only when
    ``unlisted_members`` is empty (every member is already accounted
    for), since naming the harmless rows instead of the harmed ones is
    the wrong report even when both are non-empty.
    """

    unlisted_members: list[str]
    redundant_members: list[str]


# Every listed member beyond this many is summarized as "+N more" rather
# than enumerated: `unlisted_members` is bounded by instance topology size
# (a group's total member count), not batch size, so a large group produces
# an unbounded error message otherwise. This is a fail-closed path -- the
# remedy sentence at the end is the entire value of the error -- and
# visibility/enforcement.py scans tool-error text for hidden entity IDs,
# refusing the whole call if one appears; enumerating every member makes
# that materially more likely for no benefit once the caller already has
# the point.
_MAX_LISTED_GROUP_MEMBERS = 10


def _format_member_list(members: list[str]) -> str:
    """Render a member list for an error message, capped at
    ``_MAX_LISTED_GROUP_MEMBERS`` (see its docstring)."""
    shown = ", ".join(members[:_MAX_LISTED_GROUP_MEMBERS])
    overflow = len(members) - _MAX_LISTED_GROUP_MEMBERS
    return f"{shown} (+{overflow} more)" if overflow > 0 else shown


def _find_group_member_conflicts(
    entity_ids: set[str], states: list[Any]
) -> dict[str, _GroupConflict]:
    """Return ``{group_entity_id: _GroupConflict}`` for every group/
    aggregate entity this batch targets alongside one or more of its own
    members -- direct or nested (a batch targeting an outer group and a
    leaf reachable only through an inner group it contains is exactly as
    unsafe as targeting the inner group and that leaf directly).

    Scenes ARE checked here, unlike ``bulk_selector``'s aggregate-root
    admission (``_NON_AGGREGATE_ROOT_DOMAINS``): the two ask different
    questions. Selector mode asks "may I expand this entity's ``entity_id``
    attribute to decide what to INCLUDE" -- no for a scene, since its
    configured targets can live anywhere in the house and would wrongly
    drag them into an area-scoped dispatch. This function asks "will
    dispatching this row also change something else named in this same
    batch" -- yes for a scene: ``scene.turn_on`` applies the scene's stored
    states to every entity it configures, exactly the same batch-internal
    race a real group's cascade creates. Skipping scenes here would leave
    that race open for the one entity type whose whole purpose is fanning
    a single action out to many others.
    """
    states_by_id = {
        state["entity_id"]: state
        for state in states
        if isinstance(state, dict) and isinstance(state.get("entity_id"), str)
    }
    conflicts: dict[str, _GroupConflict] = {}
    for entity_id in sorted(entity_ids):
        transitive_members = _expand_membership_transitively(entity_id, states_by_id)
        if not transitive_members:
            continue
        redundant_members = sorted((entity_ids & transitive_members) - {entity_id})
        if not redundant_members:
            continue
        unlisted_members = sorted(transitive_members - entity_ids)
        conflicts[entity_id] = _GroupConflict(unlisted_members, redundant_members)
    return conflicts


async def _reject_operations_group_member_conflicts(
    client: HomeAssistantClient, operations: list[Any]
) -> None:
    """Fail closed when an operations-mode batch targets a group/aggregate
    entity together with one or more of its own members.

    Confirmed live (2026-08-23): Home Assistant (Hue Room/Zone groups in
    particular) fans a service call on the GROUP entity out to every member
    regardless of what else is in the same batch -- an operations batch
    that turned off a Hue Room group plus 4 of its 5 members (deliberately
    omitting the 5th, to exclude it) still turned the 5th member off too,
    purely from the group row's own cascade. Listing a member row
    separately does not shield it, so this fails the WHOLE batch closed
    (nothing dispatched) rather than silently letting the group row's
    cascade override an exclusion operations mode has no way to express.

    Fails closed on its own states-fetch failure too: an unverifiable batch
    is not a verified-safe one, and this is the same infrastructure-failure
    stance ``bulk_selector.py`` takes for the analogous selector-mode read.
    ``client.get_states()`` itself now raises ``HomeAssistantConnectionError``
    rather than swallowing a malformed response to ``[]`` (see its
    docstring in ``rest_client.py``) -- that used to be the actual fail-OPEN
    hole here: an HA hiccup silently produced an empty states list, which
    read as "verified, no conflicts" instead of "could not verify".

    A single-entity (or empty/all-malformed) batch cannot contain a
    group/member conflict by construction, so the states fetch is skipped
    entirely for it. Every batch with 2+ distinct entities still pays one
    uncached, full ``GET /states`` here regardless of whether any of them
    turn out to be a group -- this trades that fixed per-call cost for
    simplicity over fetching only the batch's own (and transitively
    reachable) entities, which would need N concurrent per-entity reads
    instead of one bulk one and is bounded by batch size rather than
    instance size. Revisit if that cost becomes a real bottleneck.
    """
    entity_ids = {
        op["entity_id"]
        for op in operations
        if isinstance(op, dict) and isinstance(op.get("entity_id"), str)
    }
    if len(entity_ids) < 2:
        return
    try:
        states = await client.get_states()
    except ToolError:
        raise
    except Exception as exc:
        exception_to_structured_error(
            exc,
            context={"operation": "bulk operations group-safety check"},
            suggestions=[
                "Could not verify this operations batch against group/"
                + "aggregate membership, so nothing was dispatched.",
                "Retry the request; this may be a transient Home Assistant "
                + "connectivity issue.",
            ],
        )
        raise  # unreachable: exception_to_structured_error always raises
    conflicts = _find_group_member_conflicts(entity_ids, states)
    if not conflicts:
        return
    # Prefer naming the members that will be silently AFFECTED (not in the
    # batch, so nothing else in the call protects them) over the ones
    # merely redundant with the group's own row -- the redundant rows are
    # harmless and not what the caller needs to fix. Falls back to naming
    # the redundant ones only when every member is already accounted for
    # (no unlisted ones exist to report).
    detail = "; ".join(
        (
            f"'{group}' will also affect {_format_member_list(conflict.unlisted_members)}, "
            "which this batch did not list"
            if conflict.unlisted_members
            else f"'{group}' is redundant with its own already-listed "
            f"member(s) {_format_member_list(conflict.redundant_members)}"
        )
        for group, conflict in sorted(conflicts.items())
    )
    # A scene conflict is still real (see _find_group_member_conflicts'
    # docstring), but selector mode cannot express "act on most of a scene
    # while excluding some" -- scene is never an aggregate root there. That
    # remedy sentence is only worth offering when at least one conflict is
    # a real, selector-expressible aggregate.
    non_scene_conflict = any(
        not any(group.startswith(f"{d}.") for d in _NON_AGGREGATE_ROOT_DOMAINS)
        for group in conflicts
    )
    selector_remedy = (
        " To act on most of a group while excluding specific members, use "
        "selector mode instead: exclude_entity_ids goes INSIDE selector, not "
        "as a top-level argument, e.g. "
        '{"selector": {"domain": "light", "area_ids": ["<area_id>"], '
        '"exclude_entity_ids": ["<entity_to_skip>"]}, "action": "off"}. '
        "area_ids/floor_ids must be exact registry IDs (call "
        "ha_list_floors_areas to look them up), not display names."
        if non_scene_conflict
        else ""
    )
    raise_tool_error(
        create_validation_error(
            "This operations batch targets a group/aggregate entity "
            f"together with one or more of its own individual members: {detail}. "
            "Home Assistant applies the action to every member when the "
            "group entity is targeted, regardless of what else is listed "
            "separately -- a member row does not protect or exempt it from "
            "the group's own action, and one absent from the batch entirely "
            "is not excluded by that absence either. Target ONLY the group, "
            "or ONLY the specific member(s) you want affected, never both "
            f"in the same call.{selector_remedy}",
            parameter="operations",
        )
    )


def _selector_only_parameter_offender(
    *,
    action: str | None,
    parameters: dict[str, Any] | None,
    timeout_seconds: float | None,
    validate_first: bool,
    dry_run: bool,
) -> str | None:
    """Name the one selector-only parameter set on an operations-mode call.

    Five distinct mistakes (action, parameters, timeout_seconds,
    validate_first, dry_run) used to share one message that always blamed
    "selector" — the one parameter that was demonstrably absent, since this
    is only reached when ``selector is None``. Naming the actual offender
    lets the caller fix the real mistake on the first try.
    """
    for name, value in (
        ("action", action),
        ("parameters", parameters),
        ("timeout_seconds", timeout_seconds),
    ):
        if value is not None:
            return name
    if validate_first is not True:
        return "validate_first"
    if dry_run:
        return "dry_run"
    return None


# action, parameters, timeout_seconds and validate_first are all declared
# fields on BulkControlOperation (see its class docstring) -- in operations
# mode they belong on each row, not as a top-level tool argument, so the
# caller's actual fix is to move the value, not delete it. Only dry_run has
# no per-row equivalent (there is no such thing as "dry-run this one row"),
# so it alone gets the remove-or-switch-modes remedy.
_PER_ROW_SELECTOR_ONLY_PARAMETERS = frozenset(
    {"action", "parameters", "timeout_seconds", "validate_first"}
)

# One representative, correctly-typed sample value per per-row parameter,
# for the worked example below. A bare "..." placeholder rendered inside a
# Python dict literal is always a quoted STRING regardless of the field's
# real type -- for timeout_seconds (a float) that example teaches the
# model the wrong shape for the exact value it is being told to copy.
_PER_ROW_PARAMETER_EXAMPLE_VALUES: dict[str, Any] = {
    "action": "on",
    "parameters": {"brightness_pct": 30},
    "timeout_seconds": 5,
    "validate_first": True,
}


def _selector_only_parameter_message(offending_parameter: str) -> str:
    """Build the remedy for one selector-only parameter used in operations mode.

    Telling the caller to remove the parameter (or abandon operations mode
    entirely) discards their intent for the four fields that DO have a
    per-row home: ``operations=[...], parameters={...}`` almost certainly
    meant "apply these parameters to my operations", and the fix is to
    move the value into each row, not delete it.
    """
    if offending_parameter in _PER_ROW_SELECTOR_ONLY_PARAMETERS:
        # Built from a dict, not a hardcoded literal: 'action' is itself one
        # of the four per-row parameters this branch handles, and a fixed
        # "..., 'action': 'on', 'action': ...}" literal would render a
        # duplicate-key example -- shown to a model that is being told to
        # copy it -- whenever action is the actual offender.
        example_row: dict[str, Any] = {"entity_id": "light.kitchen"}
        if offending_parameter != "action":
            example_row["action"] = "on"
        example_row[offending_parameter] = _PER_ROW_PARAMETER_EXAMPLE_VALUES[
            offending_parameter
        ]
        return (
            f"'{offending_parameter}' is a per-operation field in operations "
            f"mode (see BulkControlOperation), not a top-level tool argument "
            f"-- move it onto each row instead, e.g. {example_row}, "
            f"and remove the top-level '{offending_parameter}' argument."
        )
    return (
        f"'{offending_parameter}' is a selector-only parameter and cannot be "
        "used with operations. Either remove "
        f"'{offending_parameter}' from this call, or switch to selector mode "
        "by replacing 'operations' with 'selector' (+ 'action')."
    )


# Per-cause (message, suggestions) for BulkSelectorInfrastructureError (see
# its docstring in bulk_selector.py). Not every infrastructure failure is a
# network problem -- CONNECTION_FAILED's own DEFAULT_SUGGESTIONS
# (check HA is running / verify HOMEASSISTANT_URL / check network) are
# actively unhelpful for a malformed local registry row or a corrupt local
# visibility config file, so those causes get their own actionable text
# instead of inheriting the connectivity defaults.
_INFRASTRUCTURE_ERROR_SUGGESTIONS: dict[
    InfrastructureErrorCause, tuple[str, list[str]]
] = {
    InfrastructureErrorCause.CONNECTIVITY: (
        "Could not resolve the selector because Home Assistant data was unavailable",
        [
            "Check if Home Assistant is running and accessible",
            "Retry the request; this may be a transient registry read failure",
        ],
    ),
    InfrastructureErrorCause.MALFORMED_DEVICE_REGISTRY: (
        "Could not resolve the selector because Home Assistant's device "
        "registry returned a malformed entry",
        [
            "This indicates malformed data in Home Assistant's own device "
            + "registry, not a problem with the selector",
            "Check Home Assistant's logs for device-registry errors, or "
            + "restart Home Assistant if the issue persists",
        ],
    ),
    InfrastructureErrorCause.VISIBILITY_CONFIG: (
        "Could not resolve the selector because the entity visibility "
        "filter could not be evaluated safely",
        [
            "Check the Entity Visibility tab in the ha-mcp settings UI -- "
            + "entity_visibility.json may be corrupt or invalid",
            "If the config looks fine, this may be a temporary Home "
            + "Assistant registry issue -- check Home Assistant is running "
            + "and retry",
        ],
    ),
}


def _attach_resolution_to_response(
    response: dict[str, Any], resolution: BulkSelectorResolution
) -> None:
    """Attach ``resolution.summary()`` to a response, surfacing its warnings
    at the top level.

    Per AGENTS.md "Return Values", ``warnings`` is always a top-level
    ``list[str]``, never nested inside another field.
    ``resolution.summary()`` nests its own warnings (e.g. "N entities were
    hidden") under ``resolution`` for internal cohesion, so this pops them
    back out. ``response`` may already carry dispatch-time warnings (e.g.
    from ``bulk_device_control``) -- those are extended, not overwritten.
    """
    summary = resolution.summary()
    resolution_warnings = summary.pop("warnings", [])
    response["resolution"] = summary
    if resolution_warnings:
        response.setdefault("warnings", []).extend(resolution_warnings)


class _AmbiguousDispatch:
    """Sentinel type for a post-send-ambiguous component write (see below)."""


# Returned by ``_call_service_via_component`` when the component frame was SENT but
# its response/confirmation never arrived (a response-wait timeout or a post-send
# transport drop): the write MAY have landed, so the caller reports it as ``partial``
# and MUST NOT re-POST via the legacy path (D9 at-most-once — an ambiguous post-send
# outcome is never retried). Distinct from ``None`` (the component provably never
# dispatched → a safe legacy first fire).
_COMPONENT_DISPATCH_AMBIGUOUS = _AmbiguousDispatch()


def _parse_json_dict_param(
    data: str | dict[str, Any] | None,
    *,
    type_error_message: str,
) -> dict[str, Any] | None:
    if data is None:
        return None
    raw: Any = None
    try:
        raw = parse_json_param(data, "data")
    except ValueError as e:
        raise_tool_error(
            create_validation_error(
                f"Invalid data parameter: {e}",
                parameter="data",
                invalid_json=True,
            )
        )
    if raw is not None and not isinstance(raw, dict):
        raise_tool_error(
            create_validation_error(
                type_error_message,
                parameter="data",
                details=f"Received type: {type(raw).__name__}",
            )
        )
    return raw if isinstance(raw, dict) else None


def _parse_event_data(data: str | dict[str, Any] | None) -> dict[str, Any] | None:
    return _parse_json_dict_param(
        data, type_error_message="Event data must be a JSON object (dict)"
    )


logger = logging.getLogger(__name__)

# Services that produce observable state changes on entities
_STATE_CHANGING_SERVICES = {
    "turn_on",
    "turn_off",
    "toggle",
    "open",
    "close",
    "lock",
    "unlock",
    "set_temperature",
    "set_hvac_mode",
    "set_fan_mode",
    # fan.set_speed was removed in the HA percentage migration (gone in 2026.6);
    # its state-changing successors are set_percentage / set_preset_mode.
    "set_percentage",
    "set_preset_mode",
    "select_option",
    "set_value",
    "set_datetime",
    "set_cover_position",
    "set_position",
    "play_media",
    "media_play",
    "media_pause",
    "media_stop",
}

# Domains where a service call does not move the TARGET entity's own primary
# state to a value the verifier can wait for. ``scene`` belongs here with
# ``automation``/``script``: activating a scene changes the member entities,
# but the scene entity's own state is a last-activated timestamp that never
# becomes "on"/"off" — so waiting for ``turn_on`` -> "on" always times out and
# only appends a spurious "could not be verified" warning (~10s wasted).
_NON_STATE_CHANGING_DOMAINS = {
    "automation",
    "script",
    "scene",
    "homeassistant",
    "notify",
    "tts",
    "persistent_notification",
    "logbook",
    "system_log",
}

#: Emitted when a ``return_response=true`` reply is not HA's
#: ``{"changed_states": [...], "service_response": ...}`` envelope. Without it an
#: empty ``result`` reads as an affirmative "no entity states changed" rather than
#: "the records could not be separated out". Module-level so the tests that
#: pin the behaviour assert against this exact string instead of a prose fragment
#: that goes tautologically green when the wording is edited.
_NON_ENVELOPE_WARNING = (
    "Home Assistant's return_response reply did not match the expected "
    "{changed_states, service_response} envelope, so no changed-state records "
    "could be separated from the response data. An empty 'result' here does NOT "
    "mean nothing changed — the whole reply is reported under 'service_response'."
)

#: Emitted when the component served a ``return_response`` call but returned no
#: ``service_response`` key. The component only sets that key for a non-null
#: response, so an absent key is ambiguous server-side: the service may genuinely
#: have returned nothing, or the component may have discarded the response on its
#: dispatched-but-unconfirmed path. Only raised alongside ``partial`` — the case
#: where the discard is actually possible — so a plain null response stays quiet.
_COMPONENT_RESPONSE_UNCONFIRMED_WARNING = (
    "The service was dispatched but its confirmation did not arrive, and no "
    "response data came back with it. A null 'service_response' here does NOT "
    "prove the service returned nothing — the response may have been produced "
    "and lost with the confirmation. Re-read state to confirm the outcome."
)

# ``_SERVICE_TO_STATE`` (the service -> expected primary-state map) is the single
# source of truth in ``util_helpers`` — imported above and shared with the bulk
# consumer (``device_control``) so both write paths hand the component the same
# confirmation hint.


# WebSocket commands that stream events or reply in two phases (an initial ack
# then the real payload as a follow-up event) rather than resolving to a single
# terminal result. ha_call_service's ws_command escape hatch only supports
# one-shot request/response commands, so these are rejected: the "subscribe"
# substring catches subscription commands, and the set names known two-phase /
# streaming commands whose names don't contain "subscribe". The set is a floor,
# not an exhaustive list -- HA's WS command naming isn't consistent enough for a
# name check alone to be fully reliable.
_WS_COMMAND_EVENT_BLOCKLIST = frozenset(
    {
        "render_template",  # event-based; use ha_eval_template instead
        "system_health/info",  # two-phase (see tools_system._fetch_health_info)
        "assist_pipeline/run",  # streams pipeline events
    }
)

# Substrings that mark a WS command as streaming / subscription-based even when
# its name isn't in the blocklist above. Such commands ack once and then push
# follow-up events on the same id; the one-shot send_command path returns the
# ack and leaks the subscription. "subscribe" covers subscribe_* and */subscribe;
# "stream" covers history/stream, logbook/event_stream, camera/stream, ...;
# "start_preview" covers template/start_preview and the config-flow preview family.
_WS_STREAMING_SUBSTRINGS = ("subscribe", "stream", "start_preview")

# One-shot WS commands that re-enter Home Assistant's service invocation. Routing
# them through the escape hatch would bypass the service-mode guards (notably the
# reserved ha_mcp_tools domain block), so they are rejected -- use the
# domain/service parameters for service calls instead.
_WS_COMMAND_SERVICE_INVOKERS = frozenset({"call_service", "execute_script"})

# Reserved WebSocket envelope keys the transport owns. Allowing them inside data
# would let a caller override the validated command type (defeating every check
# below) or collide with the transport's message id, so they are rejected.
_WS_RESERVED_ENVELOPE_KEYS = frozenset({"type", "id"})


def _is_streaming_ws_command(command_type: str) -> bool:
    """Return True for subscription / streaming / two-phase WS commands."""
    lowered = command_type.lower()
    if lowered in _WS_COMMAND_EVENT_BLOCKLIST:
        return True
    return any(sub in lowered for sub in _WS_STREAMING_SUBSTRINGS)


def _build_service_suggestions(
    domain: str, service: str, entity_id: str | None
) -> list[str]:
    """Build common error suggestions for service call failures."""
    return [
        f"Verify {entity_id} exists using ha_get_state()"
        if entity_id
        else "Specify an entity_id for targeted service calls",
        f"Check available services for {domain} domain using ha_get_skill_guide",
        "Use ha_search() to find correct entity IDs",
    ]


class ServiceTools:
    """Service call and device operation tools for Home Assistant."""

    def __init__(self, client: HomeAssistantClient, device_tools: Any) -> None:
        self._client = client
        self._device_tools = device_tools

    @staticmethod
    def _parse_service_data(
        data: str | dict[str, Any] | None,
        entity_id: str | None,
    ) -> dict[str, Any]:
        """Parse and validate the data parameter into a service_data dict."""
        service_data: dict[str, Any] = (
            _parse_json_dict_param(
                data, type_error_message="Data parameter must be a JSON object"
            )
            or {}
        )
        if entity_id:
            service_data["entity_id"] = entity_id
        return service_data

    @staticmethod
    def _validate_service_call_params(
        domain: str | None, service: str | None
    ) -> tuple[str, str]:
        """Validate service-mode params and return the (domain, service) pair.

        Raises a structured ToolError when domain/service are missing (the caller
        likely wants the ws_command escape hatch) or when the domain targets the
        reserved ha_mcp_tools namespace.
        """
        if not domain or not service:
            raise_tool_error(
                create_validation_error(
                    "domain and service are required for a service call. To send "
                    "a raw WebSocket command instead, pass ws_command.",
                    parameter="domain" if not domain else "service",
                )
            )
        # ha_mcp_tools.* services are restricted to the ha-mcp server's dedicated
        # wrappers (which inject the required caller token). Block ha_call_service
        # from forwarding to that domain — it would otherwise be a bypass path
        # around the dedicated tools. HA core's service registry lowercases the
        # domain on fallback lookup (homeassistant/core.py
        # ServiceRegistry.async_call), so normalise here to make sure a mixed-case
        # `HA_MCP_TOOLS` can't slip past this exact-string check and still resolve
        # downstream.
        if domain.strip().lower() == "ha_mcp_tools":
            raise_tool_error(
                create_validation_error(
                    (
                        "ha_call_service cannot invoke services in the "
                        "'ha_mcp_tools' domain. Use the dedicated MCP tool "
                        "instead: ha_list_files, ha_read_file, ha_write_file, "
                        "ha_delete_file, or ha_config_set_yaml."
                    ),
                    parameter="domain",
                )
            )
        return domain, service

    @staticmethod
    def _reject_incompatible_ws_params(
        entity_id: str | None,
        return_response: bool,
        verbose: bool,
        result_fields: str | list[str] | None,
        result_attribute_keys: str | list[str] | None,
    ) -> None:
        """Reject service-mode-only params when the ws_command escape hatch is used.

        These shape a registered-service call and have no meaning for a raw
        WebSocket command; silently ignoring them would be a confusing no-op, so
        (mirroring the domain/service "not both" guard) they must be omitted.
        """
        offenders = [
            name
            for name, is_set in (
                ("entity_id", entity_id is not None),
                ("return_response", return_response),
                ("verbose", verbose),
                ("result_fields", result_fields is not None),
                ("result_attribute_keys", result_attribute_keys is not None),
            )
            if is_set
        ]
        if offenders:
            raise_tool_error(
                create_validation_error(
                    "These parameters apply only to service calls and must be "
                    f"omitted when ws_command is set: {', '.join(offenders)}.",
                    parameter="ws_command",
                )
            )

    @staticmethod
    def _parse_result_projection_params(
        result_fields: str | list[str] | None,
        result_attribute_keys: str | list[str] | None,
    ) -> tuple[list[str] | None, list[str] | None]:
        """Parse and validate result_fields / result_attribute_keys into lists.

        Raises a structured VALIDATION_INVALID_PARAMETER ToolError on malformed
        input for either parameter.
        """
        try:
            parsed_result_fields = parse_string_list_param(
                result_fields, "result_fields", allow_csv=True
            )
        except ValueError as e:
            raise_tool_error(create_validation_error(str(e), parameter="result_fields"))
        try:
            parsed_result_attribute_keys = parse_string_list_param(
                result_attribute_keys, "result_attribute_keys", allow_csv=True
            )
        except ValueError as e:
            raise_tool_error(
                create_validation_error(str(e), parameter="result_attribute_keys")
            )
        return parsed_result_fields, parsed_result_attribute_keys

    @staticmethod
    def _build_timeout_response(
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        *,
        return_response: bool = False,
    ) -> dict[str, Any]:
        """Build a partial-success response for service call timeouts.

        When ``return_response`` was requested the key is emitted here too, so a
        caller never has to branch on whether the key exists: every successful
        reply carries it. It is necessarily null — the reply never arrived — and
        that null proves nothing about what the service returned, so it comes
        with the same ambiguity warning the component's unconfirmed path uses.
        """
        response: dict[str, Any] = {
            "success": True,
            "partial": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "parameters": data,
            "message": (
                f"Service {domain}.{service} was dispatched but Home Assistant "
                f"did not respond within the timeout period. The operation is likely "
                f"still running in the background."
            ),
            "warnings": [
                "Response timed out. This is normal for long-running services "
                f"like updates or firmware installs. Use ha_get_state('{entity_id}') "
                "to check the current status."
                if entity_id
                else "Response timed out. This is normal for long-running services. "
                "The service was dispatched and may still be executing."
            ],
        }
        if return_response:
            response["service_response"] = None
            response["warnings"].append(_COMPONENT_RESPONSE_UNCONFIRMED_WARNING)
        return response

    async def _capture_initial_state(self, entity_id: str | None) -> str | None:
        """Capture the current state of an entity before a service call.

        ``entity_id`` stays optional in the signature to match the caller's
        own ``str | None`` (a service call may target zero entities); the
        one current call site only reaches this when ``should_wait`` (which
        embeds an ``entity_id is not None`` check among several AND-ed
        conditions) is true, so ``entity_id`` is always real there in
        practice -- but that invariant lives in a boolean a few lines away,
        not in a form the type checker can see through. Narrowing here
        instead of trusting the caller keeps ``get_entity_state`` (which
        genuinely requires a ``str``) honestly typed.
        """
        if entity_id is None:
            return None
        try:
            state_data = await self._client.get_entity_state(entity_id)
            return state_data.get("state") if state_data else None
        except Exception as e:
            logger.debug(
                f"Could not fetch initial state for {entity_id}: {e} — state verification may be degraded"
            )
            return None

    async def _verify_state_change(
        self,
        entity_id: str,
        service: str,
        initial_state: str | None,
        response: dict[str, Any],
    ) -> None:
        """Wait for and verify entity state change after a service call, updating response in place."""
        try:
            expected = _SERVICE_TO_STATE.get(service)
            new_state = await wait_for_state_change(
                self._client,
                entity_id,
                expected_state=expected,
                initial_state=initial_state,
                timeout=10.0,
            )
            if new_state:
                response["verified_state"] = new_state.get("state")
            else:
                response.setdefault("warnings", []).append(
                    "Service executed but state change could not be verified within timeout."
                )
        except Exception as e:
            response.setdefault("warnings", []).append(
                f"Service executed but state verification failed: {e}"
            )

    @staticmethod
    def _split_return_response_envelope(
        result: Any, *, return_response: bool
    ) -> tuple[Any, Any, bool, list[str]]:
        """Split HA's reply into (changed states, response, present, warnings).

        HA answers ``return_response=true`` with an envelope
        ``{"changed_states": [...], "service_response": ...}``. The response data
        belongs at the top level of ha_call_service's reply exactly ONCE — the
        placement the component path (``_build_component_call_response``) already
        uses — so it is peeled off here, BEFORE projection, leaving only the
        changed states to project into ``result``. Returning it in both places
        shipped it twice, byte-identical, doubling its token cost (issue #2085).

        A legitimately null ``service_response`` is still reported as present
        (``True``) so the caller emits the key.

        With ``return_response`` false there is no envelope to split: HA returns a
        plain changed-states list, so it passes through untouched and no
        ``service_response`` key is emitted (``present`` False). That is the
        overwhelming majority of calls.

        Anything else — a non-dict reply, a missing key, or a ``changed_states``
        that is not a list — means a non-conforming responder (HA core always
        sends both keys, with a list). Every such shape takes ONE path: the whole
        reply becomes the response data, ``result`` stays empty, and a warning
        says so. Uniformity is the point — peeling a recognised key out of an
        unrecognised envelope would silently discard whatever else it carried,
        and an empty ``result`` would otherwise read as an affirmative "no entity
        states changed". A caller that asked for response data must never get back
        neither the data nor an explanation.
        """
        if not return_response:
            return result, None, False, []
        if (
            isinstance(result, dict)
            and "service_response" in result
            and isinstance(result.get("changed_states"), list)
        ):
            return result["changed_states"], result["service_response"], True, []
        # Any other shape is non-conforming. Hand the WHOLE reply back as the
        # response data rather than guessing which part is which: splitting one
        # recognised key out of an unrecognised envelope discards the rest (a
        # non-list ``changed_states`` would vanish entirely) and would make the
        # warning's "the whole reply is reported under 'service_response'" a lie.
        # Projecting nothing into ``result`` keeps the records from shipping twice.
        return [], result, True, [_NON_ENVELOPE_WARNING]

    @staticmethod
    def _project_service_result(
        result: Any,
        *,
        entity_id: str | None,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> tuple[Any, list[str]]:
        """Apply compact / explicit projection to a service-call ``result``.

        Issue #1446. Precedence:

        - ``verbose=True``: bypass every transformation; return ``result`` as-is.
          (``result`` here is always changed-state records, never an envelope:
          the legacy path splits the envelope off before calling this — see
          ``_split_return_response_envelope`` — and the component path passes
          transition ``new_state``s, which are never enveloped to begin with.)
        - Explicit ``fields`` or ``attribute_keys``: apply per-record projection
          via ``project_entity_record`` to every record. No compaction; this is
          the power-user path.
        - Default: apply ``compact_service_result`` (filter to ``entity_id``
          record when single string, drop top-level metadata + heavy lists).

        Returns ``(projected, warnings)``. ``warnings`` collects per-record
        typo-guard diagnostics from ``project_entity_record`` (e.g. all-empty
        ``attribute_keys`` filter) — deduplicated so an N-record list with the
        same typo doesn't emit N copies of the same warning.
        """
        if verbose:
            return result, []
        if fields is None and attribute_keys is None:
            return compact_service_result(result, entity_id), []
        if not isinstance(result, list):
            return result, []
        warnings: list[str] = []
        # ``result_attribute_keys`` only takes effect when ``attributes`` is in
        # the projected ``result_fields`` (or ``result_fields`` is None). Surface
        # a warning rather than silently ignoring the parameter — mirrors
        # ha_get_state's attribute_keys_no_effect handling.
        if (
            attribute_keys is not None
            and fields is not None
            and "attributes" not in fields
        ):
            warnings.append(
                "result_attribute_keys was ignored because 'attributes' is not "
                "in result_fields. Add 'attributes' to result_fields (or omit "
                "result_fields) to apply result_attribute_keys."
            )
        projected: list[Any] = []
        seen_warnings: set[str] = set()
        for record in result:
            new_record, warn = project_entity_record(record, fields, attribute_keys)
            projected.append(new_record)
            if warn and warn not in seen_warnings:
                seen_warnings.add(warn)
                warnings.append(warn)
        return projected, warnings

    def _handle_connection_error(
        self,
        error: HomeAssistantConnectionError,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        return_response: bool = False,
    ) -> dict[str, Any]:
        """Handle a HomeAssistantConnectionError raised while calling a service.

        Timeouts are treated as partial success (the service was dispatched but
        Home Assistant did not respond in time) and return a partial-success
        response. Non-timeout connection errors raise a structured ToolError.
        """
        # Check if this is a timeout - for service calls, timeouts typically
        # mean the service was dispatched but HA didn't respond in time.
        # The operation is likely still running (e.g., update.install, long automations).
        if isinstance(error.__cause__, httpx.TimeoutException):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        # Non-timeout connection errors are real failures
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=_build_service_suggestions(domain, service, entity_id),
        )
        return None  # unreachable: exception_to_structured_error always raises

    @staticmethod
    def _raise_unexpected_call_service_error(
        error: Exception,
        *,
        domain: str,
        service: str,
        entity_id: str | None,
    ) -> NoReturn:
        """Raise a structured ToolError for an unexpected ha_call_service failure."""
        suggestions = _build_service_suggestions(domain, service, entity_id)
        if entity_id:
            suggestions.extend(
                [
                    f"For automation: ha_call_service('automation', 'trigger', entity_id='{entity_id}')",
                    f"For universal control: ha_call_service('homeassistant', 'toggle', entity_id='{entity_id}')",
                ]
            )
        exception_to_structured_error(
            error,
            context={
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
            },
            suggestions=suggestions,
        )

    async def _send_ws_command_mapped(
        self, command_type: str, command_params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send an arbitrary WS command, mapping a dead transport to ToolError.

        The ws_command branch returns before ``ha_call_service``'s own try
        block, so without this a transport failure raised by
        ``send_websocket_message`` (#1947) would escape the tool unstructured.
        """
        try:
            result: dict[str, Any] = await self._client.send_websocket_message(
                {"type": command_type, **command_params}
            )
            return result
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"ws_command": command_type},
                suggestions=[
                    "Check the Home Assistant connection",
                    "Retry once the WebSocket link is back",
                ],
            )
            raise  # unreachable: exception_to_structured_error always raises

    async def _call_ws_command(
        self,
        ws_command: str,
        data: str | dict[str, Any] | None,
        *,
        domain: str | None,
        service: str | None,
    ) -> dict[str, Any]:
        """Send a one-shot WebSocket command via ha_call_service's escape hatch.

        Reaches Home Assistant WebSocket commands that are not registered
        services (e.g. ``repairs/ignore_issue``). Only one-shot
        request/response commands are supported — streaming / subscription
        commands are rejected up front.
        """
        command_type = ws_command.strip()
        if domain is not None or service is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide either domain + service (a registered service call) "
                    "OR ws_command (a raw WebSocket command), not both.",
                    parameter="ws_command",
                )
            )
        if not command_type:
            raise_tool_error(
                create_validation_error(
                    "ws_command must be a non-empty WebSocket command type, "
                    "e.g. 'repairs/ignore_issue'.",
                    parameter="ws_command",
                )
            )
        if _is_streaming_ws_command(command_type):
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' is a streaming or two-phase "
                    "command; ha_call_service only sends one-shot "
                    "request/response commands. For template rendering use "
                    "ha_eval_template.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in _WS_COMMAND_SERVICE_INVOKERS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' invokes Home Assistant services "
                    "and would bypass ha_call_service's safeguards. Use the "
                    "domain/service parameters for service calls instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower().startswith("ha_mcp_tools/"):
            raise_tool_error(
                create_validation_error(
                    "ha_call_service cannot invoke 'ha_mcp_tools/*' WebSocket "
                    "commands. Use the dedicated ha-mcp tools instead.",
                    parameter="ws_command",
                )
            )
        if command_type.lower() in BLOCKED_WS_WRITE_COMMANDS:
            raise_tool_error(
                create_validation_error(
                    f"ws_command '{command_type}' mutates persistent state that a "
                    "dedicated tool guards with backups and conflict checks. Use "
                    "the corresponding ha-mcp tool (e.g. ha_config_set_dashboard, "
                    "ha_set_area_or_floor, ha_remove_entity) instead.",
                    parameter="ws_command",
                )
            )
        command_params = (
            _parse_json_dict_param(
                data, type_error_message="ws_command data must be a JSON object"
            )
            or {}
        )
        reserved = _WS_RESERVED_ENVELOPE_KEYS & command_params.keys()
        if reserved:
            raise_tool_error(
                create_validation_error(
                    "data must not contain the reserved WebSocket envelope key(s) "
                    f"{', '.join(sorted(reserved))}; the command type is set by "
                    "ws_command, and the message id is managed by the transport.",
                    parameter="data",
                )
            )
        # send_websocket_message returns a {"success": ...} dict for anything
        # HA answered with, which the result-shape check below turns into a
        # structured error. A dead transport raises instead (#1947), and this
        # branch runs BEFORE ha_call_service's own try block, so the mapping
        # has to happen here or the exception escapes the tool unstructured.
        result = await self._send_ws_command_mapped(command_type, command_params)

        if not isinstance(result, dict) or not result.get("success", False):
            error_msg = (
                result.get("error") if isinstance(result, dict) else None
            ) or "WebSocket command failed"
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    str(error_msg),
                    context={"ws_command": command_type},
                    suggestions=[
                        "Verify the command type and its parameters (e.g. "
                        + "repairs/ignore_issue needs domain, issue_id, ignore)",
                        "Confirm the target still exists (a repair must be "
                        + "present to ignore it)",
                    ],
                )
            )

        return {
            "success": True,
            "ws_command": command_type,
            "parameters": command_params or None,
            "result": result.get("result"),
            "message": f"Successfully executed WebSocket command '{command_type}'",
        }

    async def _call_service_via_component(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_ids: list[str],
        wait: bool,
        timeout: float,
        return_response: bool,
    ) -> dict[str, Any] | _AmbiguousDispatch | None:
        """Route one service call through the component ``call_service`` capability.

        Returns one of three outcomes:

        * the component's frozen result envelope — ``{domain, service, dispatched,
          confirmed, partial, transitions, service_response?}`` — when the component
          advertises ``call_service``, the frame lands, and its response arrives (the
          caller maps it and does NOT re-POST, even for a ``partial`` confirmation);
        * ``None`` when the component provably never dispatched, so the caller runs
          its legacy REST POST as a safe first fire;
        * ``_COMPONENT_DISPATCH_AMBIGUOUS`` when the frame WAS sent but its
          response/confirmation never arrived — the caller reports ``partial`` and
          does NOT re-POST.

        Verb resolution stays server-side (D6): the fully-formed ``domain`` /
        ``service`` / ``service_data`` / ``entity_ids`` are handed to the component,
        which fires exactly what it is given and never guesses a service name.

        **D9 — at-most-once (correctness-critical).** The boundary is PRE-SEND vs
        POST-SEND, NOT "error vs success":

        * PRE-SEND → ``None`` (safe legacy first fire): a capability miss, an
          ``unknown_command``, or a connection-ESTABLISHMENT failure
          (``get_websocket_client`` raising before the frame is sent) all mean the
          component never dispatched. A command-ERROR RESPONSE is also ``None``: it is
          pre-dispatch for the component's own guards (the D1 domain block and
          ``ServiceNotFound`` raise before any ``async_call``), and for a non-unknown
          command error it is the ONE documented residual — an ``async_call`` that
          mutated state and THEN raised could double-apply on the legacy re-POST
          (accepted per the approved design; no idempotency token exists anywhere in
          the write path).
        * POST-SEND → never retried: a confirmation that lapsed comes back as a
          normal result dict (``partial=True`` / ``dispatched=True``). A response-wait
          TIMEOUT (``HomeAssistantCommandTimeout`` — the frame was sent) or any
          post-send transport drop is AMBIGUOUS-dispatched: the component's
          ``@async_response`` handler is a background HA task, so the client
          abandoning the message id does NOT cancel the write, and
          ``async_call(blocking=True)`` is itself unbounded (a long ``update.install``
          / script legitimately outlives the 30s response-wait). These return the
          sentinel so the caller reports ``partial`` and MUST NOT re-POST — re-POSTing
          here is the double-fire this split exists to prevent.

        **Security (layered defense-in-depth).** The server-side reserved-domain guard
        (``_validate_service_call_params``) gates BOTH this component route AND the
        legacy REST fallback, refusing ``ha_mcp_tools`` before either runs — it is the
        authoritative single-call gate. The component's own D1 block is the
        authoritative refusal AT the component for any future consumer that reaches it
        directly; here a component D1 refusal would surface as a command-error
        response → ``None`` → legacy REST, and that REST POST is itself gated by the
        same server guard, so no ``ha_mcp_tools`` invocation can reach REST.
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "call_service"):
            return None
        # PRE-SEND: an establishment failure means the frame provably never reached
        # the component. Split into its own try so a POST-SEND failure below is never
        # misclassified as pre-send → a safe legacy first fire.
        try:
            ws = await get_websocket_client(
                url=self._client.base_url,
                token=self._client.token,
                verify_ssl=getattr(self._client, "verify_ssl", None),
            )
        except Exception as exc:
            logger.warning(
                "%s establishment failed; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        # ``send_command`` transmits the frame INSIDE itself, AFTER its readiness guard
        # and the actual socket write — so exception TYPE marks the send boundary:
        # ``HomeAssistantCommandNotSent`` is raised ONLY at the readiness guard (the one
        # provably-never-sent site → safe legacy first fire); a command-ERROR response is
        # pre-dispatch by the component's guards / the documented mutate-then-raise
        # residual → legacy; a response-wait TIMEOUT, a send() that raised (bytes may
        # already be on the socket), or a post-send transport drop (a mid-await socket
        # close raises plain ``HomeAssistantConnectionError``) is POST-SEND/AMBIGUOUS →
        # partial, never retried.
        # The confirmation HINT: the expected primary state after ``service`` (or
        # ``None`` for a service with no known primary state). The component confirms
        # only on REACHING this state — skipping a multi-phase service's intermediate
        # states / attribute-only noise — and immediate-matches an idempotent no-op; a
        # ``None`` hint keeps its any-first-event confirmation. It governs confirmation
        # TIMING only; the component still returns the REAL observed transition.
        expected_state = _SERVICE_TO_STATE.get(service)
        try:
            raw = await ws.send_command(
                WS_CALL_SERVICE,
                domain=domain,
                service=service,
                service_data=service_data,
                entity_ids=entity_ids,
                wait=wait,
                timeout=timeout,
                return_response=return_response,
                expected_state=expected_state,
            )
        except HomeAssistantCommandNotSent as exc:
            # PRE-SEND: the frame provably never left the process (the send_command
            # readiness guard — the one never-sent site). The write never happened, so
            # legacy REST is a safe first fire.
            logger.warning(
                "%s not sent; falling back to legacy: %r",
                WS_CALL_SERVICE,
                exc,
            )
            return None
        except HomeAssistantCommandError as exc:
            # unknown_command means the command vanished: invalidate the cached caps so
            # the next call re-probes. Any other command error → legacy re-POST (the
            # documented at-most-once residual, see D9 above).
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "%s command error; falling back to legacy: %r",
                    WS_CALL_SERVICE,
                    exc,
                )
            return None
        except Exception as exc:
            # HomeAssistantCommandTimeout (response-wait expired — the frame WAS sent)
            # or any post-send transport drop (e.g. a pooled-WS drop after send). The
            # component may still be lawfully mid-write, so this is ambiguous-
            # dispatched: report partial, NEVER re-POST (D9 at-most-once).
            logger.warning(
                "%s post-send timeout/drop; reporting partial (not retried): %r",
                WS_CALL_SERVICE,
                exc,
            )
            return _COMPONENT_DISPATCH_AMBIGUOUS
        result = raw.get("result")
        # A SUCCESS result frame is produced ONLY after the prep ran to completion (the
        # single async_call fired), so a malformed/unusable success envelope means the
        # write already HAPPENED. Report it AMBIGUOUS (partial, never re-POSTed) — a
        # ``None`` here would route to the legacy REST path and DOUBLE-APPLY. The
        # happy-path envelope is a dict whose ``dispatched`` is True: presence of the
        # key is not enough, since a received post-dispatch envelope whose
        # ``dispatched`` isn't True is one we cannot trust to re-fire.
        if not isinstance(result, dict) or result.get("dispatched") is not True:
            return _COMPONENT_DISPATCH_AMBIGUOUS
        return result

    @staticmethod
    def _component_verified_state(
        transitions: list[Any], entity_id: str | None
    ) -> str | None:
        """The confirmed post-state for ``entity_id`` from the component transitions.

        ``ha_call_service`` targets a single entity, so the component returns one
        transition for it. Returns the transition's ``new_state.state`` (the REAL
        post-dispatch state), or ``None`` when the entity vanished / has no state.
        """
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            if entity_id is None or transition.get("entity_id") == entity_id:
                new_state = transition.get("new_state")
                if isinstance(new_state, dict):
                    return new_state.get("state")
        return None

    def _build_component_call_response(
        self,
        component_result: dict[str, Any],
        *,
        domain: str,
        service: str,
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any]:
        """Map the component ``call_service`` result into ha_call_service's shape.

        The component's real pre->post transition replaces the WS-subscribe-and-sample
        verification (``_SERVICE_TO_STATE`` is now handed to the component as a
        confirmation-timing HINT, not read here as the returned value): the
        transition ``new_state`` records are the same ``State.as_dict()`` shape the
        legacy REST POST returns, so they feed the SAME ``_project_service_result``
        projection; the confirmed target's ``new_state.state`` becomes
        ``verified_state``; and the component's ``partial`` flag (dispatched but the
        confirming event lapsed) drives the same partial-success shape the legacy
        timeout path produces (``_build_timeout_response``).
        """
        transitions = component_result.get("transitions") or []
        # The transition new_states are State.as_dict() records — the same shape the
        # legacy REST POST returns — so the existing projection helpers apply
        # unchanged (compact filters to the target, drops metadata / heavy lists).
        new_states = [
            transition["new_state"]
            for transition in transitions
            if isinstance(transition, dict)
            and isinstance(transition.get("new_state"), dict)
        ]
        projected_result, projection_warnings = self._project_service_result(
            new_states,
            entity_id=entity_id,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
        )
        response: dict[str, Any] = {
            "success": True,
            "domain": domain,
            "service": service,
            "entity_id": entity_id,
            "parameters": data,
            "result": projected_result,
            "message": f"Successfully executed {domain}.{service}",
        }
        if projection_warnings:
            response.setdefault("warnings", []).extend(projection_warnings)
        if return_response:
            # Emit the key whenever it was requested, even for a null response —
            # the legacy path does the same (``_split_return_response_envelope``),
            # and gating on ``is not None`` made the two paths answer the same
            # call with different shapes. The component only sets the key for a
            # non-null response, so a null one arrives as an absent key here.
            response["service_response"] = component_result.get("service_response")
            # ...which makes an absent key ambiguous: genuinely-null response, or
            # one the component produced and dropped with the confirmation on its
            # dispatched-unconfirmed path. Only the latter is possible when
            # ``partial``, so warn there rather than on every null.
            if "service_response" not in component_result and component_result.get(
                "partial"
            ):
                response.setdefault("warnings", []).append(
                    _COMPONENT_RESPONSE_UNCONFIRMED_WARNING
                )
        if should_wait:
            if component_result.get("partial"):
                # Dispatched, but the confirming state_changed did not arrive within
                # the wait — the same partial-success contract the legacy timeout
                # path reports (success stays True; verification is never a failure).
                response["partial"] = True
                response.setdefault("warnings", []).append(
                    "Service executed but state change could not be verified "
                    "within timeout."
                )
            else:
                verified_state = self._component_verified_state(transitions, entity_id)
                if verified_state is not None:
                    response["verified_state"] = verified_state
        return response

    async def _maybe_component_call_service(
        self,
        *,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        entity_id: str | None,
        data: str | dict[str, Any] | None,
        should_wait: bool,
        return_response: bool,
        verbose: bool,
        fields: list[str] | None,
        attribute_keys: list[str] | None,
    ) -> dict[str, Any] | None:
        """Route a confirmable single call through the component; ``None`` → do legacy.

        The component route is taken ONLY when confirming a single entity
        (``should_wait``). The capability's entire value is the real confirmed
        pre->post transition; for a non-confirmed call (``wait=False``, no / multi
        entity, or a non-state-changing domain) the component returns
        ``transitions=[]`` → ``result:[]``, silently dropping the changed-states body
        the legacy REST POST returns (e.g. a scene's member states). For those the
        legacy single POST costs the same one round-trip and is strictly richer, so
        this returns ``None`` and the caller stays on legacy.

        Returns a FINAL ``ha_call_service`` response when the component served the
        call: the mapped transition, or — on a post-send timeout / transport drop
        (the ambiguous sentinel) — the same dispatched-but-unconfirmed ``partial`` the
        legacy timeout path builds, NEVER re-POSTed (D9 at-most-once). Returns ``None``
        when the component was not used or provably never dispatched, so the caller
        runs the legacy REST path as a safe first fire.

        ``verbose`` routes to legacy too: it promises the FULL propagation chain
        (every downstream changed state), which the component path cannot deliver — it
        returns only the confirmation targets' ``new_state``s — so the richer legacy
        POST serves it instead.
        """
        # A comma-separated entity_id ("light.a,light.b") is a valid multi-target the
        # compaction path expands, but the component confirms one LITERAL entity_id: it
        # would wait for the nonexistent literal "light.a,light.b" and report a false
        # ``partial`` with an empty result even though HA changed both real entities.
        # Treat a comma as the multi-target signal → legacy REST POST (parity with the
        # verbose / non-confirmed early-outs above, which the component cannot serve).
        if not should_wait or verbose or (entity_id and "," in entity_id):
            return None
        component_result = await self._call_service_via_component(
            domain=domain,
            service=service,
            service_data=service_data,
            entity_ids=[entity_id] if entity_id else [],
            wait=True,
            timeout=10.0,
            return_response=return_response,
        )
        if isinstance(component_result, _AmbiguousDispatch):
            return self._build_timeout_response(
                domain, service, entity_id, data, return_response=return_response
            )
        if component_result is None:
            return None
        return self._build_component_call_response(
            component_result,
            domain=domain,
            service=service,
            entity_id=entity_id,
            data=data,
            should_wait=should_wait,
            return_response=return_response,
            verbose=verbose,
            fields=fields,
            attribute_keys=attribute_keys,
        )

    @tool(
        name="ha_call_service",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Call Service",
        },
    )
    @log_tool_usage
    async def ha_call_service(
        self,
        domain: str | None = None,
        service: str | None = None,
        entity_id: str | None = None,
        data: Annotated[dict[str, Any] | None, JSON_STRING_COERCION] = None,
        return_response: bool = False,
        wait: bool = True,
        verbose: Annotated[
            bool,
            Field(
                description=(
                    "Return HA's raw changed-state records unchanged (default: "
                    "False). Use as an escape hatch when you need the full "
                    "propagation chain or raw attribute payload (debug / "
                    "inspection). With return_response=True the response data "
                    "still surfaces once as the top-level service_response key, "
                    "never nested in result. "
                    "WARNING: brings back token-bloat for nested-group targets — "
                    "prefer result_fields / result_attribute_keys for targeted control."
                ),
            ),
        ] = False,
        result_fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each record in 'result' to only these top-level keys "
                    "(e.g. ['entity_id', 'state']). Mirrors ha_get_state's fields=. "
                    "Setting this DISABLES default compaction — no entity-id filter, "
                    "no metadata strip — and applies the explicit projection instead."
                ),
            ),
        ] = None,
        result_attribute_keys: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each record's 'attributes' dict to only these keys "
                    "(e.g. ['brightness', 'rgb_color']). Mirrors ha_get_state's "
                    "attribute_keys=. Setting this DISABLES default compaction. "
                    "Requires 'attributes' to be present in result_fields (or "
                    "result_fields=None)."
                ),
            ),
        ] = None,
        ws_command: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Advanced escape hatch: send a raw one-shot Home Assistant "
                    "WebSocket command that is NOT a registered service (e.g. "
                    "'repairs/ignore_issue' to dismiss a Repairs issue). When set, "
                    "omit domain/service and the other service params; put the "
                    "command's parameters in data. Streaming/two-phase and "
                    "service-invoking commands (call_service, execute_script) are "
                    "rejected."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Execute Home Assistant services to control entities and trigger automations.

        This is the universal tool for controlling all Home Assistant entities. Services follow
        the pattern domain.service (e.g., light.turn_on, climate.set_temperature).

        **Basic Usage:**
        ```python
        # Turn on a light
        ha_call_service("light", "turn_on", entity_id="light.living_room")

        # Set temperature with parameters
        ha_call_service("climate", "set_temperature",
                      entity_id="climate.thermostat", data={"temperature": 22})

        # Trigger automation
        ha_call_service("automation", "trigger", entity_id="automation.morning_routine")

        # Universal controls work with any entity
        ha_call_service("homeassistant", "toggle", entity_id="switch.porch_light")
        ```

        **Key behavior:**
        - **wait** (default True): wait for the entity state to change before
          returning. Only applies to state-changing services on a single entity.
        - **Result compaction (default ON)**: ``result`` is trimmed
          to the targeted entity's record (drops parent-group propagation) and
          stripped of ``context`` / ``last_*`` metadata and heavy attribute
          lists (``effect_list``, ``hue_scenes``). Escape hatches: ``verbose=True``
          for the raw changed-state records, or ``result_fields`` /
          ``result_attribute_keys`` for explicit per-record projection (mirrors
          ``ha_get_state``).
        - **return_response** (default False): the service's response data is
          returned once, as the top-level ``service_response`` key — never nested
          inside ``result``, which carries the changed entity states.

        **For detailed service documentation, use ha_get_skill_guide.**

        Common patterns: Use ha_get_state() to check current values before making changes.
        Use ha_search() to find correct entity IDs.

        **WebSocket command escape hatch (advanced):**
        A few Home Assistant operations are WebSocket-only commands, not
        registered services — most notably dismissing a Repairs issue. Pass
        ``ws_command`` (instead of domain/service) to send one, with its
        parameters in ``data``:
        ```python
        # Dismiss a repair (get domain/issue_id from ha_get_overview repairs
        # or ha_get_system_health include="repairs")
        ha_call_service(ws_command="repairs/ignore_issue",
                        data={"domain": "sun", "issue_id": "abc", "ignore": True})
        ```
        Only one-shot request/response commands are supported; streaming/two-phase
        and service-invoking commands are rejected, and the other service
        parameters (entity_id, return_response, etc.) don't apply.
        """
        # WebSocket-command escape hatch (issue #1839): reach one-shot WS
        # commands that aren't registered services (e.g. repairs/ignore_issue).
        if ws_command is not None:
            self._reject_incompatible_ws_params(
                entity_id,
                return_response,
                verbose,
                result_fields,
                result_attribute_keys,
            )
            return await self._call_ws_command(
                ws_command, data, domain=domain, service=service
            )

        # Service mode requires domain + service (optional at the signature level
        # only to make room for the ws_command escape hatch) and rejects the
        # reserved ha_mcp_tools domain.
        domain, service = self._validate_service_call_params(domain, service)
        try:
            service_data = self._parse_service_data(data, entity_id)

            return_response_bool = return_response
            wait_bool = wait
            verbose_bool = verbose
            parsed_result_fields, parsed_result_attribute_keys = (
                self._parse_result_projection_params(
                    result_fields, result_attribute_keys
                )
            )

            # Determine if we should wait for state change:
            # Only for state-changing services on a single entity, not for
            # trigger/reload/fire-and-forget services or services without entities.
            # This server-side decision (D6) also chooses whether to hand the
            # component wait+entity_ids: a non-state-changing call passes wait
            # implicitly false and no confirmation targets.
            should_wait = (
                wait_bool
                and entity_id is not None
                and service in _STATE_CHANGING_SERVICES
                and domain not in _NON_STATE_CHANGING_DOMAINS
            )

            # Route a confirmable single call through the component capability (D8);
            # a returned response means it served the call, None means fall through to
            # the legacy REST path below (a safe first fire, D9 at-most-once).
            component_response = await self._maybe_component_call_service(
                domain=domain,
                service=service,
                service_data=service_data,
                entity_id=entity_id,
                data=data,
                should_wait=should_wait,
                return_response=return_response_bool,
                verbose=verbose_bool,
                fields=parsed_result_fields,
                attribute_keys=parsed_result_attribute_keys,
            )
            if component_response is not None:
                return component_response

            # Legacy REST path (component absent, or it never dispatched): capture
            # initial state before the call for the WS-subscribe verification.
            initial_state = None
            if should_wait:
                initial_state = await self._capture_initial_state(entity_id)

            result = await self._client.call_service(
                domain, service, service_data, return_response=return_response_bool
            )

            # Peel the return_response envelope apart BEFORE projection so the
            # response data is emitted once, top-level, and only the changed
            # states reach ``result`` (issue #2085).
            result, service_response, has_response_envelope, envelope_warnings = (
                self._split_return_response_envelope(
                    result, return_response=return_response_bool
                )
            )

            projected_result, projection_warnings = self._project_service_result(
                result,
                entity_id=entity_id,
                verbose=verbose_bool,
                fields=parsed_result_fields,
                attribute_keys=parsed_result_attribute_keys,
            )

            response: dict[str, Any] = {
                "success": True,
                "domain": domain,
                "service": service,
                "entity_id": entity_id,
                "parameters": data,
                "result": projected_result,
                "message": f"Successfully executed {domain}.{service}",
            }
            call_warnings = [*projection_warnings, *envelope_warnings]
            if call_warnings:
                response.setdefault("warnings", []).extend(call_warnings)

            if has_response_envelope:
                response["service_response"] = service_response

            # Wait for entity state to change
            if should_wait and entity_id is not None:
                await self._verify_state_change(
                    entity_id,
                    service,
                    initial_state,
                    response,
                )

            return response
        except HomeAssistantConnectionError as error:
            return self._handle_connection_error(
                error,
                domain=domain,
                service=service,
                entity_id=entity_id,
                data=data,
                # The parameter, not ``return_response_bool`` — that local is
                # bound inside the try and would be unbound if the error fired
                # before it.
                return_response=return_response,
            )
        except ToolError:
            raise
        except Exception as error:
            self._raise_unexpected_call_service_error(
                error, domain=domain, service=service, entity_id=entity_id
            )
            return (
                None  # unreachable: _raise_unexpected_call_service_error always raises
            )

    @tool(
        name="ha_get_operation_status",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "readOnlyHint": True,
            "title": "Get Operation Status",
        },
    )
    @log_tool_usage
    async def ha_get_operation_status(
        self,
        operation_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Single operation ID or list of operation IDs to check. "
                    "Use a single string for one operation, or a list for bulk status checks."
                ),
            ),
        ],
        timeout_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 10,
    ) -> dict[str, Any]:
        """
        Get the status of one or more device operations with real-time WebSocket verification.

        Pass a single operation_id string to check one operation, or a list of IDs
        to check multiple operations at once (bulk status).

        The timeout_seconds wait window bounds both modes. Bulk checks poll
        all operations concurrently under one shared window and report
        per-item failures inside detailed_results instead of aborting the
        batch.

        Use this to track operations initiated by ha_bulk_control or ha_call_service.
        For current entity states, use ha_get_state instead.
        """
        try:
            # JSON_STRING_COERCION turns a '["op1","op2"]' string into a list
            # before the body runs, so operation_id is already the final shape.
            if isinstance(operation_id, list):
                result = await self._device_tools.get_bulk_operation_status(
                    operation_ids=operation_id, timeout_seconds=timeout_seconds
                )
                return cast(dict[str, Any], result)
            result = await self._device_tools.get_device_operation_status(
                operation_id=operation_id, timeout_seconds=timeout_seconds
            )
            return cast(dict[str, Any], result)
        except ToolError:
            raise
        except Exception as e:
            op_context: dict[str, Any] = {"operation_id": operation_id}
            exception_to_structured_error(
                e,
                context=op_context,
                suggestions=[
                    "Verify the operation ID(s) are valid",
                    "Use ha_get_state() to check current entity states instead",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_bulk_control",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Bulk Control",
        },
    )
    @log_tool_usage
    async def ha_bulk_control(
        self,
        operations: Annotated[
            list[SkipValidation[BulkControlOperation]],
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Explicit entity operations. Use this or selector, never both. "
                    "Each item requires exact entity_id and action. Use "
                    "action='off', not service='turn_off'."
                )
            ),
            # Declared type stays `list[...]` (not `| None`) so the generated
            # tool schema shows `operations` as always a list; `cast(Any, None)`
            # supplies the runtime default without a `| None` mypy would then
            # require type-narrowing for at every use below `selector is None`.
        ] = cast(Any, None),
        parallel: bool = True,
        ctx: Context | None = None,
        selector: Annotated[
            SkipValidation[BulkControlSelector] | None,
            JSON_STRING_COERCION,
            Field(
                description=(
                    "Optional exact structural scope using domain plus area_ids "
                    "and/or floor_ids, with optional exclude_entity_ids."
                )
            ),
        ] = None,
        action: Annotated[
            str | None,
            Field(description="One device action applied to every resolved leaf."),
        ] = None,
        parameters: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(description="Optional action parameters for selector mode."),
        ] = None,
        timeout_seconds: Annotated[
            float | None,
            Field(ge=0, le=60, allow_inf_nan=False, strict=True),
        ] = None,
        validate_first: Annotated[bool, Field(strict=True)] = True,
        dry_run: Annotated[bool, Field(strict=True)] = False,
    ) -> dict[str, Any]:
        """Manage explicit operations or one deterministic structural bulk action.

        When NOT to use: use ``ha_call_service`` for service-specific payloads or
        backend-native group targeting, and ``ha_search`` for fuzzy name discovery.

        **Operations mode** (``operations``, no ``selector``): put every target in
        this one call. Parallel execution is the default, and invalid items are
        reported without aborting valid operations in the same batch — but a batch
        in which every item fails validation dispatches nothing and fails the call.
        A batch that targets a group/aggregate entity together with one or more of
        its own individual members also fails closed (nothing dispatched): Home
        Assistant applies the action to every member when the group is targeted
        regardless of what else is listed, so a member row cannot exclude that
        member from the group's own action. Use selector mode with
        ``exclude_entity_ids`` when a group action must exclude specific members.

        **Selector mode** (``selector`` + ``action``): use exact area or floor IDs
        when exclusions must be applied after recursively expanding generic
        aggregate membership. Resolves a frozen visible leaf set before dispatch;
        it is not transactional, so Home Assistant may still report per-leaf
        failures. A selector resolving to more than 100 entities
        (``MAX_SELECTOR_ENTITIES``) fails closed instead of dispatching a
        partial/oversized batch — narrow it (a more specific area/floor, or add
        ``exclude_entity_ids``) and retry. Set ``dry_run`` to preview the resolved
        set without changing state.
        """
        if operations is None and selector is None:
            raise_tool_error(
                create_validation_error(
                    "Provide exactly one of operations or selector; neither was given",
                    parameter="operations",
                )
            )
        if operations is not None and selector is not None:
            raise_tool_error(
                create_validation_error(
                    "Provide exactly one of operations or selector; both were given",
                    parameter="selector",
                )
            )

        if selector is not None:
            return await self._run_bulk_selector(
                cast(dict[str, Any], selector),
                action=action,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                validate_first=validate_first,
                dry_run=dry_run,
                parallel=parallel,
                ctx=ctx,
            )

        offending_parameter = _selector_only_parameter_offender(
            action=action,
            parameters=parameters,
            timeout_seconds=timeout_seconds,
            validate_first=validate_first,
            dry_run=dry_run,
        )
        if offending_parameter is not None:
            raise_tool_error(
                create_validation_error(
                    _selector_only_parameter_message(offending_parameter),
                    parameter=offending_parameter,
                )
            )

        operations_list = _parse_bulk_operations(operations)
        await _reject_operations_group_member_conflicts(self._client, operations_list)

        result = await self._device_tools.bulk_device_control(
            operations=operations_list, parallel=parallel, ctx=ctx
        )
        return cast(dict[str, Any], result)

    async def _run_bulk_selector(
        self,
        selector: dict[str, Any],
        *,
        action: str | None,
        parameters: dict[str, Any] | None,
        timeout_seconds: float | None,
        validate_first: bool,
        dry_run: bool,
        parallel: bool,
        ctx: Context | None,
    ) -> dict[str, Any]:
        """Resolve one structural selector and, unless ``dry_run``, dispatch it.

        Split out of ``ha_bulk_control`` to keep that tool's own McCabe
        complexity under the repo's C901 threshold (AGENTS.md — no per-file
        exemptions).
        """
        if action is None:
            raise_tool_error(
                create_validation_error(
                    "Selector mode requires action", parameter="action"
                )
            )
        try:
            parsed_selector = parse_json_param(selector, "selector")
            if not isinstance(parsed_selector, dict):
                raise BulkSelectorValidationError("selector must be a JSON object")
        except ValueError as exc:
            # BulkSelectorValidationError subclasses ValueError; catching it
            # separately alongside ValueError was redundant.
            parameter = getattr(exc, "parameter", "selector")
            raise_tool_error(create_validation_error(str(exc), parameter=parameter))
        try:
            resolution = await resolve_bulk_selector(
                self._client,
                parsed_selector,
                action=action,
                parameters=parameters,
                timeout_seconds=timeout_seconds,
                validate_first=validate_first,
            )
        except BulkSelectorValidationError as exc:
            # Caller-fixable: the selector itself is wrong. Unlike the
            # `except ValueError` above (which also catches plain ValueErrors
            # with no `.parameter`), every BulkSelectorValidationError sets
            # one (default "selector") -- no getattr fallback needed.
            raise_tool_error(create_validation_error(str(exc), parameter=exc.parameter))
        except BulkSelectorInfrastructureError as exc:
            # NOT caller-fixable by editing the selector. Routed through
            # create_connection_error (not create_validation_error) so an
            # agent doesn't try to rewrite a selector that was never the
            # problem -- but the specific next step depends on exc.cause,
            # since not every cause is actually a connectivity problem.
            logger.warning(
                "ha_bulk_control: selector resolution infrastructure failure (%s): %s",
                exc.cause,
                exc,
            )
            message, suggestions = _INFRASTRUCTURE_ERROR_SUGGESTIONS.get(
                exc.cause,
                _INFRASTRUCTURE_ERROR_SUGGESTIONS[
                    InfrastructureErrorCause.CONNECTIVITY
                ],
            )
            raise_tool_error(
                create_connection_error(
                    message, details=str(exc), suggestions=suggestions
                )
            )
        except Exception as exc:
            exception_to_structured_error(
                exc,
                context={"operation": "resolve bulk selector"},
            )
            raise  # unreachable: exception_to_structured_error always raises

        if dry_run:
            response: dict[str, Any] = {
                "success": True,
                "dry_run": True,
                "dispatched": False,
            }
            _attach_resolution_to_response(response, resolution)
            return response
        try:
            result = await self._device_tools.bulk_device_control(
                operations=resolution.operations,
                parallel=parallel,
                ctx=ctx,
            )
        except ToolError:
            # bulk_device_control already raises a fully structured ToolError
            # (e.g. "every operation failed validation" -- see its own
            # raise_tool_error call sites) -- re-raise it untouched. Without
            # this guard, the except Exception below would catch it too and
            # re-classify it via exception_to_structured_error, discarding
            # its real code/message/suggestions for a generic one.
            raise
        except Exception as exc:
            # Attach the frozen resolution to the error context: a
            # non-transactional, partially-executed bulk write must leave a
            # record of which entities were resolved and attempted, in the
            # response the agent sees, not just in whatever
            # bulk_device_control itself logged.
            logger.warning(
                "ha_bulk_control: dispatch failed after resolving %d entities: %s",
                len(resolution.resolved_entity_ids),
                exc,
            )
            exception_to_structured_error(
                exc,
                context={
                    "operation": "bulk device control",
                    "resolution": resolution.summary(),
                },
            )
            raise  # unreachable: exception_to_structured_error always raises
        response = cast(dict[str, Any], result)
        _attach_resolution_to_response(response, resolution)
        return response

    @tool(
        name="ha_call_event",
        tags={"Service & Device Control"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "title": "Call Event",
        },
    )
    @log_tool_usage
    async def ha_call_event(
        self,
        event_type: str,
        data: Annotated[dict[str, Any] | None, JSON_STRING_COERCION] = None,
    ) -> dict[str, Any]:
        """Execute a custom event on the Home Assistant event bus.

        When NOT to use: for controlling entities (lights, switches, climate) — use
        ha_call_service instead. For triggering automations by name, use
        ha_call_service("automation", "trigger").

        Use this to publish custom event types consumed by event-triggered automations,
        Node-RED flows, or custom integrations that subscribe to specific event types.

        Caveats: Events are fire-and-forget; this tool confirms the event was accepted
        by the bus but does not verify whether any automation or subscriber acted on it.
        """
        # Validate event_type before hitting the wire — empty strings or path separators
        # produce malformed URLs at POST /api/events/{event_type}.
        if not event_type or not event_type.strip():
            raise_tool_error(
                create_validation_error(
                    "event_type cannot be empty or whitespace",
                    parameter="event_type",
                )
            )
        if "/" in event_type or "\\" in event_type:
            raise_tool_error(
                create_validation_error(
                    "event_type cannot contain path separators",
                    parameter="event_type",
                    details=f"Received: {event_type!r}",
                )
            )

        parsed_data = _parse_event_data(data)

        try:
            response = await self._client.fire_event(event_type, parsed_data)
        except HomeAssistantConnectionError as error:
            if isinstance(error.__cause__, httpx.TimeoutException):
                return {
                    "success": True,
                    "partial": True,
                    "event_type": event_type,
                    "message": (
                        f"Event {event_type} was dispatched but Home Assistant "
                        "did not respond within the timeout period."
                    ),
                    "warnings": [
                        "Response timed out. The event was dispatched and may still "
                        "have been delivered to subscribers."
                    ],
                }
            exception_to_structured_error(
                error,
                context={"event_type": event_type},
                suggestions=["Check Home Assistant connection"],
            )
        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={"event_type": event_type},
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify event_type is a valid identifier",
                ],
            )

        return {
            "success": True,
            "event_type": event_type,
            "message": response.get("message", f"Event {event_type} fired."),
        }


def register_service_tools(
    mcp: Any, client: HomeAssistantClient, **kwargs: Any
) -> None:
    """Register service call and operation monitoring tools with the MCP server."""
    device_tools = kwargs.get("device_tools")
    if not device_tools:
        raise ValueError("device_tools is required for service tools registration")
    register_tool_methods(mcp, ServiceTools(client, device_tools))
