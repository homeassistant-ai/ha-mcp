"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, cast

from fastmcp import Context
from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import get_global_settings
from ..errors import create_validation_error
from ..transforms.categorized_search import DEFAULT_PINNED_TOOLS
from ..utils.fuzzy_search import apply_hidden_penalty
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import (
    add_timezone_metadata,
    build_pagination_metadata,
    filter_active_repairs,
    parse_string_list_param,
    project_fields,
    project_records,
    project_repair_fields,
    public_fields,
    result_fields_warning,
)
from .util_helpers import (
    project_entity_record as _project_entity,
)

logger = logging.getLogger(__name__)

# Configuration-body buckets the merged ``ha_search`` orchestrator collects
# from ``ha_deep_search``. ``dashboards`` is opt-in (excluded from the default
# response shape) — the orchestrator's pre-populated defaults intentionally
# omit it; this tuple is the canonical "all five" list used by the bucket-copy
# and metadata-shadow logic.
_CONFIG_BUCKETS: tuple[str, ...] = (
    "automations",
    "scripts",
    "scenes",
    "helpers",
    "dashboards",
)

# Entity sub-payload keys the orchestrator must NOT lift to the top level
# of the flat dual-surface envelope. They describe the entity-side search
# only — ``search_type`` is the entity-branch's internal mode label;
# ``domain_filter`` / ``area_filter`` / ``state_filter`` are caller-input
# echoes (the caller already has them); ``area_name`` is per-entity
# decoration that belongs inside the entity record, not at the envelope
# top; ``note`` is the redundant mode-label string. None are in
# ``_ALWAYS_KEEP_PROJECTION`` or the ``fields=`` Available keys docstring,
# so leaking them would advertise undocumented keys via the typo-guard
# while a real ``fields=`` projection silently strips them.
#
# ``by_domain`` and ``state_filter_note`` are intentionally NOT in the
# strip set — both are toggle-gated diagnostic / feature outputs
# (``group_by_domain=True`` and fuzzy+state_filter respectively) with
# observable caller value at the envelope top. Both are documented as
# top-level keys + retained in ``_ALWAYS_KEEP_PROJECTION``.
_ENTITIES_BRANCH_SKIP_KEYS: tuple[str, ...] = (
    "results",
    "total_matches",
    "has_more",
    "next_offset",
    "search_type",
    "domain_filter",
    "area_filter",
    "state_filter",
    "area_name",
    "note",
    # Redundant human-readable string echoing total_matches=0 + the
    # already-known filter — same class as ``note``. The caller has the
    # filter inputs they passed; ``total_matches`` tells them whether the
    # search hit anything.
    "message",
)

# Derived from ``_CONFIG_BUCKETS``: every bucket entry is the plural
# response-key (``automations`` etc.); the ``search_types`` token is the
# singular (drop the trailing ``s``). Deriving keeps the two lists in
# lockstep — adding a new bucket auto-extends the allowed set.
_VALID_SEARCH_TYPES: frozenset[str] = frozenset(b[:-1] for b in _CONFIG_BUCKETS)


def _validate_search_types(parsed: list[str] | None) -> None:
    """Reject unknown or empty ``search_types`` values with a structured error.

    ``parse_string_list_param`` only verifies the *shape* (string / list /
    JSON-array); it does not check values against the known set, so a typo
    like ``search_types=["frobnicate"]`` would silently return zero matches
    with no warning or partial flag. Also rejects empty list: ``[]`` pins
    branch eligibility to config-only while the response echoes the default
    type list — a silent caller / runtime / response mismatch. Centralised
    here so ``ha_search`` and ``ha_deep_search`` share the contract — adding
    a new valid type needs one change.
    """
    if parsed is None:
        return
    if not parsed:
        raise_tool_error(
            create_validation_error(
                "search_types must be non-empty if provided; omit the "
                "parameter to use the default types.",
                parameter="search_types",
            )
        )
    unknown = [t for t in parsed if t not in _VALID_SEARCH_TYPES]
    if unknown:
        raise_tool_error(
            create_validation_error(
                f"Unknown search_types: {unknown}. "
                f"Valid types: {sorted(_VALID_SEARCH_TYPES)}.",
                parameter="search_types",
            )
        )


# Top-level response keys that survive a ``fields=`` projection regardless
# of the caller's request — so a projection can never hide partial / error
# state. ``success`` and ``warnings`` are guaranteed by ``project_fields``
# itself; this set extends the protection to orchestrator-specific echoes
# (query, search_types), the error / partial diagnostics, and the
# pagination axis.
_ALWAYS_KEEP_PROJECTION: frozenset[str] = frozenset(
    {
        "query",
        "search_types",
        "entity_total_matches",
        "config_total_matches",
        "errors",
        "partial",
        "partial_reason",
        "count",
        "offset",
        "limit",
        "has_more",
        "next_offset",
        "entity_has_more",
        "entity_next_offset",
        "config_has_more",
        "config_next_offset",
        # Toggle-gated entity-branch feature output — retained so callers
        # using ``group_by_domain=True`` can pair it with ``fields=`` for
        # response shaping without losing the grouping itself.
        "by_domain",
        # Conditional diagnostic — fires under fuzzy + state_filter to
        # explain why ``entity_total_matches`` differs from ``count`` (the
        # fuzzy-engine count is unfiltered; the filter applies post-hoc).
        # Retained so a caller projecting ``fields=["results", ...]``
        # still gets the explanation.
        "state_filter_note",
        # Resolved area names matching the ``area_filter`` input (which
        # may be fuzzy, e.g. ``area_filter="kitchen"`` → matches
        # ``["Kitchen", "Kitchen Pantry"]``). Surfaces which areas the
        # search actually scanned — caller value beyond the input echo.
        "area_names",
    }
)


def _mirror_partial_to_warnings(response: dict[str, Any]) -> None:
    """Mirror ``partial_reason`` into ``warnings[]`` so agents see truncation.

    The re-review's BAT data showed agents reliably read ``warnings`` but
    commonly ignore ``partial`` / ``partial_reason`` — without mirroring,
    a config-body backend incompleteness surfaces only via the partial
    keys, which agents drop when relaying results. The mirror copies the
    reason verbatim with a leading ``"incomplete results: "`` so the
    diagnostic message lands on the channel agents actually read.
    Idempotent: re-running does not re-append the same warning.
    """
    if not response.get("partial"):
        return
    reason = response.get("partial_reason")
    if not reason:
        return
    warning_text = f"incomplete results: {reason}"
    warnings = response.setdefault("warnings", [])
    if warning_text not in warnings:
        warnings.append(warning_text)


def _project_response_fields(
    response: dict[str, Any], parsed_fields: list[str] | None
) -> dict[str, Any]:
    """Project the orchestrator response via the shared ``project_fields``
    helper, extending its always-keep set with ``_ALWAYS_KEEP_PROJECTION``
    so the diagnostic / pagination contract holds.

    Delegates the projection mechanics (parsing, typo-guard, always-keep
    retention) to ``util_helpers.project_fields`` — restoring the top-level
    ``fields=`` capability that ``ha_search_entities`` carried pre-rename,
    applied to the new flat envelope. The always-keep set means
    ``fields=["entities"]`` still leaves ``partial`` / ``errors[]`` /
    ``warnings[]`` / ``*_total_matches`` / pagination keys accessible —
    projection narrows the response but never hides incompleteness.
    """
    return project_fields(
        response, parsed_fields, extra_always_keep=_ALWAYS_KEEP_PROJECTION
    )


_INTENT_SKIP_WARNING: str = (
    "config-body search skipped: domain_filter / area_filter / "
    "state_filter signals entity-only intent; pass "
    'search_types=["automation", ...] to include config matches '
    "alongside entity results."
)


def _emit_intent_skip_warning(
    response: dict[str, Any], body_skipped_by_intent_gate: bool
) -> None:
    """Append the caller-facing warning when the entity-intent gate fires.

    Extracted from the orchestrator so the contract — gate-True ⟹ exactly
    one warning entry naming the opt-back-in mechanism — is unit-testable
    without an MCP fixture. Pre-existing warnings already in the response
    are preserved.
    """
    if body_skipped_by_intent_gate:
        response.setdefault("warnings", []).append(_INTENT_SKIP_WARNING)


def _synthesize_combined_pagination(response: dict[str, Any]) -> None:
    """Set the flat ``has_more`` / ``next_offset`` from per-surface keys.

    The flat keys give callers a "iterate normally" surface; per-surface
    keys let callers see which surface still has results. Both branches
    paginate with the same caller offset/limit, so their per-surface
    next_offsets encode the same value (offset + limit) when set —
    ``or`` picks whichever is non-None. Extracted from the orchestrator
    so the OR-synthesis is unit-testable against real code, not an
    inline simulation.
    """
    entity_has_more = bool(response.get("entity_has_more"))
    config_has_more = bool(response.get("config_has_more"))
    response["has_more"] = entity_has_more or config_has_more
    response["next_offset"] = response.get("entity_next_offset") or response.get(
        "config_next_offset"
    )


def _finalize_partial_state(
    response: dict[str, Any],
    *,
    partial_local: bool,
    errors_local: list[dict[str, str]],
) -> None:
    """Apply the orchestrator-local partial state to the response.

    Sets ``partial: True`` when a branch raised, AND extends ``errors[]``
    with the orchestrator-tagged surface errors — extending, not clobbering,
    so any payload-side errors already accumulated by
    ``_merge_payload_metadata`` survive. Extracted from the orchestrator
    so the no-clobber contract is unit-testable.
    """
    if partial_local:
        response["partial"] = True
        response["errors"].extend(errors_local)


