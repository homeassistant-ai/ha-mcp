"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import get_websocket_client
from ..config import get_global_settings
from ..errors import create_validation_error
from ..transforms.categorized_search import DEFAULT_PINNED_TOOLS
from ..utils.fuzzy_search import apply_hidden_penalty
from ..visibility.resolver import (
    device_registry_needed_for_visibility,
    load_hidden_set,
    visibility_state_and_wire,
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
    JSON_STRING_COERCION,
    add_timezone_metadata,
    build_pagination_metadata,
    filter_active_repairs,
    merge_visibility_warnings,
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
# of the flat dual-surface envelope. ``state_filter`` is a caller-input
# echo with no observable verification value at the envelope top (the
# caller has the input they passed); ``area_name`` is per-entity
# decoration that belongs inside the entity record; ``note`` is a
# redundant mode-label string already conveyed by ``search_type``. None
# are in ``_ALWAYS_KEEP_PROJECTION`` or the ``fields=`` Available keys
# docstring, so leaking them would advertise undocumented keys via the
# typo-guard while a real ``fields=`` projection silently strips them.
#
# ``search_type``, ``domain_filter``, ``area_filter``, ``message``,
# ``by_domain``, ``state_filter_note``, and ``area_names`` are
# intentionally NOT in the strip set — the E2E test suite empirically
# pins their presence (search_type at 17+ sites, domain_filter at 6,
# area_filter at 1, message at 2), so callers verifiably depend on them.
# All are documented as top-level keys + retained in
# ``_ALWAYS_KEEP_PROJECTION``.
_ENTITIES_BRANCH_SKIP_KEYS: tuple[str, ...] = (
    "results",
    "total_matches",
    "has_more",
    "next_offset",
    "state_filter",
    "area_name",
    "note",
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
        # Entity-branch internal mode label ("exact_match", "fuzzy_search",
        # "area_only", "area_filtered_query", "domain_listing"). E2E tests
        # pin its presence at 17+ assertion sites — callers verifiably
        # rely on it to disambiguate which entity-search path produced
        # the result, so retained at the envelope top instead of stripped.
        "search_type",
        # Caller-input echoes — would normally be stripped as no-value
        # echoes (the caller has the inputs they passed), but the E2E
        # test suite pins their presence (domain_filter at 6 assertion
        # sites, area_filter at 1), so callers do read them back. Kept
        # at the envelope top + documented.
        "domain_filter",
        "area_filter",
        # Zero-result diagnostic ("No <domain> entities found in area:
        # <area>"). E2E tests pin it at 2 sites. Conditional emission
        # under area_filter + zero-result; survives ``fields=`` projection
        # so a narrowing caller still gets the explanation.
        "message",
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
    """Project the orchestrator response to the caller-requested top-level
    keys, retaining the diagnostic / pagination contract via
    ``_ALWAYS_KEEP_PROJECTION``.

    Inlined rather than delegated to ``util_helpers.project_fields`` so the
    pre-parsed list passes through end-to-end — the orchestrator already
    parsed ``fields=`` once via ``parse_string_list_param``, and
    ``project_fields`` would re-parse the same list (idempotent but
    redundant work on every call). Restores the top-level ``fields=``
    capability that ``ha_search_entities`` carried pre-rename, applied to
    the new flat envelope. The always-keep set means
    ``fields=["entities"]`` still leaves ``partial`` / ``errors[]`` /
    ``warnings[]`` / ``*_total_matches`` / pagination keys accessible —
    projection narrows the response but never hides incompleteness.
    """
    if parsed_fields is None:
        return response
    always_keep: set[str] = {"success", "warnings"} | set(_ALWAYS_KEEP_PROJECTION)
    requested = set(parsed_fields)
    keep = requested | always_keep
    result = {k: v for k, v in response.items() if k in keep}
    # Typo guard — flag any requested keys absent from the response so
    # ``fields=["frobnicate"]`` surfaces a diagnostic rather than a
    # mysteriously empty payload. Excludes the always-keep sentinels so
    # ``fields=["success"]`` never warns.
    unknown = sorted(requested - set(response.keys()) - always_keep)
    if unknown:
        available = sorted(k for k in response.keys() if k not in always_keep)
        result.setdefault("warnings", []).append(
            f"fields {unknown!r} not found in response — available keys: {available!r}"
        )
    return result


_INTENT_SKIP_WARNING: str = (
    "config-body search skipped: domain_filter / area_filter / "
    "state_filter signals entity-only intent. To search config bodies, "
    'pass search_types=["automation", ...] — but note this pins the call '
    "to config-only and drops the entity-result surface, so it does not "
    "return both alongside each other."
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


async def _prefetch_shared_search_snapshots(
    client: Any,
    *,
    registry_eligible: bool,
    body_eligible: bool,
) -> tuple[list[dict[str, Any]] | None, Any]:
    """Pre-fetch ``/api/states`` + the entity-registry list once for ha_search.

    Only pre-fetches when both branches are eligible — a lone branch keeps
    fetching for itself. Returns ``(shared_states, shared_registry)``; either is
    ``None`` when not pre-fetched, or when its fetch failed (each branch then
    fetches and fails on its own, reproducing the per-surface partial-result
    handling exactly). A cancellation (structured-concurrency teardown)
    propagates rather than degrading to per-branch fetching.
    """
    if not (registry_eligible and body_eligible):
        return None, None
    prefetch = await asyncio.gather(
        client.get_states(),
        client.send_websocket_message({"type": "config/entity_registry/list"}),
        return_exceptions=True,
    )
    for snap in prefetch:
        if isinstance(snap, BaseException) and not isinstance(snap, Exception):
            raise snap
    shared_states = prefetch[0] if not isinstance(prefetch[0], BaseException) else None
    shared_registry = (
        prefetch[1] if not isinstance(prefetch[1], BaseException) else None
    )
    return shared_states, shared_registry


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
            _merge_list_key(response, key, value)
            continue
        if key == "errors" and isinstance(value, list):
            _merge_list_key(response, key, value)
            continue
        if key == "partial" and isinstance(value, bool):
            response["partial"] = bool(response.get("partial")) or value
            continue
        if key == "partial_reason" and isinstance(value, str) and value:
            _merge_partial_reason(response, value)
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


def _merge_list_key(response: dict[str, Any], key: str, value: list[Any]) -> None:
    """Extend an existing list key or replace a non-list value with a new list.

    The non-list branch handles a broken upstream state where ``key`` is already
    present but not a list — that violates the ``list[str]`` contract.  Replace
    with the payload's well-typed list rather than crash on ``.extend``.
    """
    current = response.get(key)
    if isinstance(current, list):
        current.extend(value)
    else:
        response[key] = list(value)


def _merge_partial_reason(response: dict[str, Any], value: str) -> None:
    """Concatenate a new partial_reason string with de-duplication."""
    current = response.get("partial_reason")
    if isinstance(current, str) and current:
        if value not in current:
            response["partial_reason"] = f"{current} ; {value}"
    else:
        response["partial_reason"] = value


def _format_search_diagnostics(diagnostics: dict[str, Any]) -> str | None:
    """Render the component's non-empty search diagnostics into one reason fragment.

    The component reports intentional per-surface diagnostics (e.g.
    ``config_components_inaccessible: [...]`` — config domains it could not read
    from HA's in-process registries). Each non-empty entry becomes a
    human-readable ``"<label>: <values>"`` clause; empty entries are dropped.
    Returns ``None`` when nothing is reportable.
    """
    fragments: list[str] = []
    for key, value in diagnostics.items():
        if not value:
            continue
        label = key.replace("_", " ")
        if isinstance(value, (list, tuple, set)):
            fragments.append(f"{label}: {', '.join(str(v) for v in value)}")
        else:
            fragments.append(f"{label}: {value}")
    return "; ".join(fragments) if fragments else None


def _build_hidden_ids(registry_result: Any) -> set[str]:
    """Build a set of hidden entity IDs from a registry/list WS response."""
    hidden_ids: set[str] = set()
    if isinstance(registry_result, dict) and registry_result.get("success"):
        for entry in registry_result.get("result", []):
            if entry.get("hidden_by") is not None:
                eid = entry.get("entity_id")
                if eid:
                    hidden_ids.add(eid)
    else:
        # Without the registry we can't tag hidden entities, so the score-penalty
        # downgrade silently doesn't apply.  Log so the operator can correlate
        # "diagnostic entity ranking first" with this WS hiccup instead of a
        # code regression.
        logger.warning(
            "hidden_filter_unavailable: registry/list returned %r — "
            "hidden entities will rank without the score penalty",
            registry_result,
        )
    return hidden_ids


def _apply_search_outcome(
    response: dict[str, Any],
    label: str,
    outcome: dict[str, Any],
) -> None:
    """Apply one gather outcome (entities or configs) to the response dict in-place.

    Both ``_ha_search_entities`` and ``_ha_deep_search`` return their search dict
    directly. The entity builders no longer wrap it via ``add_timezone_metadata``:
    entity-search records carry none of the timestamp fields that enrichment
    converts, so it was a discarded ``/api/config`` fetch. The ``{"data": ...}``
    unwrap is kept as a defensive no-op so a future wrapped payload still reads
    correctly.
    """
    payload = (
        outcome["data"] if isinstance(outcome, dict) and "data" in outcome else outcome
    )
    if label == "entities":
        response["entities"] = payload.get("results", [])
        response["entity_total_matches"] = payload.get("total_matches", 0)
        response["entity_has_more"] = bool(payload.get("has_more", False))
        response["entity_next_offset"] = payload.get("next_offset")
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


def _apply_by_domain_grouping(
    data: dict[str, Any],
    results: list[dict[str, Any]],
    group_by_domain_bool: bool,
    per_domain_limit_int: int | None,
    parsed_result_fields: list[str] | None,
) -> None:
    """Build a by_domain map from results and attach it to data in-place."""
    if not group_by_domain_bool:
        return
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for item in results:
        domain = item.get("domain", (item.get("entity_id") or ".").split(".")[0])
        by_domain.setdefault(domain, []).append(item)
    if per_domain_limit_int is not None:
        by_domain = {d: ents[:per_domain_limit_int] for d, ents in by_domain.items()}
    if parsed_result_fields is not None:
        by_domain = {
            d: _project_records(ents, parsed_result_fields)
            for d, ents in by_domain.items()
        }
    data["by_domain"] = by_domain


def _apply_result_fields_to_response(
    data: dict[str, Any],
    parsed_result_fields: list[str] | None,
) -> None:
    """Project data['results'] to parsed_result_fields and attach any warning."""
    if parsed_result_fields is None or "results" not in data:
        return
    orig = data["results"]
    data["results"] = _project_records(orig, parsed_result_fields)
    _warn = _result_fields_warning(orig, data["results"], parsed_result_fields)
    if _warn:
        data.setdefault("warnings", []).append(_warn)


def _new_search_response(
    query: str | None, parsed_search_types: list[str] | None
) -> dict[str, Any]:
    """Build the base ha_search response envelope shared by both serving paths.

    The component-served and legacy-served paths start from this identical
    skeleton, so the two responses are shape-parity by construction: every
    accumulating diagnostic / pagination key gets a typed default here, then is
    filled by ``_apply_search_outcome`` regardless of which path produced the
    data.
    """
    return {
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
        "partial": False,
        "errors": [],
        "warnings": [],
    }


@dataclass(frozen=True)
class _ResolvedSearch:
    """Parsed + validated ha_search inputs shared by the component and legacy paths.

    ``ha_search`` parses parameters, validates them, and computes branch
    eligibility exactly once, then hands this immutable bundle to whichever
    path serves the request so both operate on identical resolved inputs. The
    filter fields (``domain_filter`` / ``area_filter`` / ``state_filter``) hold
    the **raw** tool arguments so the legacy path normalises them exactly as
    before; the component helpers normalise their own copies.
    """

    query: str | None
    query_text: str
    domain_filter: str | None
    area_filter: str | None
    state_filter: str | None
    parsed_search_types: list[str] | None
    parsed_fields: list[str] | None
    result_fields: Any
    limit: int
    offset: int
    exact_match: bool
    include_hidden: bool
    include_config: bool
    group_by_domain: bool
    per_domain_limit: int | None
    config_time_budget: float | None
    registry_eligible: bool
    body_eligible: bool
    body_skipped_by_intent_gate: bool


def _parse_component_result_fields(result_fields: Any) -> list[str] | None:
    """Parse ``result_fields`` for the component path (mirrors the entity branch).

    The legacy entity branch parses ``result_fields`` inside
    ``_validate_entity_search_params``; the component path re-uses the identical
    parse + validation so a bad ``result_fields`` raises the same structured
    error on either path.
    """
    if result_fields is None:
        return None
    try:
        parsed = parse_string_list_param(result_fields, "result_fields", allow_csv=True)
    except ValueError as exc:
        raise_tool_error(create_validation_error(str(exc), parameter="result_fields"))
    if parsed is not None and len(parsed) == 0:
        raise_tool_error(
            create_validation_error(
                "result_fields must contain at least one key",
                parameter="result_fields",
            )
        )
    return parsed


def _as_record_list(value: Any) -> list[dict[str, Any]]:
    """Coerce a component payload slice to a list of records (defensive)."""
    if isinstance(value, list):
        return value
    return []


def _normalized_domain_filter(raw: str | None) -> str | None:
    """Strip + lowercase a domain filter to the entity branch's canonical form.

    Matches ``_validate_entity_search_params`` so the component request and the
    ``domain_filter`` echo agree with the legacy path.
    """
    return ((raw or "").strip().lower()) or None


# The documented per-record entity surface (result_fields= "Available keys").
# Both legacy entity paths emit exactly these; the component path is trimmed
# to them in _shape_component_search_response.
_ENTITY_RECORD_KEYS = (
    "entity_id",
    "friendly_name",
    "domain",
    "state",
    "score",
    "match_type",
)

# Opt-in enrichment fields result_fields= can request on top of the base record
# (issue #1813 C1). Emitted per entity ONLY when named in result_fields — the
# default record shape stays the six _ENTITY_RECORD_KEYS. The component search
# already computes these per hit (its area/floor/labels/aliases registry join);
# the legacy path joins them from the registries on demand
# (SearchTools._fetch_entity_enrichment). Ordered so a projected record lists them
# consistently regardless of the caller's result_fields order.
_ENRICHMENT_FIELDS: tuple[str, ...] = ("area", "floor", "labels", "aliases")

# Every field name result_fields= accepts — base record keys plus the opt-in
# enrichment keys. A requested name outside this set is rejected up front with the
# standard validation error rather than silently projecting to empty records.
_ALLOWED_RESULT_FIELDS: frozenset[str] = frozenset(_ENTITY_RECORD_KEYS) | frozenset(
    _ENRICHMENT_FIELDS
)


def _validate_result_field_names(parsed: list[str] | None) -> None:
    """Reject unknown ``result_fields`` names with the standard validation error.

    ``result_fields`` now drives area/floor/labels/aliases enrichment (issue #1813
    C1), so an unrecognised name is a hard error rather than a silently-empty
    projection: the server must know which fields to compute. Empty is rejected too
    (omit the parameter for full records). Called once in ``ha_search`` so both the
    component and legacy serving paths share one contract.
    """
    if parsed is None:
        return
    if not parsed:
        raise_tool_error(
            create_validation_error(
                "result_fields must contain at least one key; omit the parameter "
                "for full records.",
                parameter="result_fields",
            )
        )
    unknown = [f for f in parsed if f not in _ALLOWED_RESULT_FIELDS]
    if unknown:
        raise_tool_error(
            create_validation_error(
                f"Unknown result_fields: {unknown}. "
                f"Valid keys: {sorted(_ALLOWED_RESULT_FIELDS)}.",
                parameter="result_fields",
            )
        )


def _requested_enrichment(parsed_result_fields: list[str] | None) -> tuple[str, ...]:
    """The enrichment fields named in ``result_fields``, in canonical order.

    Empty when ``result_fields`` is unset or names only base record keys — the
    signal that no enrichment work (component key retention or a legacy registry
    join) is needed, keeping the default search path cost-free.
    """
    if not parsed_result_fields:
        return ()
    requested = set(parsed_result_fields)
    return tuple(f for f in _ENRICHMENT_FIELDS if f in requested)


def _ws_result_map(resp: Any) -> dict[str, dict[str, Any]]:
    """The ``{entity_id: entry}`` map from a ``config/entity_registry/get_entries`` reply."""
    if isinstance(resp, dict) and resp.get("success"):
        result = resp.get("result")
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if isinstance(v, dict)}
    return {}


def _ws_registry_index(resp: Any, key: str) -> dict[str, dict[str, Any]]:
    """Index a ``config/*_registry/list`` reply by its id field (area_id/floor_id/…).

    A failed / malformed reply (the ``return_exceptions=True`` gather may hand back
    an exception) yields an empty index so the enrichment degrades that field to
    empty rather than raising.
    """
    out: dict[str, dict[str, Any]] = {}
    if isinstance(resp, dict) and resp.get("success"):
        for item in resp.get("result") or []:
            if isinstance(item, dict) and item.get(key):
                out[item[key]] = item
    return out


def _ws_read_failed(resp: Any) -> bool:
    """True when a gathered registry read raised or returned a non-success reply.

    Mirrors the guard inside :func:`_ws_result_map` / :func:`_ws_registry_index`
    (which quietly degrade a bad reply to an empty map). Surfacing the same
    condition lets the enrichment join report the degradation instead of emitting
    present-but-null area/floor/labels/aliases indistinguishable from a genuinely
    unassigned entity.
    """
    return not (isinstance(resp, dict) and resp.get("success"))


def _entity_enrichment_fields(
    entry: dict[str, Any],
    areas: dict[str, dict[str, Any]],
    floors: dict[str, dict[str, Any]],
    labels: dict[str, dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    requested: tuple[str, ...],
) -> dict[str, Any]:
    """Compute the requested enrichment fields for one entity from registry data.

    Mirrors the component's ``_registry_enrichment`` so the legacy and
    component-served ``result_fields`` values agree: device-inherited area/labels
    (the entity's own value wins, else the device's), area→floor resolution, and
    label id→name (falling back to the id when a label has no name). ``aliases``
    pass through from the registry entry. Only the requested keys are returned.

    String aliases only: HA core's aliases can carry the COMPUTED_NAME sentinel,
    which serializes as ``null`` over the WS registry read; a blind ``str()`` would
    publish it as the literal alias ``"None"``. The component's join filters the
    same way, so dropping non-strings keeps the two paths byte-identical (the name
    the sentinel stands for is already matched via the friendly name).
    """
    aliases = sorted(a for a in (entry.get("aliases") or []) if isinstance(a, str))
    area_id = entry.get("area_id")
    label_ids = set(entry.get("labels") or [])
    device_id = entry.get("device_id")
    device = devices.get(device_id) if device_id else None
    if device:
        if area_id is None:
            area_id = device.get("area_id")
        label_ids |= set(device.get("labels") or [])
    area = areas.get(area_id) if area_id else None
    area_name = area.get("name") if area else None
    floor_id = area.get("floor_id") if area else None
    floor = floors.get(floor_id) if floor_id else None
    floor_name = floor.get("name") if floor else None
    label_names = [
        (labels.get(lid) or {}).get("name") or lid for lid in sorted(label_ids)
    ]
    full: dict[str, Any] = {
        "area": area_name,
        "floor": floor_name,
        "labels": label_names,
        "aliases": aliases,
    }
    return {k: full[k] for k in requested}


def _normalize_component_config_record(
    bucket: str, rec: dict[str, Any], include_config: bool
) -> dict[str, Any]:
    """Map one component config-bucket record onto the exact legacy key set.

    The component speaks HA-native vocabulary (automations/scripts carry an
    ``alias``, scenes a ``name``, storage ids ride an ``id`` key, and records
    add ``source``/``kind``/``object_id`` metadata). The legacy deep-search
    records that agents, tests, and downstream consumers key on use
    ``friendly_name`` plus per-bucket id keys (``script_id``/``scene_id``) —
    so normalize here, at the single seam, rather than teaching the component
    the MCP envelope's vocabulary. Extra component fields are deliberately
    dropped for byte-level shape parity with the legacy path; enrichment
    (e.g. ``source: yaml``) can be added to BOTH paths together later.

    ``config`` key semantics mirror the legacy pipeline's include_config pop:
    present (possibly ``None`` for YAML/name-only matches) when
    ``include_config`` is True, absent otherwise. Flow-helper records carry
    their body under ``options`` component-side (data-minimized
    ``ConfigEntry.options``); legacy calls the same payload ``config``.
    """
    entity_id = rec.get("entity_id")
    name = rec.get("alias") or rec.get("name") or rec.get("friendly_name")
    out: dict[str, Any] = {}
    if bucket == "helpers":
        if rec.get("kind") == "flow" or (entity_id is None and rec.get("entry_id")):
            out["entry_id"] = rec.get("entry_id")
        else:
            out["entity_id"] = entity_id
        out["helper_type"] = rec.get("helper_type")
        out["name"] = name
    else:
        out["entity_id"] = entity_id
        if bucket == "scripts":
            out["script_id"] = rec.get("id")
        elif bucket == "scenes":
            out["scene_id"] = rec.get("id")
        out["friendly_name"] = name if name is not None else entity_id
    out["score"] = rec.get("score")
    out["match_in_name"] = bool(rec.get("match_in_name"))
    out["match_in_config"] = bool(rec.get("match_in_config"))
    if include_config:
        config = rec.get("config")
        if config is None and "options" in rec:
            config = rec.get("options")
        out["config"] = config if config else None
    return out


# Body surfaces the ``ha_mcp_tools`` component's ``search`` command accepts
# (its voluptuous allowlist also has ``entity``, appended separately by
# ``_build_component_search_request``). ``dashboard`` is deliberately absent:
# the component has no dashboard scanner, so a request naming it must stay on
# the legacy path — forwarding it just bounced off the component schema into a
# warning-laden fallback on every call (issue #2008).
_COMPONENT_BODY_SEARCH_TYPES: frozenset[str] = frozenset(
    {"automation", "script", "scene", "helper"}
)


def _component_serves_search_types(req: _ResolvedSearch) -> bool:
    """True when the component's search command accepts every requested surface.

    Only an explicit ``search_types`` list can name an unsupported surface, and
    only the body-eligible branch forwards it to the component — a
    body-ineligible request sends the entity surface alone, which the component
    always accepts. Routing is all-or-nothing per command (design § 4), so one
    unsupported surface sends the whole request to the legacy path, silently —
    the same treatment as the other route-ineligible modes, not the
    warning-emitting failure fallback.
    """
    if not req.body_eligible or req.parsed_search_types is None:
        return True
    return all(t in _COMPONENT_BODY_SEARCH_TYPES for t in req.parsed_search_types)


def _build_component_search_request(req: _ResolvedSearch) -> dict[str, Any]:
    """Translate resolved ha_search inputs into an ``ha_mcp_tools/search`` request.

    ``search_types`` on the WS command selects surfaces including the entity
    surface (``"entity"``), so branch eligibility computed server-side maps
    directly onto which surfaces the component searches — all-or-nothing per
    command. Optional string filters are omitted when empty to satisfy the
    component's ``str``-typed voluptuous schema.
    """
    search_types: list[str] = []
    if req.registry_eligible:
        search_types.append("entity")
    if req.body_eligible:
        search_types.extend(
            req.parsed_search_types or ["automation", "script", "scene", "helper"]
        )
    request: dict[str, Any] = {
        "search_types": search_types,
        "exact": req.exact_match,
        "include_hidden": req.include_hidden,
        "include_config": req.include_config,
        "limit": req.limit,
        "offset": req.offset,
    }
    if req.query_text:
        request["query"] = req.query_text
    domain_filter = _normalized_domain_filter(req.domain_filter)
    if domain_filter:
        request["domain_filter"] = domain_filter
    area_filter = (req.area_filter or "").strip()
    if area_filter:
        request["area_filter"] = area_filter
    state_filter = (req.state_filter or "").strip()
    if state_filter:
        request["state_filter"] = state_filter
    return request


def _merge_component_visibility_warnings(
    response: dict[str, Any], component_result: dict[str, Any]
) -> None:
    """Fold the component's ``visibility_warnings`` into the response warnings.

    The component emits these when a hide dimension fails open (unknown category /
    empty-registry allowlist / Assist unavailable). Merged into the same top-level
    warnings surface the legacy path fills via ``merge_visibility_warnings``, so the
    fast path is no longer silent about incomplete filtering.
    """
    component_visibility_warnings = component_result.get("visibility_warnings")
    if isinstance(component_visibility_warnings, list):
        merge_visibility_warnings(
            response,
            [w for w in component_visibility_warnings if isinstance(w, str)],
        )


def _shape_component_search_response(
    req: _ResolvedSearch, component_result: dict[str, Any]
) -> dict[str, Any]:
    """Map an ``ha_mcp_tools/search`` result into the ha_search envelope.

    The component returns per-surface records already scored and paginated
    (``entities`` + config buckets with ``*_total_matches`` / ``*_has_more``).
    Projection (``result_fields`` on entity records, ``fields`` on the
    response), by-domain grouping, and the flat pagination / partial-mirror
    finalisation all stay server-side and reuse the same helpers the legacy
    path uses (``_apply_search_outcome`` and friends), so the shape is
    identical to the legacy response by construction.
    """
    response = _new_search_response(req.query, req.parsed_search_types)
    _emit_intent_skip_warning(response, req.body_skipped_by_intent_gate)

    if req.registry_eligible:
        parsed_result_fields = _parse_component_result_fields(req.result_fields)
        # Base record is the six documented keys. result_fields may additionally
        # request enrichment fields (area/floor/labels/aliases) that the component
        # already computed per hit via its registry join — retain exactly those
        # requested keys before the result_fields projection so the enrichment
        # survives it, while a search that requests none still emits the default
        # six-key shape (parity with the legacy path).
        record_keys = (
            *_ENTITY_RECORD_KEYS,
            *_requested_enrichment(parsed_result_fields),
        )
        entities = [
            {key: rec.get(key) for key in record_keys}
            for rec in _as_record_list(component_result.get("entities"))
        ]
        entity_has_more = bool(component_result.get("entity_has_more", False))
        entity_payload: dict[str, Any] = {
            "results": entities,
            "total_matches": int(
                component_result.get("entity_total_matches", len(entities)) or 0
            ),
            "has_more": entity_has_more,
            "next_offset": (req.offset + req.limit) if entity_has_more else None,
            "offset": req.offset,
            "limit": req.limit,
            "count": len(entities),
            "search_type": "exact_match" if req.exact_match else "fuzzy_search",
        }
        domain_filter = _normalized_domain_filter(req.domain_filter)
        if domain_filter:
            entity_payload["domain_filter"] = domain_filter
        # Order mirrors _search_regular: group by domain first (it projects its
        # own records), then project the flat results[].
        _apply_by_domain_grouping(
            entity_payload,
            entities,
            req.group_by_domain,
            req.per_domain_limit,
            parsed_result_fields,
        )
        _apply_result_fields_to_response(entity_payload, parsed_result_fields)
        _apply_search_outcome(response, "entities", entity_payload)

    if req.body_eligible:
        config_has_more = bool(component_result.get("config_has_more", False))
        config_payload: dict[str, Any] = {
            "total_matches": int(component_result.get("config_total_matches", 0) or 0),
            "has_more": config_has_more,
            "next_offset": (req.offset + req.limit) if config_has_more else None,
        }
        for bucket in _CONFIG_BUCKETS:
            if bucket in component_result:
                config_payload[bucket] = [
                    _normalize_component_config_record(bucket, rec, req.include_config)
                    for rec in _as_record_list(component_result.get(bucket))
                ]
        _apply_search_outcome(response, "configs", config_payload)

    # The component reports a single overall partial flag (design § 1). In-process
    # joins are effectively never partial, but a body too large to serialize can
    # set it — carry it through honestly rather than assuming completeness.
    if component_result.get("partial"):
        response["partial"] = True
        reason = component_result.get("partial_reason")
        if isinstance(reason, str) and reason:
            _merge_partial_reason(response, reason)

    # The component also surfaces intentional per-surface diagnostics (e.g. a
    # config domain it couldn't read) separately from the overall partial flag.
    # A non-empty diagnostics map is a genuine incompleteness, so mark the
    # response partial and fold a readable clause into partial_reason rather than
    # dropping the component's signal.
    diagnostics = component_result.get("diagnostics")
    if isinstance(diagnostics, dict):
        diag_reason = _format_search_diagnostics(diagnostics)
        if diag_reason:
            response["partial"] = True
            _merge_partial_reason(response, diag_reason)

    _merge_component_visibility_warnings(response, component_result)

    response["count"] = len(response["entities"]) + sum(
        len(response.get(bucket, [])) for bucket in _CONFIG_BUCKETS
    )
    _synthesize_combined_pagination(response)
    _mirror_partial_to_warnings(response)
    return _project_response_fields(response, req.parsed_fields)


@dataclass(frozen=True)
class _OverviewInputs:
    """Resolved ``ha_get_overview`` inputs threaded to the routing/assembly.

    All display params (``detail_level`` … ``offset``) stay server-side — the
    component returns raw slices independent of them; the two include-flags gate
    which slices it bothers to snapshot.
    """

    detail_level: str
    max_entities_per_domain: int | None
    include_state: bool | None
    include_entity_id: bool | None
    domains_filter: list[str] | None
    limit: int | None
    offset: int
    include_notifications: bool
    include_dismissed_repairs: bool


@dataclass(frozen=True)
class _OverviewSlices:
    """The component's raw overview slices, adapted to the assembly's shapes.

    ``registry_slices`` bundles the five ``get_system_overview`` inputs — bare
    ``states`` / ``services`` lists plus the three registries re-wrapped in the
    ``{success, result}`` envelope ``_extract_registry_list`` / ``load_hidden_set``
    unwrap. ``config`` is the bare ``get_config()`` dict. ``notifications`` and
    ``repairs`` are re-wrapped in the WS ``{success, result}`` envelope the
    ``_fetch_*`` helpers unwrap (``repairs`` nested under ``result.issues``).
    """

    registry_slices: dict[str, Any]
    config: dict[str, Any]
    notifications: dict[str, Any]
    repairs: dict[str, Any]


def _build_component_overview_request(inputs: _OverviewInputs) -> dict[str, Any]:
    """Translate resolved ha_get_overview inputs into an ``ha_mcp_tools/overview`` request.

    The component returns raw slices independent of the display params
    (``detail_level`` / ``domains`` / ``limit`` / ``offset`` /
    ``max_entities_per_domain`` / ``include_state`` / ``include_entity_id`` stay
    server-side — the server assembles), so only the two fetch-gating flags cross
    the wire: ``include_notifications`` mirrors the wrapper's flag;
    ``include_repairs`` is always ``True`` because the wrapper always assembles
    repairs (``include_dismissed_repairs`` only filters dismissed ones
    server-side, it never skips the fetch).
    """
    return {
        "include_notifications": inputs.include_notifications,
        "include_repairs": True,
    }


def _wrap_registry(slice_value: Any) -> dict[str, Any]:
    """Wrap a bare registry list in the ``{success, result}`` envelope the assembly expects."""
    return {
        "success": True,
        "result": slice_value if isinstance(slice_value, list) else [],
    }


# The always-present overview slices the component returns independent of any
# request flag. Each must be a list; ``config`` (a dict) is checked separately.
# A missing/malformed member means the component couldn't assemble a trustworthy
# snapshot, so the caller falls back to the legacy fetch path rather than serve a
# silently-degraded overview.
_REQUIRED_OVERVIEW_LIST_SLICES = (
    "states",
    "services",
    "area_registry",
    "entity_registry",
    "device_registry",
)


def _build_overview_slices(component_result: dict[str, Any]) -> _OverviewSlices | None:
    """Adapt the component's BARE overview slices into the assembly's shapes.

    The component returns bare in-process data (no ``{success, result}`` WS
    wrapper — design § ha_mcp_tools/overview); the server's ``get_system_overview``
    + ``_fetch_*`` were written against the wrapped REST/WS payloads, so the three
    registries and the notifications/repairs reads are re-wrapped here at the
    seam. ``states`` / ``services`` / ``config`` already match their bare
    ``get_states()`` / ``get_services()`` / ``get_config()`` shapes.

    Returns ``None`` (⇒ legacy fallback) when the snapshot can't be trusted: any
    required slice missing/malformed (see ``_REQUIRED_OVERVIEW_LIST_SLICES`` plus
    ``config``), or the component reported a non-empty ``slice_errors`` list (a
    per-slice read failure it surfaced instead of silently emptying). The
    flag-gated ``notifications`` / ``repairs`` slices stay lenient — absent or
    malformed degrades to empty, matching a request that never asked for them.
    """
    result = component_result if isinstance(component_result, dict) else {}

    slice_errors = result.get("slice_errors")
    if isinstance(slice_errors, list) and slice_errors:
        return None
    for key in _REQUIRED_OVERVIEW_LIST_SLICES:
        if not isinstance(result.get(key), list):
            return None
    config = result.get("config")
    if not isinstance(config, dict):
        return None

    notifications = result.get("notifications")
    repairs = result.get("repairs")
    return _OverviewSlices(
        registry_slices={
            "states": result["states"],
            "services": result["services"],
            "area_registry": _wrap_registry(result["area_registry"]),
            "entity_registry": _wrap_registry(result["entity_registry"]),
            "device_registry": _wrap_registry(result["device_registry"]),
        },
        config=config,
        notifications={
            "success": True,
            "result": notifications if isinstance(notifications, list) else [],
        },
        repairs={
            "success": True,
            "result": {"issues": repairs if isinstance(repairs, list) else []},
        },
    )


def _normalize_regular_search_result(
    result: dict[str, Any],
    search_type: str,
    domain_filter: str | None,
    offset: int,
    limit: int,
) -> None:
    """Normalise a regular search result dict in-place: rename keys and fill pagination."""
    if "matches" in result:
        result["results"] = result.pop("matches")
    result.pop("is_truncated", None)
    if domain_filter:
        result["domain_filter"] = domain_filter
    result.setdefault("offset", offset)
    result.setdefault("limit", limit)
    result.setdefault("count", len(result.get("results", [])))
    if "has_more" not in result:
        total = result.get("total_matches", 0)
        result["has_more"] = (result["offset"] + result["count"]) < total
        result["next_offset"] = result["offset"] + limit if result["has_more"] else None
    result["search_type"] = search_type


def _build_domain_only_by_domain(
    domain: str,
    results: list[dict[str, Any]],
    per_domain_limit: int | None,
    parsed_result_fields: list[str] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Build the by_domain dict for domain-listing mode (all results are one domain)."""
    items = results[:per_domain_limit] if per_domain_limit is not None else results
    if parsed_result_fields is not None:
        items = _project_records(items, parsed_result_fields)
    return {domain: items}


def _normalize_state_filter(state_filter: str | None) -> str | None:
    """Strip whitespace and lowercase a state_filter; collapse empty strings to None."""
    if state_filter is not None:
        state_filter = state_filter.strip().lower()
        if not state_filter:
            state_filter = None
    return state_filter


def _validate_entity_search_params(
    query: str | None,
    domain_filter: str | None,
    area_filter: str | None,
    result_fields: Any,
) -> tuple[str, str | None, str | None, list[str] | None]:
    """Validate and normalise inputs for entity search; returns (query, domain_filter, area_filter, parsed_result_fields)."""
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

    query = query or ""
    # HA domains are canonically lowercase, no whitespace; agents that capitalize
    # ("Lights") or pad ("  light  ") would hit a silent zero-result against the
    # prefix match downstream. Strip-then-lowercase before validation so a
    # whitespace-only filter ("   ") collapses to "" and fails the at-least-one-set
    # check rather than passing it and falling through to a no-op fuzzy search.
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
    return query, domain_filter, area_filter, parsed_result_fields


def _missing_entity_exc(entity_id: str) -> HomeAssistantAPIError:
    """A synthetic 404 for an id the component's ``states`` read reports absent.

    Classifying a component-reported miss through the same
    ``exception_to_structured_error`` path the legacy per-id REST 404 uses makes
    the missing-id error byte-identical on both backends (ENTITY_NOT_FOUND with
    the entity_id context; the response-level ``ha_search()`` suggestion still
    fires), without the server issuing a REST call it just avoided.
    """
    return HomeAssistantAPIError(
        f"API error: 404 - Entity {entity_id} not found", status_code=404
    )


def _accumulate_state_results(
    unique_ids: list[str],
    results: list[dict[str, Any]],
    parsed_fields: list[str] | None,
    parsed_attribute_keys: list[str] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Process asyncio.gather results into (states dict, errors list, attr_warnings list)."""
    states: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    attr_warns: list[str] = []
    for eid, result in zip(unique_ids, results, strict=True):
        if result.get("success") is True and "state" in result:
            state_record, attr_warn = _project_entity(
                result["state"], parsed_fields, parsed_attribute_keys
            )
            states[eid] = state_record
            if attr_warn and attr_warn not in attr_warns:
                attr_warns.append(attr_warn)
        else:
            error_detail = result.get("error")
            if error_detail is None:
                error_detail = {"code": "INTERNAL_ERROR", "message": "Unknown error"}
            errors.append(
                {
                    "entity_id": result.get("entity_id", eid),
                    "error": error_detail,
                }
            )
    return states, errors, attr_warns


def _build_bulk_states_response(
    states: dict[str, Any],
    errors: list[dict[str, Any]],
    attr_warns: list[str],
    attribute_keys_no_effect: bool,
) -> dict[str, Any]:
    """Build the bulk-state response dict from accumulated states, errors, and warnings."""
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
    for _w in attr_warns:
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
    return response


def _raise_gather_exceptions(
    state_result: Any, registry_result: Any, device_result: Any
) -> None:
    """Re-raise fatal exceptions captured by an ``asyncio.gather(..., return_exceptions=True)``.

    ``state_result`` failure is always fatal. Auth/connection errors must
    propagate so the agent sees "your token is invalid" instead of "zero
    entities matched". ``CancelledError`` on the registry/device results
    comes through gather as a captured exception even when
    ``return_exceptions=True``; it has to propagate or the canceller waits
    forever. Other registry/device failures are tolerated by the caller (we
    just lose the hidden filter).
    """
    if isinstance(state_result, BaseException):
        raise state_result
    if isinstance(registry_result, asyncio.CancelledError):
        raise registry_result
    if isinstance(device_result, asyncio.CancelledError):
        raise device_result


def _match_exact_search_entity(
    entity: dict[str, Any],
    query_lower: str,
    domain_filter: str | None,
    visibility_hidden: set[str],
    hidden_ids: set[str],
    include_hidden: bool,
) -> dict[str, Any] | None:
    """Score a single entity for ``_exact_match_search``, or None if it's excluded/no match."""
    entity_id = entity.get("entity_id", "")
    if entity_id in visibility_hidden:
        return None
    is_hidden = entity_id in hidden_ids
    if is_hidden and not include_hidden:
        return None
    attributes = entity.get("attributes") or {}
    friendly_name = attributes.get("friendly_name", entity_id)
    domain = entity_id.split(".")[0] if "." in entity_id else ""

    # Apply domain filter if provided
    if domain_filter and domain != domain_filter:
        return None

    # Check for exact substring match in entity_id or friendly_name
    if (
        query_lower not in entity_id.lower()
        and query_lower not in friendly_name.lower()
    ):
        return None

    is_exact = query_lower == entity_id.lower() or query_lower == friendly_name.lower()
    score = 100 if is_exact else 80
    if is_hidden:
        score = apply_hidden_penalty(score, "_hidden")
    return {
        "entity_id": entity_id,
        "friendly_name": friendly_name,
        "domain": domain,
        "state": entity.get("state", "unknown"),
        "score": score,
        "match_type": "exact_match",
    }


async def _exact_match_search(
    client: Any,
    query: str,
    domain_filter: str | None,
    limit: int,
    offset: int = 0,
    include_hidden: bool = True,
    state_filter: str | None = None,
    *,
    prefetched_states: list[dict[str, Any]] | None = None,
    prefetched_registry: Any = None,
) -> dict[str, Any]:
    """
    Search entities by substring on entity_id + friendly_name.

    Used both as the ``exact_match=True`` primary path and as the
    fallback when fuzzy search raises. In addition to ``client.get_states()``,
    also queries the entity registry via WebSocket to identify
    ``hidden_by`` entities: by default they remain in results but
    receive a score penalty so visible matches sort first; pass
    ``include_hidden=False`` to filter them out entirely.

    ``prefetched_states`` / ``prefetched_registry`` are the snapshots the
    ha_search orchestrator shares with the config branch when both run (``None``
    = fetch here). The device registry is fetched only when the loaded visibility
    config has an area/label dimension that consumes it.
    """
    # Fetch states + entity registry in parallel (unless the orchestrator already
    # shared them). Registry-list failure is tolerated (we just lose the hidden
    # filter); states-fetch failure is fatal — auth/connection errors must
    # propagate so the agent sees "your token is invalid" instead of "zero
    # entities matched". The device registry is gated: it only feeds the
    # visibility area/label dimensions, so a default/area-free config skips it.
    need_device = await device_registry_needed_for_visibility()
    fetch_coros: list[Any] = []
    fetch_slots: list[str] = []
    if prefetched_states is None:
        fetch_coros.append(client.get_states())
        fetch_slots.append("states")
    if prefetched_registry is None:
        fetch_coros.append(
            client.send_websocket_message({"type": "config/entity_registry/list"})
        )
        fetch_slots.append("registry")
    if need_device:
        fetch_coros.append(
            client.send_websocket_message({"type": "config/device_registry/list"})
        )
        fetch_slots.append("device")
    fetched = (
        await asyncio.gather(*fetch_coros, return_exceptions=True)
        if fetch_coros
        else []
    )
    slots = dict(zip(fetch_slots, fetched, strict=True))
    state_result: Any = (
        prefetched_states if prefetched_states is not None else slots.get("states")
    )
    registry_result: Any = (
        prefetched_registry
        if prefetched_registry is not None
        else slots.get("registry")
    )
    device_result: Any = slots.get("device")
    _raise_gather_exceptions(state_result, registry_result, device_result)
    all_entities = state_result
    hidden_ids = _build_hidden_ids(registry_result)
    # Opt-in visibility filter: a hard exclude (unlike the hidden_by score
    # penalty). Fails open — load_hidden_set returns an empty set on any
    # config/load error, so a bad config never blanks results. Do NOT wrap in
    # try/except here, or the failure mode inverts to fail-closed (hide all).
    # states + client let the allowlist reach states-only entities and the
    # opt-in Assist-exposure dimension fetch its data; the device registry lets
    # the area/label dimensions match a device-bound entity by its device.
    visibility_hidden, visibility_warnings = await load_hidden_set(
        registry_result, state_result, client, device_result
    )

    query_lower = query.lower().strip()

    results = []
    for entity in all_entities:
        match = _match_exact_search_entity(
            entity,
            query_lower,
            domain_filter,
            visibility_hidden,
            hidden_ids,
            include_hidden,
        )
        if match is not None:
            results.append(match)

    if state_filter:
        results = [r for r in results if r.get("state") == state_filter]

    # Sort by score descending, tie-break on entity_id for stable
    # pagination when many results share a score (visible substring
    # hits at 100, hidden ones at 80 etc).
    results.sort(key=lambda x: (-x["score"], x["entity_id"]))
    paginated = results[offset : offset + limit]
    return merge_visibility_warnings(
        {
            "success": True,
            "query": query,
            **_build_pagination_metadata(len(results), offset, limit, paginated),
            "results": paginated,
            "search_type": "exact_match",
        },
        visibility_warnings,
    )


class SearchTools:
    """Tool class providing search and entity discovery capabilities."""

    def __init__(self, client: Any, smart_tools: Any) -> None:
        self._client = client
        self._smart_tools = smart_tools

    @tool(
        name="ha_search",
        tags={"Search & Discovery"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Search",
        },
    )
    @log_tool_usage
    async def ha_search(
        self,
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
            JSON_STRING_COERCION,
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
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project each entity-registry record to only the specified "
                    'keys (e.g. ["entity_id", "state"]). None = full records. '
                    "Base keys: entity_id, friendly_name, domain, state, score, "
                    "match_type. Opt-in enrichment keys (joined on request): "
                    "area, floor, labels, aliases. An unknown key is rejected."
                ),
            ),
        ] = None,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Project the response to the named top-level keys "
                    '(e.g. ["entities", "automations"]); None = full '
                    "response. Diagnostic / pagination keys are always "
                    "retained so projection cannot hide partial / error "
                    "state. Distinct from `result_fields` (which projects "
                    "each entity record's keys). Available keys: success, "
                    "query, entities, automations, scripts, scenes, "
                    "helpers, dashboards, search_types, search_type, "
                    "entity_total_matches, config_total_matches, count, "
                    "offset, limit, has_more, next_offset, "
                    "entity_has_more, entity_next_offset, "
                    "config_has_more, config_next_offset, by_domain, "
                    "state_filter_note, area_names, domain_filter, "
                    "area_filter, message, warnings, errors, partial, "
                    "partial_reason."
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

        Two surfaces run in parallel and return tagged results:
          - **entities**: entity-registry matches (entity_id, friendly name,
            area). Filter with `domain_filter`/`area_filter`; omit `query` to
            enumerate a domain/area.
          - **automations / scripts / scenes / helpers / dashboards**: matches
            *inside* config definitions — triggers, actions, sequences, scene
            entity-sets, helper bodies, dashboard cards. Driven by `query`;
            narrow with `search_types`.

        Use this whenever you need to find something in HA without deciding
        entity-name vs config-body search up front.

        When NOT to use:
          - To read a known entity_id's state: use `ha_get_state` (cheaper).
          - To inspect one automation/script/scene config by id: use the
            matching `ha_config_get_*`.
          - To list installed add-ons: use `ha_get_addon`.

        Config-body search is skipped when `domain_filter`/`area_filter`/
        `state_filter` signal entity-only intent (keeping name lookups off the
        expensive backend); a `warnings[]` entry names the skip. Pass
        `search_types=[...]` to force config search.

        Caveats:
          - `partial: True` means results are NOT exhaustive — a surface raised,
            or the config-body branch lost data (per-id time budget exhausted,
            an individual fetch failed, or a helper-type list fetch failed).
            Empty buckets with `partial: True` mean "search failed", not "no
            results". The cause is in `partial_reason`, also mirrored into
            `warnings[]` with an "incomplete results: " prefix. Do not treat a
            partial response as complete.
          - `count` is items in this response (post-pagination), not corpus
            totals — use `entity_total_matches` + `config_total_matches`.
          - `limit`/`offset` apply per-surface. Flat `has_more`/`next_offset`
            page the next call (iterate `offset = next_offset`); per-surface
            `entity_*`/`config_*` variants show which surface still has results.

        For parameters, schema, and worked examples, see ha_get_skill_guide.

        Examples:
            - List sensors in an area: ha_search(domain_filter="sensor", area_filter="Living Room")
            - Find a light by name: ha_search("kitchen", domain_filter="light")
            - Which automations use an entity: ha_search("light.bed_light")
            - Scenes touching a light: ha_search("light.kitchen", search_types=["scene"])
            - Narrow the response to the entity bucket: ha_search("kitchen", fields=["entities"])
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

        # Validate result_fields once up front so BOTH serving paths reject an
        # unknown enrichment key identically (the sub-paths re-parse the same raw
        # value for their own projection).
        try:
            parsed_result_fields = parse_string_list_param(
                result_fields, "result_fields", allow_csv=True
            )
        except ValueError as exc:
            raise_tool_error(
                create_validation_error(str(exc), parameter="result_fields")
            )
        _validate_result_field_names(parsed_result_fields)

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

        req = _ResolvedSearch(
            query=query,
            query_text=query_text,
            domain_filter=domain_filter,
            area_filter=area_filter,
            state_filter=state_filter,
            parsed_search_types=parsed_search_types,
            parsed_fields=parsed_fields,
            result_fields=result_fields,
            limit=limit,
            offset=offset,
            exact_match=exact_match,
            include_hidden=include_hidden,
            include_config=include_config,
            group_by_domain=group_by_domain,
            per_domain_limit=per_domain_limit,
            config_time_budget=config_time_budget,
            registry_eligible=registry_eligible,
            body_eligible=body_eligible,
            body_skipped_by_intent_gate=body_skipped_by_intent_gate,
        )

        # Prefer the custom component's in-process unified search when it
        # advertises the capability: one WS round-trip replaces the multi-fetch
        # legacy pipeline. Route all-or-nothing per command and fall back
        # cleanly when the component is absent, downlevel, or errors — the
        # taxonomy lives in ``_ha_search_via_component``.
        #
        # Only QUERY-DRIVEN searches route through the component. The listing
        # modes — empty/whitespace query with domain_filter (legacy
        # ``search_type: domain_listing``) and any area_filter search (legacy
        # ``area_only`` / ``area_filtered_query``, with their own area-shaped
        # response keys) — keep the legacy path: their response contracts
        # differ per mode, and after the request-dedup work they are cheap
        # registry-only calls, so the component round-trip buys nothing worth
        # the shape risk. A ``search_types`` naming a surface the component
        # lacks (``dashboard``) also stays legacy — see
        # ``_component_serves_search_types`` (issue #2008).
        #
        # Entity-visibility gate. A plain ``search`` component applies no
        # filtering, so an install with an ACTIVE visibility filter would leak
        # hidden entities through the fast path. The ``search_visibility``
        # capability closes that: a component that advertises it accepts the raw
        # hide config (``VisibilityConfig.to_wire``) as the ``visibility`` param
        # and excludes hidden entities before its own counts/pagination, exactly
        # as the legacy path does — so a visibility-active install can still take
        # the fast path. Without the capability an active filter stays on the
        # legacy path; with no active filter the plain ``search`` route runs with
        # no ``visibility`` param (old components keep working). ``ha_get_overview``
        # needs no analogous gate — it re-applies the filter server-side over the
        # component's raw slices. Checked only when the component would otherwise
        # serve, so the common (no-component / filter-off) install pays nothing.
        if (
            req.query_text
            and not (req.area_filter or "").strip()
            and _component_serves_search_types(req)
        ):
            caps = await get_component_caps(self._client)
            if component_supports(caps, "search"):
                (
                    route_component,
                    visibility,
                ) = await self._resolve_component_search_visibility(caps)
                if route_component:
                    component_response = await self._ha_search_via_component(
                        req, ctx, visibility=visibility
                    )
                    if component_response is not None:
                        return component_response

        return await self._legacy_ha_search(req, ctx)

    async def _resolve_component_search_visibility(
        self, caps: Any
    ) -> tuple[bool, dict[str, Any] | None]:
        """Decide the ha_search route under the entity-visibility gate.

        Returns ``(route_component, visibility_param)`` for a caller that has
        already confirmed the component advertises ``search``:

        - filter inactive → ``(True, None)``: the plain component search, no
          ``visibility`` param (parity with a pre-``search_visibility`` component).
        - filter active + ``search_visibility`` capability + config serialized →
          ``(True, <wire dict>)``: the component applies the hide dimensions
          in-process.
        - filter active without the capability, or the config could not be loaded
          → ``(False, None)``: the legacy path applies the filter server-side
          before the counts/pagination (fail-closed to legacy on a bad config,
          matching ``visibility_state_and_wire``'s fail-closed pairing).

        ``visibility_state_and_wire`` loads the config once for both the active
        gate and its wire form, instead of the active check and the wire fetch
        each hitting the (memoized) config read separately.
        """
        active, visibility = await visibility_state_and_wire()
        if not active:
            return True, None
        if component_supports(caps, "search_visibility") and visibility is not None:
            return True, visibility
        return False, None

    async def _ha_search_via_component(
        self,
        req: _ResolvedSearch,
        ctx: Context | None,
        *,
        visibility: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Serve ha_search from the component; ``None`` ⇒ run the legacy path.

        ``visibility`` is the serialized hide config passed to a
        ``search_visibility``-capable component so it applies the entity-
        visibility filter in-process (``None`` ⇒ no filter / not supported ⇒ the
        component surfaces every match, correct only because the caller routes
        here solely when the filter is inactive or the component can apply it).

        Error taxonomy (design § 4):

        - ``unknown_command`` (component downgraded mid-session, so the cached
          positive caps are stale): invalidate the caps and return ``None`` so
          the caller falls back **silently** — an expected, non-actionable
          transition.
        - any other ``HomeAssistantCommandError`` (a component handler bug) or a
          ``HomeAssistantCommandTimeout`` (the component WS search timed out):
          serve the correct result from the legacy path, append a ``warnings[]``
          entry, and ``log.warning`` — correct results now, breakage visible.
        - ``HomeAssistantConnectionError`` - a pooled-WS drop, or a failed
          (re)connect: served the same way. The legacy path reads
          ``/api/states`` over REST and the entity registry through the
          ``send_websocket_message`` bridge, so a component-side fault degrades
          to partial results rather than escaping. The bridge shares this
          pooled connection, so a dead transport raises there too (#1947) and
          the registry-unavailable warning names what was skipped.
        """
        try:
            raw = await self._send_component_search(req, visibility)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            legacy = await self._legacy_ha_search(req, ctx)
            legacy.setdefault("warnings", []).append(
                f"component search path failed ({exc}); served via legacy path"
            )
            logger.warning("ha_mcp_tools/search failed; fell back to legacy: %r", exc)
            return legacy
        except Exception as exc:
            legacy = await self._legacy_ha_search(req, ctx)
            legacy.setdefault("warnings", []).append(
                f"component search connection error ({exc}); served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/search connection error; fell back to legacy: %r", exc
            )
            return legacy
        return _shape_component_search_response(req, raw.get("result") or {})

    async def _send_component_search(
        self, req: _ResolvedSearch, visibility: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/search`` command over the per-client WebSocket.

        ``visibility`` (the serialized hide config) is attached only when set, so
        a plain-``search`` component (which lacks the param in its schema) never
        receives it.
        """
        ws = await get_websocket_client(
            url=self._client.base_url, token=self._client.token
        )
        request = _build_component_search_request(req)
        if visibility is not None:
            request["visibility"] = visibility
        return await ws.send_command("ha_mcp_tools/search", **request)

    async def _legacy_ha_search(
        self, req: _ResolvedSearch, ctx: Context | None
    ) -> dict[str, Any]:
        """Run the multi-fetch REST/WS ha_search orchestration (fallback path).

        Behaviourally unchanged from the pre-component implementation: the two
        surfaces fan out over shared ``/api/states`` + entity-registry
        snapshots, gather with per-surface partial handling, and assemble the
        flat dual-surface envelope.
        """
        # When both branches run they each independently fetch the full state
        # machine (/api/states) and the entity-registry list; fetch each once and
        # thread the snapshots down so the two branches share one of each instead
        # of fetching two.
        shared_states, shared_registry = await _prefetch_shared_search_snapshots(
            self._client,
            registry_eligible=req.registry_eligible,
            body_eligible=req.body_eligible,
        )

        registry_callable_kwargs: dict[str, Any] = {
            "query": req.query_text or None,
            "domain_filter": req.domain_filter,
            "area_filter": req.area_filter,
            "limit": req.limit,
            "offset": req.offset,
            "exact_match": req.exact_match,
            "include_hidden": req.include_hidden,
            "group_by_domain": req.group_by_domain,
            "per_domain_limit": req.per_domain_limit,
            "state_filter": req.state_filter,
            "result_fields": req.result_fields,
            "prefetched_states": shared_states,
            "prefetched_registry": shared_registry,
        }

        tasks: list[Any] = []
        labels: list[str] = []
        if req.registry_eligible:
            tasks.append(self._ha_search_entities(**registry_callable_kwargs))
            labels.append("entities")
        if req.body_eligible:
            tasks.append(
                self._ha_deep_search(
                    query=req.query_text,
                    search_types=req.parsed_search_types,
                    limit=req.limit,
                    offset=req.offset,
                    include_config=req.include_config,
                    exact_match=req.exact_match,
                    config_time_budget=req.config_time_budget,
                    ctx=ctx,
                    prefetched_states=shared_states,
                    prefetched_registry=shared_registry,
                )
            )
            labels.append("configs")

        # ``return_exceptions=True`` captures sub-task exceptions; the gather
        # call itself only raises if the orchestrator's own coroutine is
        # cancelled before the tasks complete.
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        response = _new_search_response(req.query, req.parsed_search_types)
        # Surface the body-skip so a caller who actually wanted config
        # matches alongside the entity scope can see why their request
        # returned no automations / scripts / etc.
        _emit_intent_skip_warning(response, req.body_skipped_by_intent_gate)
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
            _apply_search_outcome(response, label, outcome)

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

        return _project_response_fields(response, req.parsed_fields)

    async def _ha_search_entities(
        self,
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
                    "None = full records (default). "
                    "Base keys: entity_id, friendly_name, domain, state, score, match_type. "
                    "Opt-in enrichment keys (joined on request): area, floor, labels, aliases. "
                    "An unknown key is rejected."
                ),
            ),
        ] = None,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
    ) -> dict[str, Any]:
        """Search for entities (lights, sensors, switches, etc.) by name, domain, or area.

        When NOT to use: for searching inside automation, script, helper, or dashboard
        *configurations* (e.g. which automations call a service or reference an entity),
        use `ha_deep_search`.

        To enumerate all entities of a domain, omit `query` and pass `domain_filter`. For
        example, `ha_search_entities(domain_filter="calendar")` lists all calendars. At
        least one of `query`, `domain_filter`, or `area_filter` must be set.

        ``prefetched_states`` / ``prefetched_registry`` are the orchestrator's
        shared snapshots; they only reach the regular (non-area, non-domain-only)
        path, which is the only one that can run alongside the config branch.
        """
        query, domain_filter, area_filter, parsed_result_fields = (
            _validate_entity_search_params(
                query, domain_filter, area_filter, result_fields
            )
        )
        group_by_domain_bool = group_by_domain
        exact_match_bool = exact_match
        include_hidden_bool = include_hidden
        per_domain_limit_int = per_domain_limit

        try:
            state_filter = _normalize_state_filter(state_filter)

            if area_filter:
                area_result = await self._smart_tools.get_entities_by_area(
                    area_filter,
                    group_by_domain=True,
                    include_hidden=include_hidden_bool,
                )
                if query and query.strip():
                    area_search = await self._search_area_with_query(
                        query,
                        area_filter,
                        area_result,
                        domain_filter,
                        state_filter,
                        limit,
                        offset,
                        group_by_domain_bool,
                        per_domain_limit_int,
                        parsed_result_fields,
                    )
                else:
                    area_search = await self._search_area_only(
                        area_result,
                        area_filter,
                        domain_filter,
                        state_filter,
                        limit,
                        offset,
                        group_by_domain_bool,
                        per_domain_limit_int,
                        parsed_result_fields,
                    )
                # The three area builders rebuild a fresh response dict and do not
                # carry area_result's warnings; forward them here in one place so a
                # visibility/registry degradation on the area path is not silently
                # dropped (mirrors the non-area path's merge_visibility_warnings).
                # The builders now return the search dict directly (no
                # add_timezone_metadata wrapper), so warnings merge into that dict;
                # the ``["data"]`` unwrap is a defensive holdover for a hypothetical
                # future wrapped payload.
                warn_target = (
                    area_search["data"]
                    if isinstance(area_search, dict) and "data" in area_search
                    else area_search
                )
                merge_visibility_warnings(warn_target, area_result.get("warnings", []))
                return area_search

            if domain_filter and (not query or not query.strip()):
                return await self._search_domain_only(
                    query,
                    domain_filter,
                    state_filter,
                    limit,
                    offset,
                    include_hidden_bool,
                    group_by_domain_bool,
                    per_domain_limit_int,
                    parsed_result_fields,
                )

            return await self._search_regular(
                query,
                domain_filter,
                state_filter,
                limit,
                offset,
                exact_match_bool,
                include_hidden_bool,
                group_by_domain_bool,
                per_domain_limit_int,
                parsed_result_fields,
                prefetched_states=prefetched_states,
                prefetched_registry=prefetched_registry,
            )

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
            return None  # unreachable: raise_tool_error always raises
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

    async def _fetch_entity_enrichment(
        self,
        entity_ids: list[str],
        requested: tuple[str, ...],
        prefetched_entries: dict[str, dict[str, Any]] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Join area/floor/label NAMES + aliases for entity_ids (legacy enrichment).

        Generalises the area-mode alias join (:meth:`_fetch_area_entity_entries`):
        one ``config/entity_registry/get_entries`` gives each id's aliases + area_id
        + label ids + device_id, and the area/floor/label registry lists resolve
        those ids to NAMES (the device registry supplies device-inherited
        area/labels, matching the component's ``_registry_enrichment``). Only the
        registries a requested field actually needs are fetched — an aliases-only
        request skips the four ``*_registry/list`` reads, and a caller that already
        holds the registry entries (the area-mode haystack fetch) passes them as
        ``prefetched_entries`` so no second ``get_entries`` round-trip is made.
        Each fetch is fault-tolerant (``return_exceptions=True`` + the ``_ws_*``
        guards degrade a failed list to an empty index), so a registry hiccup drops
        that field to empty rather than failing the search — but a failed read is no
        longer silent: it is logged and reported so the join does not emit
        present-but-null fields indistinguishable from a genuinely unassigned
        entity. Returns ``({entity_id: {requested field: value}}, warnings)`` where
        ``warnings`` is non-empty only when a needed read failed.
        """
        if not entity_ids or not requested:
            return {}, []
        need_names = bool(set(requested) & {"area", "floor", "labels"})
        coros: list[Any] = []
        if prefetched_entries is None:
            coros.append(
                self._client.send_websocket_message(
                    {
                        "type": "config/entity_registry/get_entries",
                        "entity_ids": entity_ids,
                    }
                )
            )
        if need_names:
            coros.extend(
                self._client.send_websocket_message({"type": command})
                for command in (
                    "config/area_registry/list",
                    "config/floor_registry/list",
                    "config/label_registry/list",
                    "config/device_registry/list",
                )
            )
        fetched = await asyncio.gather(*coros, return_exceptions=True)
        for item in fetched:
            # A cancelled read must propagate, not degrade to an empty field.
            if isinstance(item, asyncio.CancelledError):
                raise item
        failed_reads: list[str] = []
        if prefetched_entries is None:
            if _ws_read_failed(fetched[0]):
                failed_reads.append("entity registry entries")
            entries = _ws_result_map(fetched[0])
            names = fetched[1:]
        else:
            entries = prefetched_entries
            names = fetched
        if need_names:
            failed_reads.extend(
                label
                for label, resp in zip(
                    (
                        "area registry",
                        "floor registry",
                        "label registry",
                        "device registry",
                    ),
                    names,
                    strict=True,
                )
                if _ws_read_failed(resp)
            )
        areas = _ws_registry_index(names[0], "area_id") if need_names else {}
        floors = _ws_registry_index(names[1], "floor_id") if need_names else {}
        labels = _ws_registry_index(names[2], "label_id") if need_names else {}
        devices = _ws_registry_index(names[3], "id") if need_names else {}
        enrichment = {
            eid: _entity_enrichment_fields(
                entries.get(eid) or {}, areas, floors, labels, devices, requested
            )
            for eid in entity_ids
        }
        warnings: list[str] = []
        if failed_reads:
            logger.warning(
                "result_fields_enrichment_failed: %d registry read(s) failed (%s) "
                "for %d entities; area/floor/labels/aliases may be incomplete",
                len(failed_reads),
                ", ".join(failed_reads),
                len(entity_ids),
            )
            warnings.append(
                "result_fields enrichment incomplete: one or more registry reads "
                "failed, so area/floor/labels/aliases may be missing or empty for "
                "some entities"
            )
        return enrichment, warnings

    async def _maybe_enrich_entity_records(
        self,
        records: list[dict[str, Any]],
        parsed_result_fields: list[str] | None,
        prefetched_entries: dict[str, dict[str, Any]] | None = None,
    ) -> list[str]:
        """Add requested area/floor/labels/aliases to entity records in place (opt-in).

        A no-op unless ``result_fields`` names an enrichment field, so the default
        search pays nothing. Records are mutated in place, so a ``by_domain`` view
        built from the same dicts before projection is enriched too. Applied before
        the ``result_fields`` projection so the requested enrichment keys survive
        it. Never withholds results: a failed registry read leaves the enrichment
        fields empty and returns a warning (which the caller surfaces at the top
        level) rather than silently emitting null fields. ``prefetched_entries``
        lets a caller that already fetched the registry entries (area mode's
        haystack fetch) avoid a duplicate ``get_entries`` round-trip. Returns any
        degraded-enrichment warnings (empty on the happy path or when enrichment is
        not requested).
        """
        requested = _requested_enrichment(parsed_result_fields)
        if not requested or not records:
            return []
        entity_ids: list[str] = [
            r["entity_id"] for r in records if isinstance(r.get("entity_id"), str)
        ]
        enrichment, warnings = await self._fetch_entity_enrichment(
            entity_ids, requested, prefetched_entries
        )
        for record in records:
            eid = record.get("entity_id")
            if isinstance(eid, str):
                fields = enrichment.get(eid)
                if fields:
                    record.update(fields)
        return warnings

    async def _fetch_area_entity_entries(
        self,
        area_entity_ids: list[str],
    ) -> dict[str, dict[str, Any]] | None:
        """Fetch entity registry entries for a list of entity IDs in one WS call.

        Returns the full ``get_entries`` result map so the caller can derive the
        alias haystack AND reuse the same entries for opt-in enrichment without a
        second round-trip. ``None`` (NOT an empty map) signals a FAILED read — a
        non-success reply or a malformed payload — so the caller can tell a genuine
        empty-but-successful prefetch (which correctly enriches to empty with no
        warning) from a read failure, and let the enrichment re-fetch and report the
        degradation instead of silently trusting the empty map. An empty ``{}`` is
        returned only for an empty input list.
        """
        entries_map: dict[str, dict[str, Any]] = {}
        if not area_entity_ids:
            return entries_map
        try:
            entries_resp = await self._client.send_websocket_message(
                {
                    "type": "config/entity_registry/get_entries",
                    "entity_ids": area_entity_ids,
                }
            )
            if isinstance(entries_resp, dict) and entries_resp.get("success"):
                return {
                    eid: entry
                    for eid, entry in (entries_resp.get("result", {}) or {}).items()
                    if isinstance(entry, dict)
                }
            logger.warning(
                "alias_enrichment_failed: get_entries returned non-success "
                "for %d area entities (resp=%r)",
                len(area_entity_ids),
                entries_resp,
            )
        except (KeyError, TypeError, AttributeError) as alias_err:
            logger.warning(
                "alias_enrichment_failed: malformed payload for %d area entities (err=%r)",
                len(area_entity_ids),
                alias_err,
            )
        return None

    async def _search_area_with_query(
        self,
        query: str,
        area_filter: str,
        area_result: dict[str, Any],
        domain_filter: str | None,
        state_filter: str | None,
        limit: int,
        offset: int,
        group_by_domain_bool: bool,
        per_domain_limit_int: int | None,
        parsed_result_fields: list[str] | None,
    ) -> dict[str, Any]:
        """Search within area entities using fuzzy matching against a query string."""
        # Collect entities from all matched areas, applying domain_filter if present.
        # _ha_search_entities calls get_entities_by_area with group_by_domain=True,
        # so area_result["areas"][id]["entities"] is always a dict keyed by domain.
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
        # the fuzzy haystack includes them.
        area_entity_ids = sorted(
            e.get("entity_id", "") for e in all_area_entities if e.get("entity_id")
        )
        # ``None`` = the prefetch read FAILED (vs an empty-but-successful map). On
        # failure the haystack loses alias tokens (as before), and passing ``None``
        # as ``prefetched_entries`` below lets the enrichment re-fetch and report the
        # degradation instead of silently trusting an empty map as authoritative.
        entries_map = await self._fetch_area_entity_entries(area_entity_ids)
        aliases_map = {
            eid: (entry.get("aliases") or [])
            for eid, entry in (entries_map or {}).items()
        }

        from ..utils.fuzzy_search import create_fuzzy_searcher

        fuzzy_searcher = create_fuzzy_searcher(threshold=80)

        entities_for_search = [
            {
                "entity_id": entity.get("entity_id", ""),
                "attributes": {"friendly_name": entity.get("friendly_name", "")},
                "state": entity.get("state", "unknown"),
                "_aliases": aliases_map.get(entity.get("entity_id", ""), []),
                "_hidden_by": entity.get("_hidden_by"),
            }
            for entity in all_area_entities
        ]

        matches, total_matches = fuzzy_searcher.search_entities(
            entities_for_search, query, limit, offset
        )

        # Top-level `area_filter` already carries this context for the caller;
        # per-result echo would be redundant and asymmetric vs the other branches.
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

        pagination = _build_pagination_metadata(total_matches, offset, limit, results)

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

        enrich_warnings = await self._maybe_enrich_entity_records(
            results, parsed_result_fields, prefetched_entries=entries_map
        )
        _apply_by_domain_grouping(
            search_data,
            results,
            group_by_domain_bool,
            per_domain_limit_int,
            parsed_result_fields,
        )
        _apply_result_fields_to_response(search_data, parsed_result_fields)
        merge_visibility_warnings(search_data, enrich_warnings)

        # No add_timezone_metadata: entity records carry no timestamp fields, so
        # the enrichment converted nothing and its /api/config fetch was pure
        # waste (the orchestrator discards its metadata wrapper anyway).
        return search_data

    async def _search_area_only_populated(
        self,
        area_result: dict[str, Any],
        area_filter: str,
        domain_filter: str | None,
        state_filter: str | None,
        limit: int,
        offset: int,
        group_by_domain_bool: bool,
        per_domain_limit_int: int | None,
        parsed_result_fields: list[str] | None,
    ) -> dict[str, Any]:
        """Build the response for an area search that returned at least one matched area."""
        all_results: list[dict[str, Any]] = []
        area_names_matched: list[str] = []
        # Iterate ALL fuzzy-matched areas, not just the first.
        # Pre-fix: ``next(iter(...))`` silently dropped every area but one —
        # a query like area_filter="bedroom" against ["bedroom","bedroom_kids"]
        # would return only one area's entities and miss the user's intended
        # one entirely.  Sort area_id keys for deterministic pagination order.
        for area_id in sorted(area_result["areas"]):
            area_data = area_result["areas"][area_id]
            area_names_matched.append(area_data.get("area_name", area_id))
            entities_data = area_data.get("entities") or {}
            for domain, entities in entities_data.items():
                if domain_filter and domain != domain_filter:
                    continue
                all_results.extend(
                    {
                        **public_fields(entity),
                        "domain": domain,
                        "score": apply_hidden_penalty(100, entity.get("_hidden_by")),
                        "match_type": "area_match",
                    }
                    for entity in entities
                )

        all_results.sort(key=lambda x: (-x["score"], x["entity_id"]))
        if state_filter:
            all_results = [r for r in all_results if r.get("state") == state_filter]
        paginated = all_results[offset : offset + limit]

        area_search_data: dict[str, Any] = {
            "success": True,
            "area_filter": area_filter,
            **_build_pagination_metadata(len(all_results), offset, limit, paginated),
            "results": paginated,
            "search_type": "area_only",
            # `area_names` lists every matched area; `area_name` (singular)
            # is kept for backward compatibility with existing callers.
            "area_names": area_names_matched,
            "area_name": (area_names_matched[0] if area_names_matched else area_filter),
        }
        if domain_filter:
            area_search_data["domain_filter"] = domain_filter
        if state_filter is not None:
            area_search_data["state_filter"] = state_filter
        # Match _search_area_only's no-results message pattern when the area
        # resolved but a domain_filter wiped out every entity in it.
        if not all_results and domain_filter:
            area_search_data["message"] = (
                f"No {domain_filter} entities found in area: {area_filter}"
            )

        enrich_warnings = await self._maybe_enrich_entity_records(
            paginated, parsed_result_fields
        )
        _apply_by_domain_grouping(
            area_search_data,
            paginated,
            group_by_domain_bool,
            per_domain_limit_int,
            parsed_result_fields,
        )
        _apply_result_fields_to_response(area_search_data, parsed_result_fields)
        merge_visibility_warnings(area_search_data, enrich_warnings)

        # No add_timezone_metadata — see _search_area_with_query.
        return area_search_data

    async def _search_area_only(
        self,
        area_result: dict[str, Any],
        area_filter: str,
        domain_filter: str | None,
        state_filter: str | None,
        limit: int,
        offset: int,
        group_by_domain_bool: bool,
        per_domain_limit_int: int | None,
        parsed_result_fields: list[str] | None,
    ) -> dict[str, Any]:
        """Return area entities without a query (area-only listing mode)."""
        if area_result.get("areas"):
            return await self._search_area_only_populated(
                area_result,
                area_filter,
                domain_filter,
                state_filter,
                limit,
                offset,
                group_by_domain_bool,
                per_domain_limit_int,
                parsed_result_fields,
            )

        # Empty match: still emit `area_names: []` so callers don't KeyError
        # when they read the field on a zero-match response.
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
        # No add_timezone_metadata — see _search_area_with_query.
        return empty_area_data

    async def _search_domain_only(
        self,
        query: str | None,
        domain_filter: str,
        state_filter: str | None,
        limit: int,
        offset: int,
        include_hidden_bool: bool,
        group_by_domain_bool: bool,
        per_domain_limit_int: int | None,
        parsed_result_fields: list[str] | None,
    ) -> dict[str, Any]:
        """List all entities of a single domain (empty query + domain_filter)."""
        # Fetch states + registry list in parallel. Registry-list failure is
        # tolerated (we just lose the hidden filter); states-fetch failure is
        # fatal — auth/connection errors must propagate. The device registry is
        # gated: it only feeds the visibility area/label dimensions, so a
        # default/area-free config skips the fetch entirely.
        need_device = await device_registry_needed_for_visibility()
        fetch_coros: list[Any] = [
            self._client.get_states(),
            self._client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            ),
        ]
        if need_device:
            fetch_coros.append(
                self._client.send_websocket_message(
                    {"type": "config/device_registry/list"}
                )
            )
        gather_results = await asyncio.gather(*fetch_coros, return_exceptions=True)
        states_result: Any = gather_results[0]
        registry_result: Any = gather_results[1]
        device_result: Any = gather_results[2] if need_device else None
        if isinstance(states_result, BaseException):
            raise states_result
        # CancelledError must propagate; gather captures it like any other
        # exception when return_exceptions=True.
        if isinstance(registry_result, asyncio.CancelledError):
            raise registry_result
        if isinstance(device_result, asyncio.CancelledError):
            raise device_result

        hidden_ids = _build_hidden_ids(registry_result)
        # Opt-in visibility filter: hard exclude, fails open (empty set on any
        # error). Do NOT wrap in try/except or the failure mode inverts. states +
        # client widen the allowlist to states-only entities and drive the opt-in
        # Assist-exposure fetch; the device registry lets the area/label
        # dimensions match a device-bound entity by its device.
        visibility_hidden, visibility_warnings = await load_hidden_set(
            registry_result, states_result, self._client, device_result
        )

        # Filter by domain. Hidden entities are kept by default (with score
        # penalty applied below); ``include_hidden=False`` filters them out.
        # The visibility exclude is applied before pagination so the counts
        # computed below stay coherent with the returned set.
        filtered_entities = [
            e
            for e in states_result
            if (eid := e.get("entity_id", "")).startswith(f"{domain_filter}.")
            and eid not in visibility_hidden
            and (include_hidden_bool or eid not in hidden_ids)
        ]

        # Score: 100 baseline for domain membership (exact, not fuzzy);
        # penalised for hidden entries so they sort below visible peers.
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
            **_build_pagination_metadata(len(scored_entities), offset, limit, results),
            "results": results,
            "search_type": "domain_listing",
            "note": f"Listing all {domain_filter} entities (empty query with domain_filter)",
        }
        if state_filter is not None:
            domain_list_data["state_filter"] = state_filter

        enrich_warnings = await self._maybe_enrich_entity_records(
            results, parsed_result_fields
        )
        _apply_result_fields_to_response(domain_list_data, parsed_result_fields)
        if group_by_domain_bool:
            domain_list_data["by_domain"] = _build_domain_only_by_domain(
                domain_filter, results, per_domain_limit_int, parsed_result_fields
            )

        # No add_timezone_metadata — see _search_area_with_query.
        return merge_visibility_warnings(
            domain_list_data, [*visibility_warnings, *enrich_warnings]
        )

    async def _search_regular(
        self,
        query: str,
        domain_filter: str | None,
        state_filter: str | None,
        limit: int,
        offset: int,
        exact_match_bool: bool,
        include_hidden_bool: bool,
        group_by_domain_bool: bool,
        per_domain_limit_int: int | None,
        parsed_result_fields: list[str] | None,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
    ) -> dict[str, Any]:
        """Perform exact-match or fuzzy entity search (no area/domain-listing shortcuts).

        ``prefetched_states`` / ``prefetched_registry`` are the snapshots the
        ha_search orchestrator shares with the config branch when both run; they
        are threaded into whichever backend this call uses (``None`` = fetch).
        """
        result: dict[str, Any]
        warning: str | None = None
        search_type = "exact_match" if exact_match_bool else "fuzzy_search"

        if exact_match_bool:
            # Exact match mode: substring matching only. No fallback —
            # _exact_match_search only fails when client.get_states() itself
            # fails, in which case any retry is futile.
            result = await _exact_match_search(
                self._client,
                query,
                domain_filter,
                limit,
                offset,
                include_hidden=include_hidden_bool,
                state_filter=state_filter,
                prefetched_states=prefetched_states,
                prefetched_registry=prefetched_registry,
            )
        else:
            # Fuzzy mode: BM25 → substring fallback on exception only.
            try:
                result = await self._smart_tools.smart_entity_search(
                    query,
                    limit,
                    offset=offset,
                    domain_filter=domain_filter,
                    include_hidden=include_hidden_bool,
                    prefetched_states=prefetched_states,
                    prefetched_registry=prefetched_registry,
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
                    self._client,
                    query,
                    domain_filter,
                    limit,
                    offset,
                    include_hidden=include_hidden_bool,
                    state_filter=state_filter,
                    prefetched_states=prefetched_states,
                    prefetched_registry=prefetched_registry,
                )
                warning = "Fuzzy search unavailable, using substring match"
                search_type = "exact_match"

        _normalize_regular_search_result(
            result, search_type, domain_filter, offset, limit
        )

        # Apply state_filter to fuzzy results BEFORE grouping so by_domain
        # stays consistent with results[]. For fuzzy_search, state_filter is
        # page-only — smart_entity_search already paginated internally, so
        # total_matches/has_more reflect the unfiltered dataset.
        if state_filter and "results" in result and search_type == "fuzzy_search":
            filtered = [r for r in result["results"] if r.get("state") == state_filter]
            result["results"] = filtered
            result["count"] = len(filtered)
            result["state_filter_note"] = (
                "state_filter applied to this page only; "
                "total_matches and has_more reflect the unfiltered "
                "fuzzy-search dataset and may yield empty pages"
            )

        enrich_warnings = await self._maybe_enrich_entity_records(
            result.get("results", []), parsed_result_fields
        )
        _apply_by_domain_grouping(
            result,
            result.get("results", []),
            group_by_domain_bool,
            per_domain_limit_int,
            parsed_result_fields,
        )

        if state_filter is not None:
            result["state_filter"] = state_filter

        if warning:
            result.setdefault("warnings", []).append(warning)
            result["partial"] = True

        _apply_result_fields_to_response(result, parsed_result_fields)
        merge_visibility_warnings(result, enrich_warnings)

        # No add_timezone_metadata — see _search_area_with_query.
        return result

    @tool(
        name="ha_get_overview",
        tags={"Search & Discovery"},
        annotations={
            "openWorldHint": True,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get System Overview",
        },
    )
    @log_tool_usage
    async def ha_get_overview(
        self,
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
            JSON_STRING_COERCION,
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
                    "Matches the HA Repairs UI which hides dismissed items by default. "
                    "To dismiss/ignore a repair, call ha_call_service with "
                    'ws_command="repairs/ignore_issue" and data={"domain": ..., '
                    '"issue_id": ..., "ignore": true}.'
                ),
            ),
        ] = False,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
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
                    "tool_discovery, settings_url, settings_url_hint, "
                    "read_only_mode, read_only_mode_hint, ha_mcp_update. Note: "
                    "``settings_url`` (stdio mode), ``settings_url_hint`` "
                    "(HTTP/Docker/OAuth mode), the ``read_only_mode`` / "
                    "``read_only_mode_hint`` pair (only while Read Only Mode "
                    "is on), and ``ha_mcp_update`` (when an update check applies) "
                    "are emitted regardless of ``fields=`` projection so the "
                    "settings page, the active mode, and a newer ha-mcp release "
                    "stay discoverable; see the tool description."
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

        The response also carries an ``ha_mcp_update`` object
        ``{current, latest, update_available}`` reporting whether a newer ha-mcp
        release is available (PyPI for pip/Docker, the Supervisor add-on store
        for the add-on) — proactively tell the user when ``update_available`` is
        true. Emitted regardless of ``fields=``; omitted only for the
        ``unknown`` version and when ``HA_MCP_DISABLE_UPDATE_CHECK`` is set.
        """
        # Validate fields= early so a malformed value returns VALIDATION_FAILED
        # with parameter="fields".
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

        parsed_domains = parse_string_list_param(domains, "domains", allow_csv=True)

        result = await self._collect_overview(
            _OverviewInputs(
                detail_level=detail_level,
                max_entities_per_domain=max_entities_per_domain,
                include_state=include_state_bool,
                include_entity_id=include_entity_id_bool,
                domains_filter=parsed_domains,
                limit=limit,
                offset=offset,
                include_notifications=include_notifications_bool,
                include_dismissed_repairs=include_dismissed_repairs_bool,
            )
        )

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

        # Surface the stdio settings UI sidecar URL when a URL file is present.
        # Added *after* ``project_fields`` so it survives every ``fields=`` projection
        # (issue #863).
        from ..stdio_settings_sidecar import read_sidecar_url

        projected = project_fields(result, parsed_fields)
        sidecar_url = read_sidecar_url()
        if sidecar_url:
            projected["settings_url"] = sidecar_url
        else:
            # No stdio sidecar URL file. In HTTP / Docker / OAuth modes hint at
            # the page (and startup-log URL) instead of guessing a wrong absolute URL
            # when bound to 0.0.0.0 (issue #1458).
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

        # Surface Read Only Mode after projection so the flag survives any
        # fields= filter.
        if get_global_settings().read_only_mode:
            projected["read_only_mode"] = True
            projected["read_only_mode_hint"] = (
                "Read Only Mode is ON: write-capable tools are disabled and "
                "all write or destructive operations are blocked "
                "server-side. You can search, read, and analyze freely. To "
                "allow changes, the user must turn off Read Only Mode in "
                "the ha-mcp settings UI (Tools tab) or the add-on "
                "configuration."
            )

        # Surface the MCP server's own update status after projection.
        from ..update_check import get_update_field

        mcp_update = await get_update_field()
        if mcp_update is not None:
            projected["ha_mcp_update"] = mcp_update

        return projected

    async def _fetch_system_info(
        self,
        result: dict[str, Any],
        detail_level: str,
        *,
        prefetched_config: dict[str, Any] | None = None,
    ) -> None:
        """Populate result['system_info'] from HA config; tolerates failure.

        ``prefetched_config`` (the component's ``config`` slice, already the bare
        ``get_config()`` dict) is used verbatim when given, skipping the fetch.
        """
        try:
            config = (
                prefetched_config
                if prefetched_config is not None
                else await self._client.get_config()
            )
            system_info: dict[str, Any] = {
                "base_url": self._client.base_url,
                "version": config.get("version"),
                "location_name": config.get("location_name"),
                "time_zone": config.get("time_zone"),
                "language": config.get("language"),
                "state": config.get("state"),
            }
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
            if "system_summary" in result:
                result["system_summary"]["version"] = config.get("version") or "unknown"
        except Exception as e:
            logger.warning(
                "Failed to fetch system info for overview: %s", e, exc_info=True
            )
            if "system_summary" in result:
                result["system_summary"].setdefault("version", "unknown")

    async def _fetch_notifications(
        self,
        result: dict[str, Any],
        *,
        prefetched_notifications: dict[str, Any] | None = None,
    ) -> None:
        """Attach active persistent notifications to result.

        ``prefetched_notifications`` (the component's ``notifications`` slice
        re-wrapped in the ``{success, result}`` envelope) is unwrapped by the same
        code as the live fetch when given, skipping the WS call.
        """
        result["notification_count"] = 0
        result["notifications"] = []
        try:
            ws_result = (
                prefetched_notifications
                if prefetched_notifications is not None
                else await self._client.send_websocket_message(
                    {"type": "persistent_notification/get"}
                )
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
            else:
                # HA answered and rejected. The pre-seeded zero would otherwise
                # report "none pending" for a section that never ran, the same
                # false negative the transport path already reports.
                err = ws_result.get("error")
                err_msg = (
                    err.get("message") if isinstance(err, dict) else err
                ) or "unknown error"
                result.setdefault("warnings", []).append(
                    f"notifications unavailable: {err_msg}"
                )
        except Exception as e:
            logger.warning(
                "Failed to fetch notifications for overview: %s", e, exc_info=True
            )
            # Leaving the keys off entirely reads as "no notifications", which
            # is a different answer from "could not ask" (#1947).
            result.setdefault("warnings", []).append(f"notifications unavailable: {e}")

    async def _fetch_repairs(
        self,
        result: dict[str, Any],
        include_dismissed_repairs_bool: bool,
        *,
        prefetched_repairs: dict[str, Any] | None = None,
    ) -> None:
        """Attach active repairs issues to result.

        ``prefetched_repairs`` (the component's ``repairs`` slice re-wrapped in the
        ``{success, result: {issues: [...]}}`` envelope) is unwrapped, filtered
        (``filter_active_repairs``), and projected by the same code as the live
        fetch when given, skipping the WS call.
        """
        result["repair_count"] = 0
        result["repairs"] = []
        try:
            repairs_result = (
                prefetched_repairs
                if prefetched_repairs is not None
                else await self._client.send_websocket_message(
                    {"type": "repairs/list_issues"}
                )
            )
            if repairs_result.get("success"):
                raw_issues = repairs_result.get("result", {}).get("issues", [])
                # Core's ``repairs/list_issues`` filters ``if issue.active``; the
                # component's ``overview`` repairs slice does NOT (it emits every
                # registry issue, carrying ``active`` additively). After an HA
                # restart the registry restores previously-reported issues as
                # ``active=False`` placeholders the legacy path and Repairs UI
                # never show, so drop them here for parity. Legacy rows omit
                # ``active`` (None) → no-op for them.
                all_issues = [i for i in raw_issues if i.get("active") is not False]
                visible_issues = filter_active_repairs(
                    all_issues,
                    include_dismissed=include_dismissed_repairs_bool,
                )
                result["repair_count"] = len(visible_issues)
                if not include_dismissed_repairs_bool:
                    # Baseline excludes inactive registry stubs so they are
                    # not miscounted as dismissed.
                    dismissed_count = len(
                        filter_active_repairs(all_issues, include_dismissed=True)
                    ) - len(visible_issues)
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

    async def _collect_overview(self, inputs: _OverviewInputs) -> dict[str, Any]:
        """Assemble the HA-sourced overview, preferring the in-process component.

        The component's ``ha_mcp_tools/overview`` returns the eight raw reads the
        legacy path makes today (states + services + the three registries +
        config + notifications + repairs) in one WebSocket round-trip; the server
        feeds those slices into its **unchanged** assembly
        (``get_system_overview`` + ``system_info`` / ``notifications`` /
        ``repairs``), so the two paths are byte-identical by construction. Routed
        all-or-nothing per the ``ha_search`` precedent, gated solely on the
        ``overview`` capability. Unlike ``ha_search``, an active
        entity-visibility filter does NOT force the legacy path here:
        ``get_system_overview`` calls ``load_hidden_set`` unconditionally and
        ``_build_overview_slices`` hands it the same registry + states envelope
        the legacy fetch produces, so the filter is applied server-side over the
        component's slices and the output stays byte-identical to legacy
        (``load_hidden_set`` reaches any extra data it needs — e.g. the
        Assist-exposure dimension — through the ``client`` it is already passed).
        The server-side-only fields (``tool_discovery``, ``settings_url``,
        ``read_only_mode``, ``ha_mcp_update``) and the ``fields=`` projection are
        applied by ``ha_get_overview`` after this, identically on both paths.
        """
        caps = await get_component_caps(self._client)
        if component_supports(caps, "overview"):
            component_result = await self._overview_via_component(inputs)
            if component_result is not None:
                return component_result
        return await self._assemble_overview(inputs, None)

    async def _overview_via_component(
        self, inputs: _OverviewInputs
    ) -> dict[str, Any] | None:
        """Serve the overview from the component's slices; ``None`` ⇒ run legacy.

        Error taxonomy (design § 4), mirroring ``_ha_search_via_component``:

        - ``unknown_command`` (the component was downgraded mid-session, so the
          cached positive caps are stale): invalidate the caps and return
          ``None`` so the caller falls back **silently** — an expected,
          non-actionable transition.
        - any other ``HomeAssistantCommandError`` (a component handler bug) or a
          ``HomeAssistantCommandTimeout`` (the component WS overview timed out):
          serve the correct result from the legacy path, append a ``warnings[]``
          entry, and ``log.warning`` — correct results now, breakage visible.
        - a malformed slice payload (a required slice missing/malformed, or a
          non-empty ``slice_errors`` — ``_build_overview_slices`` returns
          ``None``): treated like the command-error branch (legacy + warning +
          log), so a partial snapshot never serves a silently-degraded overview.
        - ``HomeAssistantConnectionError`` (pooled-WS drop) or the plain
          ``Exception`` ``get_websocket_client()`` raises on a failed (re)connect:
          served the same way — the legacy overview reads ``/api/states`` +
          ``/api/services`` over REST and the registries through the swallowing
          ``send_websocket_message`` bridge, so it degrades to a partial overview
          rather than dying identically on a pooled-WS drop; a transport failure
          must not escape.
        """
        try:
            raw = await self._send_component_overview(inputs)
        except (HomeAssistantCommandError, HomeAssistantCommandTimeout) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            legacy = await self._assemble_overview(inputs, None)
            legacy.setdefault("warnings", []).append(
                f"component overview path failed ({exc}); served via legacy path"
            )
            logger.warning("ha_mcp_tools/overview failed; fell back to legacy: %r", exc)
            return legacy
        except Exception as exc:
            legacy = await self._assemble_overview(inputs, None)
            legacy.setdefault("warnings", []).append(
                f"component overview connection error ({exc}); served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/overview connection error; fell back to legacy: %r", exc
            )
            return legacy
        slices = _build_overview_slices(raw.get("result") or {})
        if slices is None:
            legacy = await self._assemble_overview(inputs, None)
            legacy.setdefault("warnings", []).append(
                "component overview returned malformed slices; served via legacy path"
            )
            logger.warning(
                "ha_mcp_tools/overview returned malformed slices; fell back to legacy"
            )
            return legacy
        return await self._assemble_overview(inputs, slices)

    async def _send_component_overview(self, inputs: _OverviewInputs) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/overview`` command over the per-client WebSocket."""
        ws = await get_websocket_client(
            url=self._client.base_url, token=self._client.token
        )
        return await ws.send_command(
            "ha_mcp_tools/overview",
            **_build_component_overview_request(inputs),
        )

    async def _assemble_overview(
        self, inputs: _OverviewInputs, prefetched: _OverviewSlices | None
    ) -> dict[str, Any]:
        """Assemble the overview from slices — prefetched or legacy-fetched.

        Identical assembly either way: ``get_system_overview``'s join plus the
        wrapper's ``system_info`` / ``notifications`` / ``repairs``, with the
        entity-visibility filter applied server-side over the (prefetched or
        fetched) registry + states. ``prefetched`` carries the component's raw
        slices adapted to the shapes the assembly consumes; ``None`` runs the
        original per-read REST/WS fetches. Byte-parity between the two is by
        construction — same code, only the data source differs.
        """
        result = await self._smart_tools.get_system_overview(
            inputs.detail_level,
            inputs.max_entities_per_domain,
            inputs.include_state,
            inputs.include_entity_id,
            domains_filter=inputs.domains_filter,
            limit=inputs.limit,
            offset=inputs.offset,
            prefetched_slices=prefetched.registry_slices if prefetched else None,
        )
        result = cast(dict[str, Any], result)

        await self._fetch_system_info(
            result,
            inputs.detail_level,
            prefetched_config=prefetched.config if prefetched else None,
        )

        if inputs.include_notifications:
            await self._fetch_notifications(
                result,
                prefetched_notifications=(
                    prefetched.notifications if prefetched else None
                ),
            )

        await self._fetch_repairs(
            result,
            inputs.include_dismissed_repairs,
            prefetched_repairs=prefetched.repairs if prefetched else None,
        )
        return result

    async def _ha_deep_search(
        self,
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
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
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
            result = await self._smart_tools.deep_search(
                query,
                parsed_search_types,
                limit,
                offset,
                include_config_bool,
                exact_match=exact_match_bool,
                config_time_budget=config_time_budget,
                ctx=ctx,
                prefetched_states=prefetched_states,
                prefetched_registry=prefetched_registry,
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

    @tool(
        name="ha_get_state",
        tags={"Search & Discovery"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Entity State",
        },
    )
    @log_tool_usage
    async def ha_get_state(
        self,
        entity_id: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description="Entity ID or list of entity IDs to retrieve state for "
                "(e.g., 'light.kitchen' or ['light.kitchen', 'sensor.temperature'])"
            ),
        ],
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
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
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified keys from each entity's attributes dict "
                    '(e.g. ["brightness", "color_temp_kelvin"] for lights). '
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
        # the same string/CSV input per entity.
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
        # ignoring it.
        attribute_keys_no_effect = (
            parsed_attribute_keys is not None
            and parsed_fields is not None
            and "attributes" not in parsed_fields
        )

        if isinstance(entity_id, str):
            return await self._get_single_entity_state(
                entity_id,
                parsed_fields,
                parsed_attribute_keys,
                attribute_keys_no_effect,
            )
        return await self._get_bulk_entity_states(
            entity_id, parsed_fields, parsed_attribute_keys, attribute_keys_no_effect
        )

    async def _get_single_entity_state(
        self,
        entity_id: str,
        parsed_fields: list[str] | None,
        parsed_attribute_keys: list[str] | None,
        attribute_keys_no_effect: bool,
    ) -> dict[str, Any]:
        """Fetch and return state for a single entity ID."""
        try:
            result = await self._get_one_state(entity_id)
            entity_record, attr_warn = _project_entity(
                result, parsed_fields, parsed_attribute_keys
            )
            # Always wrap (include_metadata=True); callers and tests rely on
            # the ``result["data"]`` envelope even when fields= is active.
            wrapped = await add_timezone_metadata(self._client, entity_record)
            # ``attribute_keys`` was specified but ``attributes`` is not in the
            # projected ``fields=`` set. Attach the warning at the outer wrapper
            # level (sibling of ``data``/``metadata``) — the FIELDS PROJECTION
            # contract: ``fields=`` filters the keys of the returned record;
            # ``warnings`` is not a record key.
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
            return None  # unreachable: exception_to_structured_error always raises

    async def _get_bulk_entity_states(
        self,
        entity_ids: list[str],
        parsed_fields: list[str] | None,
        parsed_attribute_keys: list[str] | None,
        attribute_keys_no_effect: bool,
    ) -> dict[str, Any]:
        """Fetch states for multiple entity IDs in parallel."""
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
            results = await self._resolve_bulk_state_results(unique_ids)
            states, errors, attr_warns = _accumulate_state_results(
                unique_ids, results, parsed_fields, parsed_attribute_keys
            )
            response = _build_bulk_states_response(
                states, errors, attr_warns, attribute_keys_no_effect
            )
            return await add_timezone_metadata(self._client, response)

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting bulk states: {e}", exc_info=True)
            exception_to_structured_error(
                e,
                context={"entity_ids": entity_ids},
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _fetch_single_state(self, eid: str) -> dict[str, Any]:
        """Fetch state for one entity; returns structured error dict on failure."""
        try:
            state = await self._client.get_entity_state(eid)
            return {"success": True, "entity_id": eid, "state": state}
        except Exception as e:
            logger.warning(f"Failed to fetch state for '{eid}': {e}")
            # ast-grep-ignore — batch item failure, aggregated via asyncio.gather
            return exception_to_structured_error(
                e,
                context={"entity_id": eid},
                raise_error=False,
            )

    async def _get_one_state(self, entity_id: str) -> dict[str, Any]:
        """Return one entity's raw state dict — component bulk-read or legacy REST.

        The same fetch primitive the bulk path uses, so single- and bulk-mode
        ``ha_get_state`` share one code path. When the component serves it, an id
        it authoritatively reports absent raises the same 404 the legacy REST read
        would, so the caller's exception handler produces the identical
        single-entity ENTITY_NOT_FOUND (with its ``ha_search()`` suggestion).
        """
        component = await self._fetch_states_via_component([entity_id])
        if component is None:
            legacy_state: dict[str, Any] = await self._client.get_entity_state(
                entity_id
            )
            return legacy_state
        states = component.get("states") or {}
        if entity_id in states:
            record: dict[str, Any] = states[entity_id]
            return record
        raise _missing_entity_exc(entity_id)

    async def _resolve_bulk_state_results(
        self, unique_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Per-id fetch results for a bulk read — component bulk-read or legacy REST.

        Returns one entry per id in ``_fetch_single_state`` shape (a hit dict or a
        structured error). The component path resolves every id in ONE
        ``ha_mcp_tools/states`` frame instead of up to 100 REST GETs; a missing id
        is mapped to the same 404-classified error the legacy per-id path yields,
        so ``_accumulate_state_results`` and the response-level ``ha_search()``
        suggestion behave identically on both backends.
        """
        component = await self._fetch_states_via_component(unique_ids)
        if component is None:
            return list(
                await asyncio.gather(
                    *(self._fetch_single_state(eid) for eid in unique_ids)
                )
            )
        states = component.get("states") or {}
        return [self._component_state_result(eid, states) for eid in unique_ids]

    def _component_state_result(
        self, entity_id: str, states: dict[str, Any]
    ) -> dict[str, Any]:
        """One found/missing per-id result from the component's ``states`` map."""
        if entity_id in states:
            return {"success": True, "entity_id": entity_id, "state": states[entity_id]}
        # ast-grep-ignore — batch item failure, mapped to the legacy 404 shape
        return exception_to_structured_error(
            _missing_entity_exc(entity_id),
            context={"entity_id": entity_id},
            raise_error=False,
        )

    async def _fetch_states_via_component(
        self, entity_ids: list[str]
    ) -> dict[str, Any] | None:
        """One ``ha_mcp_tools/states`` bulk read; ``None`` ⇒ use the legacy REST path.

        Returns the component's ``{states, missing}`` payload (found ids mapped to
        their ``State.as_dict()`` body) or ``None`` when the component lacks the
        ``states`` capability, was downgraded (``unknown_command`` → invalidate the
        cached caps), or errored (logged). Falls back **silently** — unlike
        ``ha_search`` / ``ha_get_zone`` which append a ``warnings[]`` entry —
        because ``ha_get_state``'s single- and bulk-mode responses do not share one
        warnings channel; the ``log.warning`` preserves operator visibility and the
        legacy REST path returns the byte-identical correct data either way.
        ``ha_get_state``'s legacy path is a REST ``get_entity_state`` read on a
        SEPARATE transport, so a WS transport/connect failure
        (``HomeAssistantConnectionError``, covering both a pooled-WS drop and a
        failed connect) is caught here and falls back to REST — an install whose REST API
        still works keeps getting its state instead of a spurious connection error.
        If REST is also down, the legacy path raises the same connection error
        itself. (``ha_search`` / ``ha_get_overview`` likewise fall back on a
        transport failure — no component fetch helper propagates one.)
        """
        caps = await get_component_caps(self._client)
        if not component_supports(caps, "states"):
            return None
        try:
            raw = await self._send_component_states(entity_ids)
        except (
            HomeAssistantCommandError,
            HomeAssistantCommandTimeout,
            HomeAssistantConnectionError,
        ) as exc:
            if is_unknown_command(exc):
                invalidate_caps(self._client)
            else:
                logger.warning(
                    "ha_mcp_tools/states failed; fell back to legacy: %r", exc
                )
            return None
        except Exception as exc:
            # Anything the tuple above doesn't cover → legacy REST, so an
            # install whose REST API still works keeps answering.
            logger.warning(
                "ha_mcp_tools/states connection error; fell back to legacy: %r", exc
            )
            return None
        result = raw.get("result") or {}
        if not isinstance(result.get("states"), dict):
            return None
        return result

    async def _send_component_states(self, entity_ids: list[str]) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/states`` command over the per-client WebSocket."""
        ws = await get_websocket_client(
            url=self._client.base_url, token=self._client.token
        )
        return await ws.send_command("ha_mcp_tools/states", entity_ids=entity_ids)


def register_search_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register search and discovery tools with the MCP server."""
    smart_tools = kwargs.get("smart_tools")
    if not smart_tools:
        raise ValueError("smart_tools is required for search tools registration")
    register_tool_methods(mcp, SearchTools(client, smart_tools))
