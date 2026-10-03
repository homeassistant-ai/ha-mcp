"""Response shaping for ``ha_search``.

Constants, validation, projection, merge and pagination helpers shared by
the component and legacy search routes.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ...errors import create_validation_error
from ..helpers import (
    raise_tool_error,
)
from ..util_helpers import (
    build_pagination_metadata,
    parse_string_list_param,
    project_records,
    result_fields_warning,
)

logger = logging.getLogger(__name__)


# Configuration-body buckets the merged ``ha_search`` orchestrator collects
# from ``_ha_deep_search``. ``dashboards`` is opt-in (excluded from the default
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
    here so ``ha_search`` and ``_ha_deep_search`` share the contract — adding
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
        # "area_only", "area_filtered_query", "domain_listing",
        # "state_listing"). E2E tests
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
    "use the corresponding get/list tool or repeat the query without "
    "entity filters."
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
        _merge_partial_reason(
            response,
            "; ".join(
                f"{error['surface']}: {error['error']}" for error in errors_local
            ),
        )


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
      ``query`` / ``domain_filter`` / ``area_filter`` / ``state_filter`` is set,
      except when the caller pinned config-only via an explicit ``search_types``.
      ``state_filter`` alone is sufficient — it enumerates every entity in that
      state (issue #2002), mirroring the ``domain_filter``-only listing mode.
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
    any_registry_input = bool(
        query_text or domain_filter_text or area_filter_text or state_filter_text
    )
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
            d: _project_records(ents, _effective_result_fields(parsed_result_fields))
            for d, ents in by_domain.items()
        }
    data["by_domain"] = by_domain


def _effective_result_fields(parsed_result_fields: list[str]) -> list[str]:
    """Retain is_group whenever member_entity_ids is projected."""
    if (
        "member_entity_ids" in parsed_result_fields
        and "is_group" not in parsed_result_fields
    ):
        return [*parsed_result_fields, "is_group"]
    return parsed_result_fields


def _apply_result_fields_to_response(
    data: dict[str, Any],
    parsed_result_fields: list[str] | None,
) -> None:
    """Project data['results'] to parsed_result_fields and attach any warning."""
    if parsed_result_fields is None or "results" not in data:
        return
    orig = data["results"]
    data["results"] = _project_records(
        orig, _effective_result_fields(parsed_result_fields)
    )
    warning_fields = [
        field for field in parsed_result_fields if field != "member_entity_ids"
    ]
    _warn = (
        _result_fields_warning(orig, data["results"], warning_fields)
        if warning_fields
        else None
    )
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
        "warnings": (
            [
                "entity search skipped: explicit search_types selects "
                "configuration-only search; omit search_types to search entities."
            ]
            if parsed_search_types is not None
            else []
        ),
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