def _compute_eligibility(
    *,
    query_text: str,
    domain_filter_text: str,
    area_filter_text: str,
    state_filter_text: str,
    explicit_config_only: bool,
) -> tuple[bool, bool, bool]:
    """Decide which sub-search branches the orchestrator should fan out to.

    Returns ``(registry_eligible, body_eligible, body_skipped_by_intent_gate)``:

    - ``registry_eligible``: the entity-registry branch runs whenever any of
      ``query`` / ``domain_filter`` / ``area_filter`` is set, except when the
      caller pinned config-only via an explicit ``search_types``.
    - ``body_eligible``: the config-body branch runs only when a ``query``
      term is set AND the caller's inputs do not signal entity-only intent.
      "Entity-only intent" = any of ``domain_filter`` / ``area_filter`` /
      ``state_filter`` is set; the caller is scoping to entities, so the
      heavy config-body search would be wasted work (BAT-verified pattern).
      The gate is overridden by an explicit ``search_types`` pin.
    - ``body_skipped_by_intent_gate``: True when ``body_eligible`` was
      flipped from True to False by the entity-intent rule (caller passed
      ``query`` + entity-filter without explicit pin). The orchestrator
      surfaces a warning in this case so callers can opt back in.
    """
    any_registry_input = bool(query_text or domain_filter_text or area_filter_text)
    registry_eligible = any_registry_input and not explicit_config_only
    entity_intent_signal = bool(
        domain_filter_text or area_filter_text or state_filter_text
    )
    body_eligible_unguarded = bool(query_text)
    body_eligible = body_eligible_unguarded and (
        explicit_config_only or not entity_intent_signal
    )
    body_skipped_by_intent_gate = (
        body_eligible_unguarded and entity_intent_signal and not explicit_config_only
    )
    return registry_eligible, body_eligible, body_skipped_by_intent_gate


def _merge_payload_metadata(
    response: dict[str, Any],
    payload: dict[str, Any],
    *,
    skip_keys: tuple[str, ...],
) -> None:
    """Shallow-merge non-conflicting metadata from a sub-helper payload into the
    orchestrator response.

    Accumulating keys — extend / OR-merge across branches so neither side's
    diagnostic data is silently dropped: ``warnings`` (list[str], extend),
    ``errors`` (list[dict], extend), ``partial`` (bool, OR),
    ``partial_reason`` (str, separator-concat with de-dup).

    Other keys use first-wins shadow-protect so orchestrator-owned fields
    (``success``, ``query``, ``search_types``, ...) survive payload echoes.
    """
    for key, value in payload.items():
        if key in skip_keys:
            continue
        if key == "warnings" and isinstance(value, list):
            current = response.get("warnings")
            if isinstance(current, list):
                current.extend(value)
            else:
                # ``warnings`` already present but not a list — that already
                # violates the top-level ``list[str]`` contract upstream.
                # Replace with the payload's well-typed list rather than
                # crash on ``.extend`` from ``setdefault`` returning the
                # broken value.
                response["warnings"] = list(value)
            continue
        if key == "errors" and isinstance(value, list):
            current = response.get("errors")
            if isinstance(current, list):
                current.extend(value)
            else:
                response["errors"] = list(value)
            continue
        if key == "partial" and isinstance(value, bool):
            response["partial"] = bool(response.get("partial")) or value
            continue
        if key == "partial_reason" and isinstance(value, str) and value:
            current = response.get("partial_reason")
            if isinstance(current, str) and current:
                if value not in current:
                    response["partial_reason"] = f"{current} ; {value}"
            else:
                response["partial_reason"] = value
            continue
        if key in response:
            continue
        response[key] = value


def _build_pagination_metadata(
    total_matches: int, offset: int, limit: int, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build standardized pagination metadata for search responses.

    Thin wrapper around the shared ``build_pagination_metadata`` helper that
    keeps the existing call-site signature (accepts a *results* list) and
    renames ``total_count`` → ``total_matches`` to match the search tools'
    response shape.
    """
    meta = build_pagination_metadata(total_matches, offset, limit, len(results))
    meta["total_matches"] = meta.pop("total_count")
    return meta


# Module-level aliases so existing call sites keep their names unchanged.
# The implementations live in util_helpers so tools_areas / tools_services
# can share them without a cross-module import.
_project_records = project_records
_result_fields_warning = result_fields_warning


async def _exact_match_search(
    client: Any,
    query: str,
    domain_filter: str | None,
    limit: int,
    offset: int = 0,
    include_hidden: bool = True,
    state_filter: str | None = None,
) -> dict[str, Any]:
    """
    Search entities by substring on entity_id + friendly_name.

    Used both as the ``exact_match=True`` primary path and as the
    fallback when fuzzy search raises. In addition to ``client.get_states()``,
    also queries the entity registry via WebSocket to identify
    ``hidden_by`` entities: by default they remain in results but
    receive a score penalty so visible matches sort first; pass
    ``include_hidden=False`` to filter them out entirely.
    """
    # Fetch states + entity registry in parallel. Registry-list failure
    # is tolerated (we just lose the hidden filter); states-fetch failure
    # is fatal — auth/connection errors must propagate so the agent sees
    # "your token is invalid" instead of "zero entities matched".
    entities_task = client.get_states()
    registry_task = client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    gather_results = await asyncio.gather(
        entities_task, registry_task, return_exceptions=True
    )
    state_result: Any = gather_results[0]
    registry_result: Any = gather_results[1]
    if isinstance(state_result, BaseException):
        raise state_result
    # CancelledError comes through gather as a captured exception even
    # when return_exceptions=True; it has to propagate or the canceller
    # waits forever.
    if isinstance(registry_result, asyncio.CancelledError):
        raise registry_result
    all_entities = state_result
    hidden_ids: set[str] = set()
    if isinstance(registry_result, dict) and registry_result.get("success"):
        for entry in registry_result.get("result", []):
            if entry.get("hidden_by") is not None:
                eid = entry.get("entity_id")
                if eid:
                    hidden_ids.add(eid)
    else:
        # Without the registry we can't tag hidden entities, so the
        # score-penalty downgrade silently doesn't apply. Log so the
        # operator can correlate "diagnostic entity ranking first" with
        # this WS hiccup instead of a code regression.
        logger.warning(
            "hidden_filter_unavailable: registry/list returned %r — "
            "hidden entities will rank without the score penalty",
            registry_result,
        )

    query_lower = query.lower().strip()

    results = []
    for entity in all_entities:
        entity_id = entity.get("entity_id", "")
        is_hidden = entity_id in hidden_ids
        if is_hidden and not include_hidden:
            continue
        attributes = entity.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        # Apply domain filter if provided
        if domain_filter and domain != domain_filter:
            continue

        # Check for exact substring match in entity_id or friendly_name
        if query_lower in entity_id.lower() or query_lower in friendly_name.lower():
            is_exact = (
                query_lower == entity_id.lower() or query_lower == friendly_name.lower()
            )
            score = 100 if is_exact else 80
            if is_hidden:
                score = apply_hidden_penalty(score, "_hidden")
            results.append(
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "domain": domain,
                    "state": entity.get("state", "unknown"),
                    "score": score,
                    "match_type": "exact_match",
                }
            )

    if state_filter:
        results = [r for r in results if r.get("state") == state_filter]

    # Sort by score descending, tie-break on entity_id for stable
    # pagination when many results share a score (visible substring
    # hits at 100, hidden ones at 80 etc).
    results.sort(key=lambda x: (-x["score"], x["entity_id"]))
    paginated = results[offset : offset + limit]
    return {
        "success": True,
        "query": query,
        **_build_pagination_metadata(len(results), offset, limit, paginated),
        "results": paginated,
        "search_type": "exact_match",
    }


def register_search_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register search and discovery tools with the MCP server."""
    smart_tools = kwargs.get("smart_tools")
    if not smart_tools:
        raise ValueError("smart_tools is required for search tools registration")

    @mcp.tool(
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Search",
        },
    )
    @log_tool_usage
    async def ha_search(
        query: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "What to search for (entity name fragment, free-text "
                    "config term, entity_id). Searches BOTH the entity "
                    "registry (entity_ids, friendly names, areas) AND "
                    "configuration bodies (automation triggers/actions, "
                    "script sequences, scene contents, helper bodies, "
                    "dashboard cards) in one call. Use this for any "
                    "find-something-in-HA question — entity OR config. "
                    "Omit `query` to enumerate by `domain_filter` and/or "
                    "`area_filter` alone (registry-listing mode); "
                    "configuration-body search is skipped in that mode "
                    "because there is no term to match against."
                ),
            ),
        ] = None,
        domain_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Narrow entity-registry results to a single domain "
                    "(e.g. 'light', 'sensor'). Does not affect configuration "
                    "search."
                ),
            ),
        ] = None,
        area_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Narrow entity-registry results to an area (id or name). "
                    "Does not affect configuration search."
                ),
            ),
        ] = None,
        search_types: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Configuration types to include in body search: "
                    "'automation', 'script', 'scene', 'helper', 'dashboard'. "
                    "Default = automation+script+scene+helper. Pass as list "
                    "or JSON-array string."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                description=(
                    "Maximum results per surface (entities, configs). Default: 10."
                ),
            ),
        ] = 10,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of results to skip for pagination.",
            ),
        ] = 0,
        exact_match: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Exact substring matching (default). Set False for "
                    "fuzzy matching when the query may have typos."
                ),
            ),
        ] = True,
        include_hidden: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Include hidden entities in registry results (with a "
                    "score penalty so they sort below visible matches). "
                    "Set False to exclude entirely."
                ),
            ),
        ] = True,
        include_config: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Include full configuration bodies in body-search "
                    "results. Default: False (summary only)."
                ),
            ),
        ] = False,
        group_by_domain: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Group entity-registry results by domain (entity-side only). "
                    "Adds a `by_domain` map to the response."
                ),
            ),
        ] = False,
        per_domain_limit: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "When `group_by_domain=True`, cap entity-registry results "
                    "per domain to this number. Ignored otherwise."
                ),
            ),
        ] = None,
        state_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Filter entity-registry results to a specific state "
                    '(e.g. "on", "off", "unavailable"). Case-insensitive.'
                ),
            ),
        ] = None,
        result_fields: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Project each entity-registry record to only the specified "
                    'keys (e.g. ["entity_id", "state"]). None = full records.'
                ),
            ),
        ] = None,
        fields: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Project the response to only the specified top-level "
                    'keys (e.g. ["entities", "automations"]). Diagnostic / '
                    "pagination keys are always retained regardless of "
                    "projection, so narrowing the response shape cannot "
                    "hide partial / error state. None = full response. "
                    "Distinct from `result_fields` (which projects each "
                    "entity record's fields). Available keys: success, "
                    "query, entities, automations, scripts, scenes, "
                    "helpers, dashboards, search_types, "
                    "entity_total_matches, config_total_matches, count, "
                    "offset, limit, has_more, next_offset, "
                    "entity_has_more, entity_next_offset, "
                    "config_has_more, config_next_offset, by_domain, "
                    "state_filter_note, area_names, warnings, errors, "
                    "partial, partial_reason."
                ),
            ),
        ] = None,
        config_time_budget: Annotated[
            float | None,
            Field(
                default=None,
                gt=0,
                le=300,
                description=(
                    "Per-call override for the per-id config-fetch wall-clock "
                    "budget (seconds). Replaces the per-type "
                    "HAMCP_*_CONFIG_TIME_BUDGET defaults for the automation, "
                    "script, AND scene branches when their bulk-fetch falls "
                    "through to per-id Attempt-C. Use when a `partial: True` "
                    "response names time-budget skipping. Stateless per-call: "
                    "one caller raising the budget doesn't affect others. "
                    "None = use the per-type env defaults."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Search for entities (lights, sensors, switches, climate, etc.) by name, domain, or area — AND inside automation/script/scene/helper/dashboard configurations — in one call.

        Searches two surfaces in parallel and returns tagged results:
          - **entities**: matches from the entity registry (entity_id,
            friendly name, area). Use `domain_filter` and/or `area_filter` to
            list/narrow. Omit `query` to enumerate entities by domain/area.
          - **automations / scripts / scenes / helpers / dashboards**: matches
            *inside* configuration definitions — triggers, actions, sequences,
            scene entity-sets, helper bodies, dashboard cards. Use `query`
            with config-body terms; filter with `search_types`.

        Eligibility:
          - Registry (entity) search runs whenever `query`, `domain_filter`,
            or `area_filter` is set, except when `search_types` is explicitly
            set (which pins to config-only).
          - Config-body search runs only when `query` is non-empty AND the
            caller's inputs do not signal entity-only intent — i.e. when
            none of `domain_filter`/`area_filter`/`state_filter` is set, OR
            when `search_types` is explicitly set as an override. The
            "filter set ⇒ skip body" rule keeps name-based single-entity
            lookups (e.g. `ha_search("bedroom motion", domain_filter=
            "binary_sensor")`) off the expensive config-body backend; pass
            `search_types=[...]` to opt back in (a warning surfaces in the
            response when the gate fires so the skip is visible).

        Use this whenever you need to find something in HA — without needing
        to decide between entity-name search vs config-body search up front.

        When NOT to use:
          - To fetch the state of a known entity_id: use `ha_get_state` (cheaper,
            no search overhead).
          - To inspect a specific automation/script/scene config by id: use the
            matching `ha_config_get_*` tool.
          - To list installed add-ons: use `ha_get_addon`.

        Caveats:
          - Both surfaces fan out in parallel; response carries `partial: True`
            plus an `errors[]` array tagged by surface ("entities" / "configs")
            when one branch raises. Empty `entities`/`automations`/... combined
            with `partial: True` means "search failed", not "no results".
          - `partial: True` is ALSO set (with `partial_reason`) when the
            config-body branch loses data on the per-type fetch paths —
            either the per-id wall-clock budget exhausts and skips
            unfetched configs, OR individual fetches raise exceptions
            (caught at debug-level so they would otherwise be silent), OR
            an `input_*` helper-type list fetch fails. Helpers run on every
            default call, so silent per-type-list failures would otherwise
            leave callers unable to tell a real zero-match from a partial
            backend outage.
          - When `partial: True` is set, the `partial_reason` text is
            also mirrored into `warnings[]` with an `"incomplete results: "`
            prefix. Agents that read `warnings` consistently (the
            entity-intent skip warning lands fine) but ignore `partial`
            still see the truncation diagnostic this way.
          - When the body branch is skipped by the entity-intent gate above,
            the response carries a `warnings[]` entry naming the skip
            reason; pass `search_types=[...]` to override.
          - The `fields=` parameter projects the response to only the
            named top-level keys; diagnostic / pagination keys
            (`success`, `warnings`, `errors`, `partial`, `partial_reason`,
            `*_total_matches`, `has_more`, `next_offset`, and per-surface
            counterparts) are always retained so projection cannot hide
            incomplete-results state. Distinct from `result_fields=`
            which projects each entity record's fields.
          - `count` is items in this response (post-pagination), not total
            matches across the corpus. Use `entity_total_matches` +
            `config_total_matches` for the totals.
          - `limit`/`offset` are applied per-surface independently. The flat
            `has_more` / `next_offset` keys describe the next caller-page
            (same offset/limit semantics as a single-surface tool — iterate
            with `offset = next_offset`). Per-surface
            `entity_has_more`/`entity_next_offset` and
            `config_has_more`/`config_next_offset` let callers see which
            surface still has results when only one of two does.

        Examples:
            - List sensors in an area: ha_search(domain_filter="sensor", area_filter="Living Room")
            - List all calendars: ha_search(domain_filter="calendar")
            - Find a light by name: ha_search("kitchen", domain_filter="light")
            - Which automations use an entity (no filter, body included): ha_search("light.bed_light")
            - Scenes touching a light: ha_search("light.kitchen", search_types=["scene"])
            - Narrow the response to only the entity bucket: ha_search("kitchen", fields=["entities"])
        """
        try:
            parsed_search_types = parse_string_list_param(search_types, "search_types")
        except ValueError as exc:
            raise_tool_error(
                create_validation_error(str(exc), parameter="search_types")
            )
        _validate_search_types(parsed_search_types)
        try:
            parsed_fields = parse_string_list_param(fields, "fields", allow_csv=True)
        except ValueError as exc:
            raise_tool_error(create_validation_error(str(exc), parameter="fields"))

        # Normalise the caller-input strings once; the eligibility helper
        # below is purely a function of normalized inputs so it stays
        # unit-testable without an MCP fixture.
        query_text = (query or "").strip()
        domain_filter_text = (domain_filter or "").strip()
        area_filter_text = (area_filter or "").strip()
        state_filter_text = (state_filter or "").strip()
        explicit_config_only = parsed_search_types is not None
        registry_eligible, body_eligible, body_skipped_by_intent_gate = (
            _compute_eligibility(
                query_text=query_text,
                domain_filter_text=domain_filter_text,
                area_filter_text=area_filter_text,
                state_filter_text=state_filter_text,
                explicit_config_only=explicit_config_only,
            )
        )

        if not registry_eligible and not body_eligible:
            raise_tool_error(
                create_validation_error(
                    "ha_search requires a non-empty query, or one of "
                    "domain_filter / area_filter to enumerate.",
                    parameter="query",
                )
            )

        registry_callable_kwargs: dict[str, Any] = {
            "query": query_text or None,
            "domain_filter": domain_filter,
            "area_filter": area_filter,
            "limit": limit,
            "offset": offset,
            "exact_match": exact_match,
            "include_hidden": include_hidden,
            "group_by_domain": group_by_domain,
            "per_domain_limit": per_domain_limit,
            "state_filter": state_filter,
            "result_fields": result_fields,
        }

        tasks: list[Any] = []
        labels: list[str] = []
        if registry_eligible:
            tasks.append(ha_search_entities(**registry_callable_kwargs))
            labels.append("entities")
        if body_eligible:
            tasks.append(
                ha_deep_search(
                    query=query_text,
                    search_types=parsed_search_types,
                    limit=limit,
                    offset=offset,
                    include_config=include_config,
                    exact_match=exact_match,
                    config_time_budget=config_time_budget,
                    ctx=ctx,
                )
            )
            labels.append("configs")

        # ``return_exceptions=True`` captures sub-task exceptions; the gather
        # call itself only raises if the orchestrator's own coroutine is
        # cancelled before the tasks complete.
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        response: dict[str, Any] = {
            "success": True,
            "query": query,
            "entities": [],
            "entity_total_matches": 0,
            "automations": [],
            "scripts": [],
            "scenes": [],
            "helpers": [],
            "search_types": parsed_search_types
            or ["automation", "script", "scene", "helper"],
            "config_total_matches": 0,
            # Pre-init the accumulating diagnostic keys so callers reading
            # ``response["errors"]`` / ``response["partial"]`` / ``response
            # ["warnings"]`` get a typed default instead of ``KeyError`` on
            # the no-error path, and so ``_merge_payload_metadata`` extends
            # rather than first-wins.
            "partial": False,
            "errors": [],
            "warnings": [],
        }
        # Surface the body-skip so a caller who actually wanted config
        # matches alongside the entity scope can see why their request
        # returned no automations / scripts / etc.
        _emit_intent_skip_warning(response, body_skipped_by_intent_gate)
        partial = False
        errors: list[dict[str, str]] = []
        for label, outcome in zip(labels, outcomes, strict=True):
            # Propagate non-Exception BaseException (CancelledError, SystemExit,
            # KeyboardInterrupt, GeneratorExit) so callers — timeouts, structured
            # concurrency, signal handlers — can react cleanly.
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, Exception
            ):
                raise outcome
            if isinstance(outcome, Exception):
                partial = True
                errors.append({"surface": label, "error": str(outcome)})
                logger.warning("ha_search %s branch failed: %r", label, outcome)
                continue
            # ha_search_entities wraps its payload via add_timezone_metadata
            # into {"data": {...}, "metadata": {...}}; ha_deep_search returns
            # the search dict directly. Unwrap once so downstream reads see
            # the same shape regardless of which helper produced it.
            payload = (
                outcome["data"]
                if isinstance(outcome, dict) and "data" in outcome
                else outcome
            )
            if label == "entities":
                response["entities"] = payload.get("results", [])
                response["entity_total_matches"] = payload.get("total_matches", 0)
                response["entity_has_more"] = bool(payload.get("has_more", False))
                response["entity_next_offset"] = payload.get("next_offset")
                # ``offset``/``limit`` are caller inputs echoed by both
                # branches with identical values — first-wins via the merge
                # is correct, no skip needed. ``has_more``/``next_offset``
                # ARE per-surface so they must be skipped (synthesized below).
                # See ``_ENTITIES_BRANCH_SKIP_KEYS`` for why the entity
                # sub-payload context keys are stripped here too.
                _merge_payload_metadata(
                    response,
                    payload,
                    skip_keys=_ENTITIES_BRANCH_SKIP_KEYS,
                )
            elif label == "configs":
                for bucket in _CONFIG_BUCKETS:
                    if bucket in payload:
                        response[bucket] = payload[bucket]
                response["config_total_matches"] = payload.get("total_matches", 0)
                response["config_has_more"] = bool(payload.get("has_more", False))
                response["config_next_offset"] = payload.get("next_offset")
                _merge_payload_metadata(
                    response,
                    payload,
                    skip_keys=(
                        *_CONFIG_BUCKETS,
                        "total_matches",
                        "has_more",
                        "next_offset",
                    ),
                )

        # ``count`` mirrors the previous ha_search_entities semantics: items
        # returned in this response (post-pagination), not total matches across
        # the corpus. Total matches live in entity_total_matches +
        # config_total_matches.
        response["count"] = len(response["entities"]) + sum(
            len(response.get(bucket, [])) for bucket in _CONFIG_BUCKETS
        )

        _synthesize_combined_pagination(response)
        _finalize_partial_state(response, partial_local=partial, errors_local=errors)
        _mirror_partial_to_warnings(response)

        return _project_response_fields(response, parsed_fields)

    async def ha_search_entities(
        query: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Entity name to search for (fuzzy or exact match). "
                    "Omit to list entities; `domain_filter` or `area_filter` "
                    "must be set in that mode."
                ),
            ),
        ] = None,
        domain_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Limit to a single domain (e.g. 'light', 'sensor', "
                    "'calendar'). Case-insensitive — values are normalized "
                    "to lowercase before matching."
                ),
            ),
        ] = None,
        area_filter: Annotated[
            str | None,
            Field(
                default=None,
                description="Limit to entities in a specific area (area ID or name).",
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                description="Maximum number of results to return (default: 10, minimum: 1)",
            ),
        ] = 10,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
        group_by_domain: bool = False,
        exact_match: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Use exact substring matching (default: True). "
                    "Set to False for fuzzy matching when the query may contain "
                    "typos or approximate terms."
                ),
            ),
        ] = True,
        include_hidden: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Include entities marked hidden_by in the entity registry "
                    "(default: True). Hidden entities still appear in results "
                    "but receive a score penalty so they sort below comparable "
                    "visible matches — typically pulling integration "
                    "diagnostics and user-suppressed entries to the bottom of "
                    "the list rather than excluding them. Set to False to "
                    "filter them out entirely."
                ),
            ),
        ] = True,
        per_domain_limit: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "When group_by_domain=True, cap results per domain to this number. "
                    "Applied after the global limit — use a high limit (e.g. limit=200) "
                    "with per_domain_limit=5 to get up to 5 entities from each domain. "
                    "Ignored when group_by_domain=False. "
                    "None = no per-domain cap (default)."
                ),
            ),
        ] = None,
        state_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Filter results to entities in a specific state "
                    '(e.g. "on", "off", "unavailable"). Case-insensitive — '
                    "input is lowercased before matching. Applied server-side after "
                    "search results are collected. For exact-match and domain-listing "
                    "searches, total_matches reflects the filtered count. For fuzzy "
                    "searches, state_filter is page-only and total_matches remains "
                    "unfiltered (see state_filter_note in the response). "
                    "None = no state filter (default)."
                ),
            ),
        ] = None,
        result_fields: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Project each entity record in results[] to only the specified keys. "
                    'E.g. ["entity_id", "state"] returns slim entity records. '
                    "None = full records (default). Unknown keys yield empty records; "
                    "omit result_fields to see all available keys. "
                    "Available keys: entity_id, friendly_name, domain, state, score, match_type."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Search for entities (lights, sensors, switches, etc.) by name, domain, or area.

        When NOT to use: for searching inside automation, script, helper, or dashboard
        *configurations* (e.g. which automations call a service or reference an entity),
        use `ha_deep_search`.

        To enumerate all entities of a domain, omit `query` and pass `domain_filter`. For
        example, `ha_search_entities(domain_filter="calendar")` lists all calendars. At
        least one of `query`, `domain_filter`, or `area_filter` must be set.
        """
        parsed_result_fields: list[str] | None = None
        if result_fields is not None:
            try:
                parsed_result_fields = parse_string_list_param(
                    result_fields, "result_fields", allow_csv=True
                )
                if parsed_result_fields is not None and len(parsed_result_fields) == 0:
                    raise ValueError("result_fields must contain at least one key")
            except ValueError as exc:
                raise_tool_error(
                    create_validation_error(str(exc), parameter="result_fields")
                )
        # Normalize omitted/None query to empty string so downstream logic is unchanged
        query = query or ""
        # HA domains are canonically lowercase, no whitespace; agents
        # that capitalize ("Lights") or pad ("  light  ") would
        # otherwise hit a silent zero-result against the prefix match
        # downstream. Strip-then-lowercase before validation so a
        # whitespace-only filter ("   ") collapses to "" and fails the
        # at-least-one-set check rather than passing it and falling
        # through to a no-op fuzzy search.
        if domain_filter:
            domain_filter = domain_filter.strip().lower()
        if area_filter:
            area_filter = area_filter.strip()
        if not query.strip() and not domain_filter and not area_filter:
            raise_tool_error(
                create_validation_error(
                    "At least one of 'query', 'domain_filter', or 'area_filter' must be set.",
                    parameter="query",
                )
            )

        try:
            group_by_domain_bool = group_by_domain
            exact_match_bool = exact_match
            include_hidden_bool = include_hidden
            per_domain_limit_int = per_domain_limit

            # Normalize state_filter — strip surrounding whitespace so
            # "on " and " on" match HA's canonical lowercase state values.
            # HA states are typically lowercase; we don't lowercase here
            # HA states are always lowercase ("on", "off", "unavailable").
            # Normalise to avoid silent zero-result surprises from "ON" / " on ".
            if state_filter is not None:
                state_filter = state_filter.strip().lower()
                # Collapse whitespace-only strings to None (no filter)
                if not state_filter:
                    state_filter = None

            # If area_filter is provided, use area-based search
            if area_filter:
                area_result = await smart_tools.get_entities_by_area(
                    area_filter,
                    group_by_domain=True,
                    include_hidden=include_hidden_bool,
                )

                # If we also have a query, filter the area results
                if query and query.strip():
                    # Collect entities from all matched areas, applying
                    # domain_filter if present. get_entities_by_area is called
                    # with group_by_domain=True above, so entities is always a
                    # dict keyed by domain. Iterate sorted area_id keys so
                    # the order matches the area_only branch.
                    all_area_entities = []
                    for area_id in sorted(area_result.get("areas", {})):
                        area_data = area_result["areas"][area_id]
                        entities = area_data.get("entities") or {}
                        if domain_filter:
                            all_area_entities.extend(entities.get(domain_filter, []))
                        else:
                            for domain_entities in entities.values():
                                all_area_entities.extend(domain_entities)

                    # Batch-fetch aliases for the surviving entity_ids so
                    # the fuzzy haystack includes them. Aliases live only
                    # in get_entries (not the slim list endpoint), so
                    # this is a single bounded round-trip on top of
                    # get_entities_by_area's calls.
                    area_entity_ids = sorted(
                        e.get("entity_id", "")
                        for e in all_area_entities
                        if e.get("entity_id")
                    )
                    aliases_map: dict[str, list[str]] = {}
                    if area_entity_ids:
                        try:
                            entries_resp = await client.send_websocket_message(
                                {
                                    "type": "config/entity_registry/get_entries",
                                    "entity_ids": area_entity_ids,
                                }
                            )
                            if isinstance(entries_resp, dict) and entries_resp.get(
                                "success"
                            ):
                                for eid, entry in (
                                    entries_resp.get("result", {}) or {}
                                ).items():
                                    if isinstance(entry, dict):
                                        aliases_map[eid] = (
                                            entry.get("aliases", []) or []
                                        )
                            else:
                                logger.warning(
                                    "alias_enrichment_failed: get_entries "
                                    "returned non-success for %d area "
                                    "entities (resp=%r)",
                                    len(area_entity_ids),
                                    entries_resp,
                                )
                        except (KeyError, TypeError, AttributeError) as alias_err:
                            logger.warning(
                                "alias_enrichment_failed: malformed payload "
                                "for %d area entities (err=%r)",
                                len(area_entity_ids),
                                alias_err,
                            )

                    # Apply fuzzy search to area entities
                    from ..utils.fuzzy_search import create_fuzzy_searcher

                    fuzzy_searcher = create_fuzzy_searcher(threshold=80)

                    entities_for_search = [
                        {
                            "entity_id": entity.get("entity_id", ""),
                            "attributes": {
                                "friendly_name": entity.get("friendly_name", "")
                            },
                            "state": entity.get("state", "unknown"),
                            "_aliases": aliases_map.get(
                                entity.get("entity_id", ""), []
                            ),
                            "_hidden_by": entity.get("_hidden_by"),
                        }
                        for entity in all_area_entities
                    ]

                    matches, total_matches = fuzzy_searcher.search_entities(
                        entities_for_search, query, limit, offset
                    )

                    # Top-level `area_filter` already carries this
                    # context for the caller; per-result echo would be
                    # redundant and asymmetric vs the other branches.
                    results = [
                        {
                            "entity_id": match["entity_id"],
                            "friendly_name": match["friendly_name"],
                            "domain": match["domain"],
                            "state": match["state"],
                            "score": match["score"],
                            "match_type": match["match_type"],
                        }
                        for match in matches
                    ]

                    if state_filter:
                        results = [r for r in results if r.get("state") == state_filter]

                    pagination = _build_pagination_metadata(
                        total_matches, offset, limit, results
                    )

                    search_data: dict[str, Any] = {
                        "success": True,
                        "query": query,
                        "area_filter": area_filter,
                        **pagination,
                        "results": results,
                        "search_type": "area_filtered_query",
                    }
                    if domain_filter:
                        search_data["domain_filter"] = domain_filter
                    if state_filter is not None:
                        search_data["state_filter"] = state_filter
                        # Area+query uses fuzzy pagination internally; state_filter
                        # is applied to the returned page, not the full dataset.
                        search_data["state_filter_note"] = (
                            "state_filter applied to this page only; "
                            "total_matches and has_more reflect the unfiltered "
                            "fuzzy-search dataset and may yield empty pages"
                        )

                    if group_by_domain_bool:
                        by_domain: dict[str, list[dict[str, Any]]] = {}
                        for item in results:
                            domain = item["domain"]
                            if domain not in by_domain:
                                by_domain[domain] = []
                            by_domain[domain].append(item)
                        if per_domain_limit_int is not None:
                            by_domain = {
                                d: entities[:per_domain_limit_int]
                                for d, entities in by_domain.items()
                            }
                        if parsed_result_fields is not None:
                            by_domain = {
                                d: _project_records(entities, parsed_result_fields)
                                for d, entities in by_domain.items()
                            }
                        search_data["by_domain"] = by_domain

                    if parsed_result_fields is not None and "results" in search_data:
                        _orig = search_data["results"]
                        search_data["results"] = _project_records(
                            _orig, parsed_result_fields
                        )
                        _warn = _result_fields_warning(
                            _orig, search_data["results"], parsed_result_fields
                        )
                        if _warn:
                            search_data.setdefault("warnings", []).append(_warn)

                    _r = await add_timezone_metadata(client, search_data)
                    return _r
                else:
                    # Just area filter, return area results with enhanced format
                    if area_result.get("areas"):
                        # Iterate ALL fuzzy-matched areas, not just the first.
                        # Pre-fix: `next(iter(...))` silently dropped every
                        # area but one — a query like area_filter="bedroom"
                        # against ["bedroom","bedroom_kids"] would return
                        # only one area's entities and miss the user's
                        # intended one entirely. Match the with-query
                        # branch by iterating all matched areas.
                        all_results: list[dict[str, Any]] = []
                        area_names_matched: list[str] = []
                        # Sort area_id keys to make iteration deterministic;
                        # the upstream `matched_area_ids` is a set, so
                        # without sorting we'd be at the mercy of CPython
                        # set-iteration order.
                        for area_id in sorted(area_result["areas"]):
                            area_data = area_result["areas"][area_id]
                            area_names_matched.append(
                                area_data.get("area_name", area_id)
                            )
                            entities_data = area_data.get("entities") or {}
                            for domain, entities in entities_data.items():
                                if domain_filter and domain != domain_filter:
                                    continue
                                # ``public_fields`` strips internal
                                # ``_aliases`` / ``_hidden_by`` enrichments
                                # before the dict crosses the public-API
                                # boundary; ``{**..., ...}`` avoids
                                # mutating dicts owned by smart_search.
                                # Score=100 baseline because area
                                # membership is exact (not fuzzy); hidden
                                # entities receive the standard penalty
                                # so they sort below visible peers within
                                # the same area.
                                all_results.extend(
                                    {
                                        **public_fields(entity),
                                        "domain": domain,
                                        "score": apply_hidden_penalty(
                                            100, entity.get("_hidden_by")
                                        ),
                                        "match_type": "area_match",
                                    }
                                    for entity in entities
                                )

                        # Re-sort so visible matches outrank penalised
                        # hidden ones; iteration order alone doesn't
                        # guarantee that for an area with mixed entities.
                        # Tie-break on entity_id so paginated requests
                        # return a stable ordering when many results
                        # share a score (every visible area_match is at
                        # 100, every hidden one at 80 — without the
                        # secondary key the page split would shift
                        # between calls).
                        all_results.sort(key=lambda x: (-x["score"], x["entity_id"]))
                        if state_filter:
                            all_results = [
                                r for r in all_results if r.get("state") == state_filter
                            ]
                        paginated = all_results[offset : offset + limit]

                        area_search_data: dict[str, Any] = {
                            "success": True,
                            "area_filter": area_filter,
                            **_build_pagination_metadata(
                                len(all_results), offset, limit, paginated
                            ),
                            "results": paginated,
                            "search_type": "area_only",
                            # `area_names` lists every matched area;
                            # `area_name` (singular) is kept for backward
                            # compatibility with existing callers — new
                            # callers should read `area_names`.
                            "area_names": area_names_matched,
                            "area_name": (
                                area_names_matched[0]
                                if area_names_matched
                                else area_filter
                            ),
                        }
                        if domain_filter:
                            area_search_data["domain_filter"] = domain_filter
                        if state_filter is not None:
                            area_search_data["state_filter"] = state_filter
                        # Mirror the empty-area branch's message when
                        # the area resolved but a domain_filter wiped
                        # out every entity in it — otherwise the caller
                        # sees total_matches=0 with no hint as to which
                        # filter caused it.
                        if not all_results and domain_filter:
                            area_search_data["message"] = (
                                f"No {domain_filter} entities found in area: "
                                f"{area_filter}"
                            )
                        if group_by_domain_bool:
                            # Group the paginated slice (not all_results) so
                            # by_domain and results stay in sync.
                            paginated_by_domain: dict[str, list[dict[str, Any]]] = {}
                            for entity in paginated:
                                paginated_by_domain.setdefault(
                                    entity["domain"], []
                                ).append(entity)
                            if per_domain_limit_int is not None:
                                paginated_by_domain = {
                                    d: entities[:per_domain_limit_int]
                                    for d, entities in paginated_by_domain.items()
                                }
                            if parsed_result_fields is not None:
                                paginated_by_domain = {
                                    d: _project_records(entities, parsed_result_fields)
                                    for d, entities in paginated_by_domain.items()
                                }
                            area_search_data["by_domain"] = paginated_by_domain
                        if (
                            parsed_result_fields is not None
                            and "results" in area_search_data
                        ):
                            _orig = area_search_data["results"]
                            area_search_data["results"] = _project_records(
                                _orig, parsed_result_fields
                            )
                            _warn = _result_fields_warning(
                                _orig, area_search_data["results"], parsed_result_fields
                            )
                            if _warn:
                                area_search_data.setdefault("warnings", []).append(
                                    _warn
                                )
                        _r = await add_timezone_metadata(client, area_search_data)
                        return _r
                    else:
                        # Empty match: still emit `area_names: []` so
                        # callers don't KeyError when they read the
                        # field on a zero-match response. Symmetry with
                        # the populated branch.
                        empty_area_data: dict[str, Any] = {
                            "success": True,
                            "area_filter": area_filter,
                            **_build_pagination_metadata(0, offset, limit, []),
                            "results": [],
                            "search_type": "area_only",
                            "area_names": [],
                            "message": f"No entities found in area: {area_filter}",
                        }
                        if domain_filter:
                            empty_area_data["domain_filter"] = domain_filter
                        if state_filter is not None:
                            empty_area_data["state_filter"] = state_filter
                        if group_by_domain_bool:
                            empty_area_data["by_domain"] = {}
                        _r = await add_timezone_metadata(client, empty_area_data)
                        return _r

            # Regular entity search (no area filter)
            # Handle empty query with domain_filter - list all entities of that domain
            if domain_filter and (not query or not query.strip()):
                # Fetch states + registry list in parallel. Registry-list
                # failure is tolerated (we just lose the hidden filter);
                # states-fetch failure is fatal — auth/connection errors
                # must propagate so the agent sees the real cause instead
                # of silently ranked-zero results.
                states_task = client.get_states()
                registry_task = client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
                gather_results = await asyncio.gather(
                    states_task, registry_task, return_exceptions=True
                )
                states_result: Any = gather_results[0]
                registry_result: Any = gather_results[1]
                if isinstance(states_result, BaseException):
                    raise states_result
                # CancelledError must propagate; gather captures it like
                # any other exception when return_exceptions=True.
                if isinstance(registry_result, asyncio.CancelledError):
                    raise registry_result
                all_entities = states_result
                hidden_ids: set[str] = set()
                if isinstance(registry_result, dict) and registry_result.get("success"):
                    for entry in registry_result.get("result", []):
                        if entry.get("hidden_by") is not None:
                            eid = entry.get("entity_id")
                            if eid:
                                hidden_ids.add(eid)
                else:
                    logger.warning(
                        "hidden_filter_unavailable: registry/list returned "
                        "%r — hidden entities in domain_listing will rank "
                        "without the score penalty",
                        registry_result,
                    )

                # Filter by domain. Hidden entities are kept by default
                # (with score penalty applied below); ``include_hidden=False``
                # filters them out entirely.
                filtered_entities = [
                    e
                    for e in all_entities
                    if e.get("entity_id", "").startswith(f"{domain_filter}.")
                    and (include_hidden_bool or e.get("entity_id") not in hidden_ids)
                ]

                # Score: 100 baseline for domain membership (exact, not
                # fuzzy); penalised for hidden entries so they sort below
                # visible peers within the same domain.
                scored_entities = []
                for entity in filtered_entities:
                    entity_id = entity.get("entity_id", "")
                    attributes = entity.get("attributes", {})
                    score = apply_hidden_penalty(
                        100, "_hidden" if entity_id in hidden_ids else None
                    )
                    scored_entities.append(
                        {
                            "entity_id": entity_id,
                            "friendly_name": attributes.get("friendly_name", entity_id),
                            "domain": domain_filter,
                            "state": entity.get("state", "unknown"),
                            "score": score,
                            "match_type": "domain_listing",
                        }
                    )
                # Tie-break on entity_id for stable pagination — every
                # visible domain entry scores 100 and every hidden one
                # scores 80, so sorting by score alone leaves the
                # within-tier ordering up to dict iteration.
                scored_entities.sort(key=lambda x: (-x["score"], x["entity_id"]))
                if state_filter:
                    scored_entities = [
                        e for e in scored_entities if e.get("state") == state_filter
                    ]
                results = scored_entities[offset : offset + limit]

                domain_list_data: dict[str, Any] = {
                    "success": True,
                    "query": query,
                    "domain_filter": domain_filter,
                    **_build_pagination_metadata(
                        len(scored_entities), offset, limit, results
                    ),
                    "results": results,
                    "search_type": "domain_listing",
                    "note": f"Listing all {domain_filter} entities (empty query with domain_filter)",
                }
                if state_filter is not None:
                    domain_list_data["state_filter"] = state_filter
                if parsed_result_fields is not None:
                    _orig = results
                    domain_list_data["results"] = _project_records(
                        _orig, parsed_result_fields
                    )
                    _warn = _result_fields_warning(
                        _orig, domain_list_data["results"], parsed_result_fields
                    )
                    if _warn:
                        domain_list_data.setdefault("warnings", []).append(_warn)
                if group_by_domain_bool:
                    domain_list_results = (
                        results[:per_domain_limit_int]
                        if per_domain_limit_int is not None
                        else results
                    )
                    if parsed_result_fields is not None:
                        domain_list_results = _project_records(
                            domain_list_results, parsed_result_fields
                        )
                    domain_list_data["by_domain"] = {domain_filter: domain_list_results}
                _r = await add_timezone_metadata(client, domain_list_data)
                return _r

            # Search strategy depends on exact_match setting:
            # - exact_match=True: substring match
            # - exact_match=False: fuzzy first, fall back to substring on failure
            #
            # If both real strategies fail we propagate the exception so
            # callers see why; we deliberately do NOT fall back to a
            # zero-scored entity dump (a clean error is strictly more
            # useful to an agent than a noise pile flagged
            # `partial: True`).

            result: dict[str, Any]
            warning: str | None = None
            search_type = "exact_match" if exact_match_bool else "fuzzy_search"

            if exact_match_bool:
                # Exact match mode: substring matching only. No fallback —
                # _exact_match_search only fails when client.get_states()
                # itself fails, in which case any further retry is futile.
                result = await _exact_match_search(
                    client,
                    query,
                    domain_filter,
                    limit,
                    offset,
                    include_hidden=include_hidden_bool,
                    state_filter=state_filter,
                )
                search_type = "exact_match"
            else:
                # Fuzzy mode: BM25 → substring fallback on exception only.
                try:
                    result = await smart_tools.smart_entity_search(
                        query,
                        limit,
                        offset=offset,
                        domain_filter=domain_filter,
                        include_hidden=include_hidden_bool,
                    )
                    search_type = "fuzzy_search"
                except asyncio.CancelledError:
                    raise
                except ToolError:
                    # Auth/connection/structured failures must propagate; the
                    # substring fallback below is for fuzzy-engine bugs only.
                    raise
                except Exception as fuzzy_error:
                    logger.warning(
                        f"Fuzzy search failed, falling back to substring "
                        f"match: {fuzzy_error}"
                    )
                    result = await _exact_match_search(
                        client,
                        query,
                        domain_filter,
                        limit,
                        offset,
                        include_hidden=include_hidden_bool,
                        state_filter=state_filter,
                    )
                    warning = "Fuzzy search unavailable, using substring match"
                    search_type = "exact_match"

            # Convert 'matches' to 'results' for backward compatibility
            if "matches" in result:
                result["results"] = result.pop("matches")

            # Remove legacy is_truncated if present (replaced by has_more)
            result.pop("is_truncated", None)

            # Add domain_filter to result if it was provided (for API consistency)
            if domain_filter:
                result["domain_filter"] = domain_filter

            # Ensure pagination metadata exists in result
            result.setdefault("offset", offset)
            result.setdefault("limit", limit)
            result.setdefault("count", len(result.get("results", [])))
            if "has_more" not in result:
                total = result.get("total_matches", 0)
                result["has_more"] = (result["offset"] + result["count"]) < total
                result["next_offset"] = (
                    result["offset"] + limit if result["has_more"] else None
                )

            # Apply state_filter to fuzzy results BEFORE grouping so by_domain stays
            # consistent with results[].
            # Note: for fuzzy_search, state_filter is page-only — smart_entity_search
            # already paginated internally, so we cannot know the pre-filter total.
            # total_matches and has_more reflect the unfiltered fuzzy-search dataset;
            # only count is updated to match the filtered page.
            if state_filter and "results" in result and search_type == "fuzzy_search":
                filtered = [
                    r for r in result["results"] if r.get("state") == state_filter
                ]
                result["results"] = filtered
                result["count"] = len(filtered)
                # Signal that state_filter is page-only for fuzzy mode.
                # total_matches and has_more/next_offset reflect the unfiltered
                # fuzzy dataset — subsequent pages may also come back empty if
                # no entities on that page match the state filter.
                result["state_filter_note"] = (
                    "state_filter applied to this page only; "
                    "total_matches and has_more reflect the unfiltered "
                    "fuzzy-search dataset and may yield empty pages"
                )

            # Group by domain if requested (built from already-filtered results)
            if group_by_domain_bool and "results" in result:
                by_domain = {}
                for entity in result["results"]:
                    domain = entity.get("domain", entity["entity_id"].split(".")[0])
                    if domain not in by_domain:
                        by_domain[domain] = []
                    by_domain[domain].append(entity)
                if per_domain_limit_int is not None:
                    by_domain = {
                        d: entities[:per_domain_limit_int]
                        for d, entities in by_domain.items()
                    }
                result["by_domain"] = by_domain

            result["search_type"] = search_type

            # Echo state_filter in response so callers can see what filter was applied.
            # Gate on ``is not None`` (not truthy) so an empty-string or
            # falsy-but-intentional value is still reflected in the response.
            # (state_filter=None means no filter was requested — omit in that case.)
            if state_filter is not None:
                result["state_filter"] = state_filter

            # Add warning and partial flag if fallback was used
            if warning:
                result.setdefault("warnings", []).append(warning)
                result["partial"] = True

            # Apply per-record projection to results and by_domain
            if parsed_result_fields is not None and "results" in result:
                _orig = result["results"]
                result["results"] = _project_records(_orig, parsed_result_fields)
                _warn = _result_fields_warning(
                    _orig, result["results"], parsed_result_fields
                )
                if _warn:
                    result.setdefault("warnings", []).append(_warn)
            if parsed_result_fields is not None and "by_domain" in result:
                result["by_domain"] = {
                    d: _project_records(entities, parsed_result_fields)
                    for d, entities in result["by_domain"].items()
                }

            _r = await add_timezone_metadata(client, result)
            return _r

        except ToolError:
            raise
        except ValueError as e:
            # ValueError from param validation — surface as VALIDATION_FAILED
            # with the original message and NO generic operational
            # suggestions (those would just be misleading boilerplate
            # next to an unrelated message like "limit must be at least
            # 1, got 0").
            raise_tool_error(
                create_validation_error(
                    str(e),
                    context={
                        "query": query,
                        "domain_filter": domain_filter,
                        "area_filter": area_filter,
                    },
                )
            )
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "domain_filter": domain_filter,
                    "area_filter": area_filter,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                    "Check area/domain filter spelling",
                ],
            )
            return None  # unreachable: error helpers above always raise
        return None  # py/mixed-returns: explicit terminal; error handlers above always raise (NoReturn), unreachable

    @mcp.tool(
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get System Overview",
        },
    )
    @log_tool_usage
    async def ha_get_overview(
        detail_level: Annotated[
            Literal["minimal", "standard", "full"],
            Field(
                default="minimal",
                description=(
                    "'minimal': 10 entities/domain, top-5 states (default); "
                    "'standard': 200 entities/page, top-10 states (use offset for more); "
                    "'full': 200 entities/page + entity_id + state + full states. "
                    "Use 'domains', 'limit', or max_entities_per_domain to control size"
                ),
            ),
        ] = "minimal",
        domains: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Filter to specific domains (e.g. 'light,sensor' or ['light','sensor']). "
                    "None = all domains. Useful to avoid context window overload."
                ),
            ),
        ] = None,
        limit: Annotated[
            int | None,
            Field(
                default=None,
                ge=1,
                description=(
                    "Max total entities across all domains (default: unlimited for minimal, "
                    "200 for standard/full). Counts and states always complete. "
                    "Use with offset for pagination."
                ),
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of entities to skip for pagination (default: 0)",
            ),
        ] = 0,
        max_entities_per_domain: Annotated[
            int | None,
            Field(
                default=None,
                description="Override default entity cap per domain (minimal=10, standard/full=unlimited). 0 = no limit on entities or states.",
            ),
        ] = None,
        include_state: Annotated[
            bool | None,
            Field(
                default=None,
                description="Include state field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
        include_entity_id: Annotated[
            bool | None,
            Field(
                default=None,
                description="Include entity_id field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
        include_notifications: Annotated[
            bool | None,
            Field(
                default=True,
                description="Include active persistent notifications (default: True). Set False to skip.",
            ),
        ] = True,
        include_dismissed_repairs: Annotated[
            bool | None,
            Field(
                default=False,
                description=(
                    "Include user-dismissed/ignored repairs (default: False). "
                    "Matches the HA Repairs UI which hides dismissed items by default."
                ),
            ),
        ] = False,
        fields: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level response keys to reduce "
                    'response size (e.g. ["system_info", "domains"]). '
                    "None = full response (default). "
                    "Available keys: success, system_summary, domain_stats, "
                    "area_analysis, ai_insights, pagination, partial, warnings, "
                    "device_types, service_availability, system_info, "
                    "notification_count, notifications, repair_count, "
                    "dismissed_repair_count, repairs, repairs_error, "
                    "tool_discovery, settings_url, settings_url_hint. Note: "
                    "``settings_url`` (stdio mode) and ``settings_url_hint`` "
                    "(HTTP/Docker/OAuth mode) are emitted regardless of "
                    "``fields=`` projection so the settings page stays "
                    "discoverable; see the tool description."
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get AI-friendly system overview with intelligent categorization.

        Returns comprehensive system information at the requested detail level,
        including Home Assistant base_url, version, location, timezone, entity overview,
        and active persistent notifications (if any).
        Use 'minimal' (default) for most queries. Domain counts and states_summary
        are always complete regardless of entity pagination.
        Standard/full modes paginate entities (default 200 per page) — use offset
        to fetch more. Use 'domains' filter to narrow scope.

        Use fields= to project the response to only the keys you need — a
        significantly smaller payload when fetching a single sub-section (e.g.
        fields=["system_info"] returns just that section instead of the full overview).

        When (and only when) the ha-mcp settings-UI sidecar is running
        (stdio mode, e.g. Claude Desktop / Claude Code), the response
        includes a ``settings_url`` field — the local URL to the
        tool-configuration page. Hand this URL to the user when they
        ask how to enable or disable tools or change server settings.
        ``settings_url`` is emitted regardless of ``fields=``
        projection (so it stays discoverable even when callers
        minimize the response) but only when the sidecar URL file
        actually exists.

        In HTTP / Docker / OAuth modes there is no sidecar URL file and the
        server can't know its externally reachable host, so the response
        instead carries a ``settings_url_hint`` string telling the user where
        the page is mounted and to read the full URL from the startup logs.
        Hand whichever of the two fields is present to the user.
        """
        # Validate fields= early so a malformed value returns VALIDATION_FAILED
        # with parameter="fields" (ha_get_overview has no outer try/except, so
        # a raw ValueError would escape uncaught).
        parsed_fields: list[str] | None = None
        if fields is not None:
            try:
                parsed_fields = parse_string_list_param(
                    fields, "fields", allow_csv=True
                )
            except ValueError as exc:
                raise_tool_error(create_validation_error(str(exc), parameter="fields"))

        include_state_bool = include_state
        include_entity_id_bool = include_entity_id
        include_notifications_bool = (
            include_notifications if include_notifications is not None else True
        )
        include_dismissed_repairs_bool = bool(include_dismissed_repairs)

        # Parse domains filter
        parsed_domains = parse_string_list_param(domains, "domains", allow_csv=True)

        # Parse pagination parameters
        limit_int = limit
        offset_int = offset

        result = await smart_tools.get_system_overview(
            detail_level,
            max_entities_per_domain,
            include_state_bool,
            include_entity_id_bool,
            domains_filter=parsed_domains,
            limit=limit_int,
            offset=offset_int,
        )
        result = cast(dict[str, Any], result)

        # Include system info - essential fields always, full details at "full" level
        try:
            config = await client.get_config()
            system_info: dict[str, Any] = {
                "base_url": client.base_url,
                "version": config.get("version"),
                "location_name": config.get("location_name"),
                "time_zone": config.get("time_zone"),
                "language": config.get("language"),
                "state": config.get("state"),
            }
            # Full detail level adds extended system info
            if detail_level == "full":
                system_info.update(
                    {
                        "country": config.get("country"),
                        "currency": config.get("currency"),
                        "unit_system": config.get("unit_system", {}),
                        "latitude": config.get("latitude"),
                        "longitude": config.get("longitude"),
                        "elevation": config.get("elevation"),
                        "components_loaded": len(config.get("components", [])),
                        "safe_mode": config.get("safe_mode", False),
                        "internal_url": config.get("internal_url"),
                        "external_url": config.get("external_url"),
                        # No default: distinguish HA-not-exposing-the-key (None)
                        # from empty-allowlist ([]) — security-relevant for agents.
                        "allowlist_external_dirs": config.get(
                            "allowlist_external_dirs"
                        ),
                    }
                )
            result["system_info"] = system_info
            # Enrich system_summary with HA version (config already fetched above).
            # Use `or "unknown"` so a None version (HA omitting the key) still
            # surfaces a sentinel value rather than null.
            if "system_summary" in result:
                result["system_summary"]["version"] = config.get("version") or "unknown"
        except Exception as e:
            logger.warning(
                "Failed to fetch system info for overview: %s", e, exc_info=True
            )
            # Config fetch failed — populate version sentinel so system_summary
            # always has a "version" key regardless of connection state.
            if "system_summary" in result:
                result["system_summary"].setdefault("version", "unknown")

        # Include active persistent notifications. ``notifications`` is
        # advertised in the ``fields=`` docstring as an available key,
        # so it must be present whenever ``include_notifications`` is on
        # — even if the list comes back empty — so ``fields=
        # ["notifications"]`` doesn't trip the ``project_fields``
        # "key not found" warning on an instance with no active alerts.
        if include_notifications_bool:
            result["notification_count"] = 0
            result["notifications"] = []
            try:
                ws_result = await client.send_websocket_message(
                    {"type": "persistent_notification/get"}
                )
                if ws_result.get("success"):
                    notifications = ws_result.get("result", [])
                    result["notification_count"] = len(notifications)
                    result["notifications"] = [
                        {
                            "notification_id": n.get("notification_id"),
                            "title": n.get("title"),
                            "message": n.get("message"),
                            "created_at": n.get("created_at"),
                        }
                        for n in notifications
                    ]
            except Exception as e:
                logger.warning(
                    "Failed to fetch notifications for overview: %s", e, exc_info=True
                )

        # Active repairs only by default — matches the HA Repairs UI so agents
        # don't chase problems the user already dismissed. ``repairs`` is
        # always emitted (empty list when none) for the same reason
        # ``notifications`` is — the ``fields=`` docstring advertises it
        # as available unconditionally.
        result["repair_count"] = 0
        result["repairs"] = []
        try:
            repairs_result = await client.send_websocket_message(
                {"type": "repairs/list_issues"}
            )
            if repairs_result.get("success"):
                all_issues = repairs_result.get("result", {}).get("issues", [])
                visible_issues = filter_active_repairs(
                    all_issues,
                    include_dismissed=include_dismissed_repairs_bool,
                )
                result["repair_count"] = len(visible_issues)
                if not include_dismissed_repairs_bool:
                    dismissed_count = len(all_issues) - len(visible_issues)
                    if dismissed_count:
                        result["dismissed_repair_count"] = dismissed_count
                result["repairs"] = [project_repair_fields(r) for r in visible_issues]
            else:
                err = repairs_result.get("error") or {}
                err_msg = (
                    err.get("message") if isinstance(err, dict) else str(err)
                ) or "unknown error"
                logger.warning(
                    "repairs/list_issues returned success=false: %s", err_msg
                )
                result["repairs_error"] = f"Could not fetch repairs: {err_msg}"
        except Exception as e:
            logger.warning("Failed to fetch repairs for overview: %s", e, exc_info=True)
            result["repairs_error"] = f"Could not fetch repairs: {e}"

        # Include tool discovery hint when search transform is active
        settings = get_global_settings()
        if settings.enable_tool_search:
            result["tool_discovery"] = {
                "hint": (
                    "This server uses search-based tool discovery. "
                    "Use ha_search_tools(query='...') to find tools, then "
                    "execute the discovered tool directly by name (preferred), "
                    "or via a proxy for permission gating: "
                    "ha_call_read_tool, ha_call_write_tool, or "
                    "ha_call_delete_tool. Each proxy takes name and arguments "
                    "as separate top-level params. Call proxy tools SEQUENTIALLY "
                    "(not in parallel) to avoid cascading cancellations. "
                    "Do NOT assume a capability is unavailable without searching first."
                ),
                "pinned_tools": sorted(
                    [
                        *DEFAULT_PINNED_TOOLS,
                        "ha_search_tools",
                        "ha_call_read_tool",
                        "ha_call_write_tool",
                        "ha_call_delete_tool",
                    ]
                ),
            }

        # Surface the stdio settings UI sidecar URL when a URL file is
        # present (issue #863). The LLM can hand this URL to the user
        # when they ask how to change settings — the sidecar process
        # outlives the stdio MCP subprocess, so the URL stays reachable.
        # Surfacing is advisory: a missing or unreadable URL file
        # MUST NOT fail the overview tool. The file is normally only
        # present in stdio mode; HTTP modes mount the settings page on
        # the FastMCP server directly. A leftover URL file from a prior
        # stdio run on the same machine could in principle be surfaced
        # by an HTTP-mode process — acceptable because the URL itself
        # is gated by the random secret path either way.
        #
        # Added *after* ``project_fields`` so it survives every
        # ``fields=`` projection — even an LLM that calls
        # ``fields=["system_info"]`` (to minimize payload) still sees
        # the URL and can hand it to the user. Hiding it behind the
        # projection made it effectively invisible to less-attentive
        # LLMs that scanned only the documented ``fields=`` enum.
        from ..stdio_settings_sidecar import read_sidecar_url

        projected = project_fields(result, parsed_fields)
        sidecar_url = read_sidecar_url()
        if sidecar_url:
            projected["settings_url"] = sidecar_url
        else:
            # No stdio sidecar URL file. In HTTP / Docker / OAuth modes the
            # settings page is mounted on this server, but the process binds
            # 0.0.0.0 and can't know its externally reachable host — so hint
            # at the page (and the startup-log URL) instead of guessing a
            # wrong absolute URL (issue #1458).
            from ..settings_ui import get_http_settings_prefix

            http_prefix = get_http_settings_prefix()
            if http_prefix:
                settings_path = f"{http_prefix.rstrip('/')}/settings"
                projected["settings_url_hint"] = (
                    "The settings page (enable/disable/pin tools, feature "
                    "flags, advanced settings, backups, tool-approval) is "
                    f"served at '{settings_path}' on this MCP server. Find the "
                    "full URL in the ha-mcp startup logs, or append it to the "
                    "base URL your client connects to."
                )

        return projected

    async def ha_deep_search(
        query: str,
        search_types: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Types to search: 'automation', 'script', 'scene', 'helper', 'dashboard'. "
                    "Pass as list or JSON array string. Default: automation, script, scene, helper."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=5,
                ge=1,
                description="Maximum total results to return (default: 5)",
            ),
        ] = 5,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
        include_config: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Include full config in results. Default: False (returns summary only). "
                    "Use ha_config_get_automation/ha_config_get_script for individual configs."
                ),
            ),
        ] = False,
        exact_match: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Use exact substring matching (default: True). "
                    "Set to False for fuzzy matching when the query may contain typos "
                    "or when searching with approximate terms."
                ),
            ),
        ] = True,
        config_time_budget: Annotated[
            float | None,
            Field(
                default=None,
                gt=0,
                le=300,
                description=(
                    "Per-call override for the per-id config-fetch wall-clock "
                    "budget (seconds). Replaces the per-type "
                    "HAMCP_*_CONFIG_TIME_BUDGET defaults for automation, "
                    "script, AND scene branches. Use when a `partial: True` "
                    "response names time-budget skipping. Stateless per-call: "
                    "one caller's override doesn't affect others. None = use "
                    "the per-type env defaults."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Search inside automation, script, scene, helper, and dashboard *configurations* — not for finding entity IDs.

        Use this when you need to find configurations by what they *do* (e.g., which automations
        call a specific service, which scenes set a particular entity, or any config that contains
        a certain action). For finding entity IDs by name, use ha_search instead.

        Searches within configuration definitions including triggers, actions, sequences, scene
        entity sets, and other config fields. Also searches dashboard configurations (cards,
        badges, views) when search_types includes 'dashboard'.

        **NOTE:** Dashboards and badges are NOT searched by default. Add 'dashboard' to
        search_types to include them.

        The 'helper' search covers both input_* helpers (input_boolean, input_number, ...)
        and UI-created flow-based helpers (template, group, utility_meter, derivative, ...).
        For flow-helpers, results carry the parent config entry id under ``entry_id``.
        When ``include_config=False`` (the default), pair with
        ``ha_get_integration(entry_id=..., include_options=True)`` to retrieve the full
        config; set ``include_config=True`` to get it inline in one call.

        Args:
            query: Search query (exact substring by default, or fuzzy with exact_match=False)
            search_types: Types to search (default: ["automation", "script", "scene", "helper"])
            limit: Maximum total results to return (default: 5)
            exact_match: Use exact substring matching (default: True)

        Examples:
            - Find automations referencing an entity: ha_deep_search("sensor.temperature")
            - Find with fuzzy matching: ha_deep_search("motion", exact_match=False)
            - Find scenes touching a light: ha_deep_search("light.kitchen")
            - Search dashboards for entity refs: ha_deep_search("sensor.temperature", search_types=["dashboard"])
            - Search everything: ha_deep_search("light.bedroom", search_types=["automation","script","scene","helper","dashboard"])
        """
        # Parse search_types to handle JSON string input from MCP clients
        try:
            parsed_search_types = parse_string_list_param(search_types, "search_types")
        except ValueError as exc:
            raise_tool_error(
                create_validation_error(str(exc), parameter="search_types")
            )
        _validate_search_types(parsed_search_types)
        include_config_bool = include_config
        exact_match_bool = exact_match
        try:
            result = await smart_tools.deep_search(
                query,
                parsed_search_types,
                limit,
                offset,
                include_config_bool,
                exact_match=exact_match_bool,
                config_time_budget=config_time_budget,
                ctx=ctx,
            )
            return cast(dict[str, Any], result)
        except ToolError:
            raise
        except Exception as e:
            logger.error(
                f"Error in deep search: query={query}, "
                f"search_types={parsed_search_types}, limit={limit}, "
                f"error={e}",
                exc_info=True,
            )
            exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "search_types": parsed_search_types,
                    "limit": limit,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                ],
            )
            return None  # unreachable: exception_to_structured_error always raises

    @mcp.tool(
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Entity State",
        },
    )
    @log_tool_usage
    async def ha_get_state(
        entity_id: Annotated[
            str | list[str],
            Field(
                description="Entity ID or list of entity IDs to retrieve state for "
                "(e.g., 'light.kitchen' or ['light.kitchen', 'sensor.temperature'])"
            ),
        ],
        fields: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level entity record keys to reduce "
                    'response size (e.g. ["state", "attributes"]). '
                    "None = full entity record (default). "
                    "Available keys: entity_id, state, attributes, last_changed, "
                    "last_reported, last_updated, context."
                ),
            ),
        ] = None,
        attribute_keys: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Return only the specified keys from each entity's attributes dict "
                    '(e.g. ["brightness", "color_temp"] for lights). '
                    "None = full attributes (default). "
                    "Unknown keys are silently dropped. "
                    'Requires "attributes" to be present in fields= (or fields=None).'
                ),
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get current status, state, and attributes of one or more entities (lights, switches, sensors, climate, covers, locks, fans, etc.).

        SINGLE ENTITY:
        Pass a string entity_id. Returns the entity's full state and attributes.

        MULTIPLE ENTITIES:
        Pass a list of entity IDs (max 100). Efficiently retrieves states using
        parallel requests. Duplicates are automatically deduplicated.
        Returns success=True if at least one entity state was retrieved.
        Check 'error_count' for any failed lookups in partial-success scenarios.

        FIELDS PROJECTION:
        `fields=` projects the per-entity record keys (see the fields= parameter
        description for the full key list), NOT the outer bulk response wrapper.
        In single-entity mode it filters keys of the returned record directly. In bulk
        mode it filters keys of each record inside `states[entity_id]`; outer keys
        (`success`, `count`, `states`, `errors`, ...) are always preserved.
        `attribute_keys=` further narrows the `attributes` sub-dict and is only applied
        when `"attributes"` is in `fields=` (or `fields=None`); otherwise it is a no-op.

        When `attribute_keys=` is set but has no effect (because `attributes` was
        excluded by `fields=`), a `warnings` list is emitted outside the projected
        entity record(s): in bulk mode at the response wrapper level (sibling of
        `success`/`count`/`states`); in single-entity mode at the top-level result
        (sibling of `data`/`metadata`, since the projected record IS `data`).
        The warnings list is never a record key, so `fields=["state"]` returns a
        record with only `state` regardless of whether the no-effect warning fires.

        EXAMPLES:
        - Single: ha_get_state("light.kitchen")
        - Multiple: ha_get_state(["light.kitchen", "light.living_room", "sensor.temperature"])
        - State only: ha_get_state("light.kitchen", fields=["state"])
        - Slim bulk: ha_get_state(["light.kitchen", "sensor.temperature"], fields=["state", "attributes"], attribute_keys=["brightness"])
        """
        # Parse projection params once up front so the bulk loop doesn't re-parse
        # the same string/CSV input per entity (100 entities → 200 parses pre-fix).
        # parse_string_list_param raises ValueError on bad input; surface as
        # VALIDATION_FAILED with parameter="fields"/"attribute_keys" via the
        # normal ToolError flow.
        try:
            parsed_fields = parse_string_list_param(fields, "fields", allow_csv=True)
        except ValueError as e:
            raise_tool_error(create_validation_error(str(e), parameter="fields"))
        try:
            parsed_attribute_keys = parse_string_list_param(
                attribute_keys, "attribute_keys", allow_csv=True
            )
        except ValueError as e:
            raise_tool_error(
                create_validation_error(str(e), parameter="attribute_keys")
            )

        # `attribute_keys` only takes effect when `attributes` is in the projected
        # field set (or `fields=None`). Surface a warning rather than silently
        # ignoring it — caller likely intended to slim attributes and would
        # otherwise see an unfiltered or absent `attributes` key with no signal.
        attribute_keys_no_effect = (
            parsed_attribute_keys is not None
            and parsed_fields is not None
            and "attributes" not in parsed_fields
        )

        # Single entity path
        if isinstance(entity_id, str):
            try:
                result = await client.get_entity_state(entity_id)
                entity_record, attr_warn = _project_entity(
                    result, parsed_fields, parsed_attribute_keys
                )
                # Always wrap (include_metadata=True); callers and tests rely on
                # the ``result["data"]`` envelope even when fields= is active.
                wrapped = await add_timezone_metadata(client, entity_record)
                # ``attribute_keys`` was specified but ``attributes`` is not
                # in the projected ``fields=`` set. Attach the warning at
                # the outer wrapper level (sibling of ``data``/``metadata``)
                # rather than spreading it into ``data`` — the FIELDS
                # PROJECTION contract: ``fields=`` filters the keys of the
                # returned record; ``warnings`` is not a record key.
                # Bulk path keeps ``warnings`` outside the per-entity records
                # (at ``data`` level, sibling of ``states``); in single-entity
                # mode the projected record IS ``data``, so the analogous
                # "outside" location is the top-level wrapper (sibling of
                # ``data``/``metadata``).
                # ``add_timezone_metadata`` always returns a dict, so
                # ``wrapped.setdefault("warnings", [])`` is type-safe regardless
                # of ``entity_record``'s type — no isinstance guard needed.
                if attribute_keys_no_effect:
                    wrapped.setdefault("warnings", []).append(
                        "attribute_keys was ignored because 'attributes' is not in "
                        "fields=. Add 'attributes' to fields= (or omit fields=) to "
                        "apply attribute_keys."
                    )
                if attr_warn:
                    wrapped.setdefault("warnings", []).append(attr_warn)
                return wrapped
            except ToolError:
                raise
            except Exception as e:
                exception_to_structured_error(
                    e,
                    context={"entity_id": entity_id},
                    suggestions=[
                        f"Verify entity '{entity_id}' exists in Home Assistant",
                        "Check Home Assistant connection",
                        "Use ha_search() to find correct entity IDs",
                    ],
                )

        # Multiple entities path
        entity_ids: list[str] = entity_id
        MAX_ENTITIES = 100

        if not isinstance(entity_ids, list) or not entity_ids:
            raise_tool_error(
                create_validation_error(
                    "entity_id must be a non-empty string or list of entity ID strings",
                    parameter="entity_id",
                )
            )

        if not all(isinstance(eid, str) for eid in entity_ids):
            raise_tool_error(
                create_validation_error(
                    "All entity_id values must be strings",
                    parameter="entity_id",
                )
            )

        if len(entity_ids) > MAX_ENTITIES:
            raise_tool_error(
                create_validation_error(
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum of {MAX_ENTITIES}",
                    parameter="entity_id",
                )
            )

        # Deduplicate while preserving order
        unique_ids = list(dict.fromkeys(entity_ids))
        if len(unique_ids) < len(entity_ids):
            logger.debug(
                f"Deduplicated entity_ids: {len(entity_ids)} -> {len(unique_ids)}"
            )

        try:

            async def _fetch_state(eid: str) -> dict[str, Any]:
                try:
                    state = await client.get_entity_state(eid)
                    return {"success": True, "entity_id": eid, "state": state}
                except Exception as e:
                    logger.warning(f"Failed to fetch state for '{eid}': {e}")
                    # ast-grep-ignore — batch item failure, aggregated via asyncio.gather
                    return exception_to_structured_error(
                        e,
                        context={"entity_id": eid},
                        raise_error=False,
                    )

            results = await asyncio.gather(*(_fetch_state(eid) for eid in unique_ids))

            states: dict[str, Any] = {}
            errors: list[dict[str, Any]] = []
            _bulk_attr_warns: list[str] = []

            for eid, result in zip(unique_ids, results, strict=True):
                if result.get("success") is True and "state" in result:
                    state_record, attr_warn = _project_entity(
                        result["state"], parsed_fields, parsed_attribute_keys
                    )
                    states[eid] = state_record
                    # Collect unique attribute-typo warnings across entities
                    # (different entities may report different available keys).
                    if attr_warn and attr_warn not in _bulk_attr_warns:
                        _bulk_attr_warns.append(attr_warn)
                else:
                    error_detail = result.get("error")
                    if error_detail is None:
                        error_detail = {
                            "code": "INTERNAL_ERROR",
                            "message": "Unknown error",
                        }
                    errors.append(
                        {
                            "entity_id": result.get("entity_id", eid),
                            "error": error_detail,
                        }
                    )

            response: dict[str, Any] = {
                "success": len(states) > 0,
                "count": len(states),
                "states": states,
            }

            if attribute_keys_no_effect:
                response.setdefault("warnings", []).append(
                    "attribute_keys was ignored because 'attributes' is not in "
                    "fields=. Add 'attributes' to fields= (or omit fields=) to "
                    "apply attribute_keys."
                )

            for _w in _bulk_attr_warns:
                response.setdefault("warnings", []).append(_w)

            if errors:
                response["errors"] = errors
                response["error_count"] = len(errors)
                response["suggestions"] = [
                    "Use ha_search() to find correct entity IDs for failed lookups",
                    "Verify entities exist in Home Assistant",
                ]
                if states:
                    response["partial"] = True

            return await add_timezone_metadata(client, response)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting bulk states: {e}", exc_info=True)
            exception_to_structured_error(
                e,
                context={"entity_ids": entity_ids},
            )
            return None  # unreachable: exception_to_structured_error always raises
