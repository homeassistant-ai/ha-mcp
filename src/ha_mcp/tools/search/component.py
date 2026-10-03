"""Component route for ``ha_search``.

Request building, dashboard-window merging and response shaping for the
``ha_mcp_tools`` search command, plus the methods that call it.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ha_mcp._vendor.fastmcp import Context

from ...client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ...client.websocket_client import get_websocket_client
from ...visibility.model import VisibilityWire, wire_has_allowlist_dimensions
from ...visibility.resolver import (
    visibility_state_and_wire,
)
from ..component_api import (
    component_supports,
    invalidate_caps,
    is_unknown_command,
)
from ..smart_search import DEFAULT_CONCURRENCY_LIMIT, DeepSearchMixin
from ..util_helpers import merge_visibility_warnings
from .entities import (
    _ENTITY_RECORD_KEYS,
    _requested_enrichment,
    _requested_membership,
)
from .legacy import LegacySearchMixin
from .response import (
    _CONFIG_BUCKETS,
    _apply_by_domain_grouping,
    _apply_result_fields_to_response,
    _apply_search_outcome,
    _as_record_list,
    _emit_intent_skip_warning,
    _finalize_partial_state,
    _format_search_diagnostics,
    _merge_partial_reason,
    _mirror_partial_to_warnings,
    _new_search_response,
    _normalized_domain_filter,
    _parse_component_result_fields,
    _project_response_fields,
    _ResolvedSearch,
    _synthesize_combined_pagination,
)

logger = logging.getLogger(__name__)


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
    # True when Home Assistant's reference graph named this config as
    # referencing the queried entity (#2258). Independent of
    # ``match_in_config``: a config the graph confirms but whose body was
    # unreadable carries this flag alone. Always False on the component route,
    # which consults no graph because its in-process scan already reads the
    # YAML and has no fetch budget, so it has neither blind spot the graph
    # covers. Read it as "the graph named this one", never as "the graph found
    # nothing".
    out["match_in_references"] = bool(rec.get("match_in_references"))
    if include_config:
        config = rec.get("config")
        if config is None and "options" in rec:
            config = rec.get("options")
        out["config"] = config if config else None
    return out


# Body surfaces the ``ha_mcp_tools`` component's ``search`` command accepts
# (its voluptuous allowlist also has ``entity``, appended separately by
# ``_build_component_search_request``). ``dashboard`` is deliberately absent:
# the command has no dashboard scanner and its ``vol.In(ALL_SEARCH_TYPES)``
# rejects the value, which bounced every such call off the component schema
# into a warning-laden fallback (issue #2008). The value is still ROUTE-
# eligible: a request naming it keeps the fast path for the surfaces above
# while the dashboards leg fills that bucket and the server merges the two
# (issue #2289) — only the wire request drops it.
_COMPONENT_BODY_SEARCH_TYPES: frozenset[str] = frozenset(
    {"automation", "script", "scene", "helper"}
)


# The ``search_types`` value the component's ``search`` command cannot serve;
# the dashboards leg serves it instead (issue #2289).
_DASHBOARD_SEARCH_TYPE = "dashboard"


# The component's ``limits.max_results``, which its ``search`` schema enforces
# as a hard ``vol.Range(max=...)`` on ``limit``. The dashboard merge fetches a
# ``[0, offset + limit)`` window, so a deep enough page exceeds it — see
# ``_dashboard_split_serviceable``.
_COMPONENT_MAX_RESULTS = 500


# Config buckets a component ``search`` result can carry. ``dashboards`` is
# excluded: the command has no dashboard surface, so that bucket always comes
# from the dashboards leg (issue #2289).
_COMPONENT_CONFIG_BUCKETS: tuple[str, ...] = tuple(
    bucket for bucket in _CONFIG_BUCKETS if bucket != "dashboards"
)


def _component_serves_search_types(req: _ResolvedSearch) -> bool:
    """True when the component route can serve every requested surface.

    Only an explicit ``search_types`` list can name a surface the component's
    ``search`` command lacks, and only the body-eligible branch forwards it — a
    body-ineligible request sends the entity surface alone, which the component
    always accepts.

    ``dashboard`` is served ALONGSIDE the command rather than by it: the wire
    request drops the value (``_build_component_search_request``) and the
    dashboards leg fills the bucket, merged server-side (issue #2289). Any
    other unsupported surface still sends the whole request to the legacy path,
    silently — the same treatment as the other route-ineligible modes, not the
    warning-emitting failure fallback.
    """
    if not req.body_eligible or req.parsed_search_types is None:
        return True
    return all(
        t in _COMPONENT_BODY_SEARCH_TYPES or t == _DASHBOARD_SEARCH_TYPE
        for t in req.parsed_search_types
    )


def _component_body_search_types(req: _ResolvedSearch) -> list[str]:
    """The body surfaces forwarded to the component, ``dashboard`` stripped.

    The component's ``search`` schema rejects ``dashboard``, so the value never
    crosses the wire even when the caller asked for it — the dashboards leg
    serves that bucket instead (issue #2289).
    """
    parsed = req.parsed_search_types or ["automation", "script", "scene", "helper"]
    return [t for t in parsed if t != _DASHBOARD_SEARCH_TYPE]


def _build_component_search_request(
    req: _ResolvedSearch, *, dashboard_split: bool = False
) -> dict[str, Any]:
    """Translate resolved ha_search inputs into an ``ha_mcp_tools/search`` request.

    ``search_types`` on the WS command selects surfaces including the entity
    surface (``"entity"``), so branch eligibility computed server-side maps
    directly onto which surfaces the component searches. Optional string
    filters are omitted when empty to satisfy the component's ``str``-typed
    voluptuous schema.

    ``dashboard_split`` switches to the WINDOW fetch the dashboard merge needs.
    The component pages its own corpus, so the server can only page the MERGED
    list if it holds every component record that could reach the caller's page
    — that is the whole ``[0, offset + limit)`` prefix. With the default
    ``offset=0`` the window is byte-identical to the plain request.
    """
    search_types: list[str] = []
    if req.registry_eligible:
        search_types.append("entity")
    if req.body_eligible:
        search_types.extend(_component_body_search_types(req))
    request: dict[str, Any] = {
        "search_types": search_types,
        "exact": req.exact_match,
        "include_hidden": req.include_hidden,
        "include_config": req.include_config,
        "limit": (req.offset + req.limit) if dashboard_split else req.limit,
        "offset": 0 if dashboard_split else req.offset,
    }
    membership_fields = _requested_membership(
        _parse_component_result_fields(req.result_fields)
    )
    if membership_fields:
        request["result_fields"] = list(membership_fields)
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


def _dashboard_split_requested(req: _ResolvedSearch) -> bool:
    """True when this request needs the dashboards leg merged in (issue #2289)."""
    return bool(
        req.body_eligible
        and req.parsed_search_types is not None
        and _DASHBOARD_SEARCH_TYPE in req.parsed_search_types
    )


def _component_max_results(caps: Any) -> int:
    """The component's advertised ``limits.max_results``, defaulting to 500.

    The advisory ``limits`` ride the cached ``info`` probe, so a component
    advertising a smaller ceiling gates the window fetch here instead of
    accepting the split and then rejecting the frame in its schema (an
    avoidable failed round-trip whose fallback still serves the caller).
    """
    limits = getattr(caps, "limits", None)
    value = limits.get("max_results") if isinstance(limits, dict) else None
    if isinstance(value, int) and value > 0:
        return value
    return _COMPONENT_MAX_RESULTS


def _dashboard_split_serviceable(req: _ResolvedSearch, caps: Any) -> bool:
    """True when the split can serve the request; False ⇒ legacy serves it whole.

    Two corner cases route away rather than being half-served:

    - Nothing is left for the component command once ``dashboard`` is stripped.
      An explicit ``search_types`` pin also drops the entity surface, so
      ``search_types=["dashboard"]`` would send an empty request and leave the
      split as a dashboards leg wearing a component envelope — the legacy path
      already produces exactly that, with its own bucket.
    - On older components without ``search_unified``, the window fetch would
      ask for more records than the component's ``limit`` ceiling
      (``_component_max_results``), which its schema rejects outright.
    """
    serves_body = req.body_eligible and bool(_component_body_search_types(req))
    if not (req.registry_eligible or serves_body):
        return False
    return component_supports(caps, "search_unified") or (
        req.offset + req.limit <= _component_max_results(caps)
    )


@dataclass(frozen=True)
class _DashboardLeg:
    """The dashboards surface's contribution to a component-served ha_search.

    ``records`` are legacy-shaped (``url_path`` / ``title`` /
    ``score``), NOT component records — they never pass through
    ``_normalize_component_config_record``. ``failed`` is the leg's own
    "not scanned" count, reported with the deep path's wording. ``error`` is
    set only when the leg raised, in which case the bucket is empty and the
    response says so rather than reading as a clean zero.
    """

    records: list[dict[str, Any]]
    failed: int = 0
    error: str | None = None


def _apply_entity_window(req: _ResolvedSearch, windowed: dict[str, Any]) -> None:
    """Re-slice the over-fetched entity surface onto the caller's page.

    The window fetch over-fetches every surface the request names. Entity
    records merge with nothing, so their page is the same slice applied
    server-side. ``entity_total_matches`` is a corpus total and
    pagination-independent, so ``entity_has_more`` is recomputed against it
    (and pinned, so the shaping helper never falls back to the sliced length).

    No CURRENT request gets past the early return: the split needs an explicit
    ``search_types``, which pins the call config-only
    (``_compute_eligibility``), so the window carries no entity surface at all.
    This is the guard that keeps the window honest if that eligibility ever
    admits one — without it the entity page would silently be the whole
    ``[0, offset + limit)`` prefix.
    """
    if not req.registry_eligible:
        return
    entities = _as_record_list(windowed.get("entities"))
    total = int(windowed.get("entity_total_matches", len(entities)) or 0)
    page = entities[req.offset : req.offset + req.limit]
    windowed["entities"] = page
    windowed["entity_total_matches"] = total
    windowed["entity_has_more"] = (req.offset + len(page)) < total


def _merge_sort_key(record: dict[str, Any]) -> str:
    """Mirror of the component's ``_sort_key`` config tiebreak, dashboards added.

    The component cuts its window with ``(-score, _sort_key)`` where
    ``_sort_key`` is ``str(entity_id or id or name or "")``
    (``custom_components/ha_mcp_tools/websocket_api/search.py``); those fields ride
    the wire records unchanged, so the merge reproduces the exact order that
    decided window membership. Dashboard records are outside the component
    corpus, so any deterministic key places them consistently across pages —
    ``url_path`` extends the same chain.
    """
    return str(
        record.get("entity_id")
        or record.get("id")
        or record.get("name")
        or record.get("url_path")
        or ""
    )


def _merge_dashboard_window(
    req: _ResolvedSearch,
    component_result: dict[str, Any],
    dashboard_records: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Page the window fetch and the dashboards leg as ONE score-sorted list.

    The legacy contract is a global score sort across every config bucket, then
    ``[offset : offset + limit]`` (``_deep._paginate_and_build_response``), so
    the merged page has to be cut server-side. It is exact: the component
    fetched the whole ``[0, offset + limit)`` window of its own corpus, a
    superset of every component record that can reach the caller's page, and
    the leg returns its bucket unpaginated.

    The cut uses the SAME total order that selected the window —
    ``(-score, _merge_sort_key)``, mirroring the component's
    ``(-score, _sort_key)`` — because window membership is decided by the
    component's order: cutting the merge with any other order lets an
    equal-score record at a window boundary repeat on one page and vanish
    from the next as ``offset`` grows.

    Returns the component result rewritten for the caller's page — buckets
    sliced, entity records re-sliced, totals and ``*_has_more`` recomputed —
    plus the dashboards page.
    """
    tagged: list[tuple[str, dict[str, Any]]] = [
        (bucket, record)
        for bucket in _COMPONENT_CONFIG_BUCKETS
        if bucket in component_result
        for record in _as_record_list(component_result.get(bucket))
    ]
    tagged.extend(("dashboards", record) for record in dashboard_records)
    tagged.sort(
        key=lambda item: (-(item[1].get("score") or 0), _merge_sort_key(item[1]))
    )
    page = tagged[req.offset : req.offset + req.limit]

    total = int(component_result.get("config_total_matches", 0) or 0) + len(
        dashboard_records
    )
    windowed: dict[str, Any] = dict(component_result)
    for bucket in _COMPONENT_CONFIG_BUCKETS:
        if bucket in component_result:
            windowed[bucket] = [rec for name, rec in page if name == bucket]
    windowed["config_total_matches"] = total
    # The component's own flag still carries information the merged count
    # cannot: it reports a corpus extending past the window, whose records are
    # not in the merged list at all.
    windowed["config_has_more"] = (req.offset + len(page)) < total or bool(
        component_result.get("config_has_more")
    )
    _apply_entity_window(req, windowed)

    dashboards_page = [rec for name, rec in page if name == "dashboards"]
    if not req.include_config:
        # Mirrors the legacy pipeline's per-record pop, which also runs on the
        # paginated slice rather than the corpus.
        for record in dashboards_page:
            record.pop("config", None)
    return windowed, dashboards_page


def _apply_dashboard_leg_state(response: dict[str, Any], leg: _DashboardLeg) -> None:
    """Fold the dashboards leg's incompleteness into the merged response.

    ``failed`` reuses the deep path's own per-type wording verbatim, so a
    merged response and a legacy one report the same sentence for the same
    condition. A raised leg is reported like a failed legacy branch — an
    ``errors[]`` entry naming the surface plus ``partial`` — so an empty
    dashboards bucket is never mistaken for a clean zero.
    """
    if leg.failed:
        DeepSearchMixin._apply_per_type_partial_flag(
            response, dashboard_failed=leg.failed
        )
    if leg.error is not None:
        _finalize_partial_state(
            response,
            partial_local=True,
            errors_local=[{"surface": "dashboards", "error": leg.error}],
        )


def _merge_component_visibility_warnings(
    response: dict[str, Any], component_result: dict[str, Any]
) -> None:
    """Fold component visibility and location warnings into the response.

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
    elif component_visibility_warnings is not None:
        logger.warning(
            "component visibility_warnings ignored: expected a list, got %s",
            type(component_visibility_warnings).__name__,
        )

    component_warnings = component_result.get("warnings")
    if isinstance(component_warnings, list):
        merge_visibility_warnings(
            response,
            [warning for warning in component_warnings if isinstance(warning, str)],
        )


async def _scrub_component_config_buckets(
    response: dict[str, Any], client: Any
) -> None:
    """Omit component config-body records referencing a hidden entity (enforce mode).

    The component's ``search_visibility`` wire applies the hide dimensions to
    ENTITY results only; its config-body records (automations/scripts/scenes/
    helpers/dashboards) can still reference a hidden entity, and the enforcement
    middleware's outbound scan would then refuse the whole search on contact
    instead of the issue-#2015 "collection reads omit" contract. Mirror of the
    legacy path's scrub (``_deep._scrub_results_for_enforce``), applied after
    ``_shape_component_search_response``. Totals are decremented by the dropped
    count — the component's corpus-side match count cannot be recomputed
    server-side. No-op unless enforce mode is active.
    """
    from ...visibility.enforcement import active_hidden_regex, scrub_records

    regex = await active_hidden_regex(client)
    if regex is None:
        return
    dropped = 0
    for bucket in _CONFIG_BUCKETS:
        records = response.get(bucket)
        if records:
            kept = scrub_records(records, regex)
            dropped += len(records) - len(kept)
            response[bucket] = kept
    if not dropped:
        return
    if isinstance(response.get("config_total_matches"), int):
        response["config_total_matches"] = max(
            0, response["config_total_matches"] - dropped
        )
    if isinstance(response.get("count"), int):
        response["count"] = max(0, response["count"] - dropped)


def _component_config_payload(
    req: _ResolvedSearch,
    component_result: dict[str, Any],
    dashboard_leg: _DashboardLeg | None,
) -> dict[str, Any]:
    """Build the config-surface payload ``_apply_search_outcome`` consumes.

    Component records are normalized into the legacy vocabulary; the
    dashboards leg's records are already legacy-shaped, so they are attached
    as-is (issue #2289).
    """
    config_has_more = bool(component_result.get("config_has_more", False))
    payload: dict[str, Any] = {
        "total_matches": int(component_result.get("config_total_matches", 0) or 0),
        "has_more": config_has_more,
        "next_offset": (req.offset + req.limit) if config_has_more else None,
    }
    for bucket in _CONFIG_BUCKETS:
        if bucket in component_result:
            payload[bucket] = [
                _normalize_component_config_record(bucket, rec, req.include_config)
                for rec in _as_record_list(component_result.get(bucket))
            ]
    if dashboard_leg is not None:
        payload["dashboards"] = dashboard_leg.records
    return payload


def _component_entity_search_mode(req: _ResolvedSearch) -> str:
    """Retain each public listing mode while using component matching."""
    if (req.area_filter or "").strip():
        return "area_filtered_query" if req.query_text else "area_only"
    if req.query_text:
        return "exact_match" if req.exact_match else "fuzzy_search"
    return "domain_listing" if (req.domain_filter or "").strip() else "state_listing"


def _component_listing_metadata(
    req: _ResolvedSearch, payload: dict[str, Any], component_result: dict[str, Any]
) -> None:
    """Preserve public listing labels and empty-area messages without rescanning."""
    domain = _normalized_domain_filter(req.domain_filter)
    if domain:
        payload["domain_filter"] = domain
    area = (req.area_filter or "").strip()
    if area:
        payload["area_filter"] = area
        payload["area_names"] = component_result.get("area_names", [])
        if not payload["total_matches"]:
            qualifier = f"{domain} " if domain else ""
            payload["message"] = f"No {qualifier}entities found in area: {area}"
    if not req.query_text:
        match_type = "area_match" if area else payload["search_type"]
        for entity in payload["results"]:
            entity["match_type"] = match_type


def _shape_component_search_response(
    req: _ResolvedSearch,
    component_result: dict[str, Any],
    *,
    dashboard_leg: _DashboardLeg | None = None,
) -> dict[str, Any]:
    """Map an ``ha_mcp_tools/search`` result into the ha_search envelope.

    The component returns per-surface records already scored and paginated
    (``entities`` + config buckets with ``*_total_matches`` / ``*_has_more``).
    Projection (``result_fields`` on entity records, ``fields`` on the
    response), by-domain grouping, and the flat pagination / partial-mirror
    finalisation all stay server-side and reuse the same helpers the legacy
    path uses (``_apply_search_outcome`` and friends), so the shape is
    identical to the legacy response by construction.

    ``dashboard_leg`` carries the separately-served dashboards bucket for a
    ``dashboard``-including request (issue #2289); it is injected here, before
    the count / partial-mirror finalisation, so a ``fields=`` projection and
    the enforce-mode scrub both see the merged envelope.
    """
    response = _new_search_response(req.query, req.parsed_search_types)
    _emit_intent_skip_warning(response, req.body_skipped_by_intent_gate)

    if req.registry_eligible:
        parsed_result_fields = _parse_component_result_fields(req.result_fields)
        # Base records contain the six documented keys. Requested enrichment
        # fields retain the component's area/floor/labels/aliases registry join;
        # requested membership fields append its opt-in state-derived metadata.
        # With neither class requested, the default six-key shape is unchanged.
        record_keys = (
            *_ENTITY_RECORD_KEYS,
            *_requested_enrichment(parsed_result_fields),
            *_requested_membership(parsed_result_fields),
        )
        conditional_keys = set(_requested_membership(parsed_result_fields))
        entities = [
            {
                key: rec[key] if key in conditional_keys else rec.get(key)
                for key in record_keys
                if key not in conditional_keys or key in rec
            }
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
            "search_type": _component_entity_search_mode(req),
        }
        _component_listing_metadata(req, entity_payload, component_result)
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
        _apply_search_outcome(
            response,
            "configs",
            _component_config_payload(req, component_result, dashboard_leg),
        )

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

    if dashboard_leg is not None:
        _apply_dashboard_leg_state(response, dashboard_leg)

    _merge_component_visibility_warnings(response, component_result)

    response["count"] = len(response["entities"]) + sum(
        len(response.get(bucket, [])) for bucket in _CONFIG_BUCKETS
    )
    _synthesize_combined_pagination(response)
    _mirror_partial_to_warnings(response)
    return _project_response_fields(response, req.parsed_fields)


class ComponentSearchMixin(LegacySearchMixin):
    """Routes ``ha_search`` through the component search command."""

    async def _resolve_component_search_visibility(
        self, caps: Any
    ) -> tuple[bool, VisibilityWire | None]:
        """Decide the ha_search route under the entity-visibility gate.

        Returns ``(route_component, visibility_param)`` for a caller that has
        already confirmed the component advertises ``search``:

        - filter inactive → ``(True, None)``: the plain component search, no
          ``visibility`` param (parity with a pre-``search_visibility`` component).
        - filter active + ``search_visibility`` + config serialized:
          - component also advertises ``search_visibility_allowlist_authorization``
            → ``(True, <wire dict + allowlist_authorization: True>)``. The key opts
            the component into the revised rule (an allow match authorizes past
            category, HA-hidden, and Assist filters). It is never sent to a
            component lacking the capability: that component's strict schema would
            reject it, and the key's absence is what keeps a newer component on the
            legacy precedence the released server still applies in its own
            outbound scan.
          - no allowlist dimensions → ``(True, <wire dict>)``: the nine-key wire;
            with no allow dimensions the two precedences agree.
          - allowlist active without the capability → ``(False, None)``.
        - config could not be loaded → ``(False, None)``: the legacy path applies
          the filter server-side before the counts/pagination (fail-closed to
          legacy, matching ``visibility_state_and_wire``'s fail-closed pairing).

        ``visibility_state_and_wire`` loads the config once for both the active
        gate and its wire form, instead of the active check and the wire fetch
        each hitting the (memoized) config read separately.
        """
        active, visibility = await visibility_state_and_wire()
        if not active:
            return True, None
        if not component_supports(caps, "search_visibility") or visibility is None:
            return False, None
        if component_supports(caps, "search_visibility_allowlist_authorization"):
            authorized: VisibilityWire = {**visibility, "allowlist_authorization": True}
            return True, authorized
        if wire_has_allowlist_dimensions(visibility):
            return False, None
        return True, visibility

    async def _ha_search_via_component(
        self,
        req: _ResolvedSearch,
        ctx: Context | None,
        *,
        visibility: VisibilityWire | None = None,
        caps: Any = None,
    ) -> dict[str, Any] | None:
        """Serve ha_search from the component; ``None`` ⇒ run the legacy path.

        ``visibility`` is the serialized hide config passed to a
        ``search_visibility``-capable component so it applies the entity-
        visibility filter in-process (``None`` ⇒ no filter / not supported ⇒ the
        component surfaces every match, correct only because the caller routes
        here solely when the filter is inactive or the component can apply it).

        A ``search_types`` naming ``dashboard`` splits the work — the command
        serves the surfaces it has, the dashboards leg serves that bucket, and
        the two are merged (issue #2289). ``_dashboard_split_serviceable``
        names the corner cases that send the whole call to legacy instead;
        ``caps`` (the routing block's cached probe) feeds its
        component-advertised ``limits.max_results`` ceiling.
        """
        if _dashboard_split_requested(req):
            if not _dashboard_split_serviceable(req, caps):
                return None
            return await self._component_search_with_dashboards(
                req, ctx, visibility=visibility
            )
        try:
            raw = await self._send_component_search(req, visibility)
        except Exception as exc:  # noqa: BLE001
            return await self._component_search_fallback(req, ctx, exc)
        response = _shape_component_search_response(req, raw.get("result") or {})
        await _scrub_component_config_buckets(response, self._client)
        return response

    async def _component_search_fallback(
        self, req: _ResolvedSearch, ctx: Context | None, exc: Exception
    ) -> dict[str, Any] | None:
        """Apply the component-search failure taxonomy (design § 4).

        Returns the legacy-served response, or ``None`` when the caller should
        run the legacy path itself with no diagnostic:

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
        if isinstance(exc, (HomeAssistantCommandError, HomeAssistantCommandTimeout)):
            if is_unknown_command(exc):
                invalidate_caps(self._client)
                return None
            warning = f"component search path failed ({exc}); served via legacy path"
            log_message = "ha_mcp_tools/search failed; fell back to legacy: %r"
        else:
            warning = (
                f"component search connection error ({exc}); served via legacy path"
            )
            log_message = (
                "ha_mcp_tools/search connection error; fell back to legacy: %r"
            )
        legacy = await self._legacy_ha_search(req, ctx)
        legacy.setdefault("warnings", []).append(warning)
        logger.warning(log_message, exc)
        return legacy

    async def _component_search_with_dashboards(
        self,
        req: _ResolvedSearch,
        ctx: Context | None,
        *,
        visibility: VisibilityWire | None,
    ) -> dict[str, Any] | None:
        """Serve a ``dashboard``-including request from both legs (issue #2289).

        The two legs run concurrently: the dashboards leg is a second frame on
        the same pooled connection (or, on a component without
        ``dashboards_doc_search``, the legacy per-dashboard walk), so
        serialising them would add its latency to every mixed search.

        A failing component leg takes the taxonomy's legacy fallback, which
        serves the WHOLE call — dashboards bucket included — so the dashboards
        leg is CANCELLED on that failure rather than awaited: the legacy
        response carries its own dashboards bucket, and on the slow leg shapes
        (an older component's per-dashboard walk, fuzzy, ``include_config``)
        waiting would delay the fallback and then re-walk the same dashboards
        inside it.
        """
        component_task = asyncio.ensure_future(
            self._send_component_search(req, visibility, dashboard_split=True)
        )
        dashboard_task = asyncio.ensure_future(self._search_dashboards_leg(req))
        try:
            raw = await component_task
        except Exception as exc:  # noqa: BLE001
            # Settle the leg via ``asyncio.wait``, which never raises the
            # leg's own CancelledError — while a cancellation of THIS
            # coroutine delivered at that await still propagates instead of
            # being consumed right before the fallback runs the whole legacy
            # search uncancellably.
            dashboard_task.cancel()
            await asyncio.wait([dashboard_task])
            return await self._component_search_fallback(req, ctx, exc)
        except BaseException:
            # Cancellation / shutdown of THIS coroutine: settle the leg before
            # propagating, so no cancelled task outlives the call, then
            # re-raise like ``_legacy_ha_search``. A second cancellation
            # delivered during the wait propagates on its own, which is the
            # same outcome.
            dashboard_task.cancel()
            await asyncio.wait([dashboard_task])
            raise
        try:
            dashboard_outcome = await dashboard_task
        except BaseException:
            # Same invariant as above for the second await: a cancellation
            # delivered here would otherwise leave the leg task running
            # detached after this call unwinds. Settle it, then propagate.
            dashboard_task.cancel()
            await asyncio.wait([dashboard_task])
            raise

        windowed, dashboards_page = _merge_dashboard_window(
            req, raw.get("result") or {}, dashboard_outcome.records
        )
        response = _shape_component_search_response(
            req,
            windowed,
            dashboard_leg=_DashboardLeg(
                records=dashboards_page,
                failed=dashboard_outcome.failed,
                error=dashboard_outcome.error,
            ),
        )
        await _scrub_component_config_buckets(response, self._client)
        return response

    async def _search_dashboards_leg(self, req: _ResolvedSearch) -> _DashboardLeg:
        """The dashboards bucket for a merged component search.

        Reuses the deep path's own dashboards surface, which is already
        component-first (the ``dashboards_doc_search`` frame) with the legacy
        per-dashboard walk as its fallback — so the merged route covers exactly
        the dashboards the legacy route covers.

        An ordinary failure is reported through ``_DashboardLeg.error`` instead
        of raising: the other surfaces succeeded, so the call returns them and
        says the dashboard bucket is incomplete. Cancellation still propagates.
        """
        semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY_LIMIT)
        try:
            records, failed = await self._smart_tools._search_dashboards_surface(
                req.query_text.lower(),
                req.exact_match,
                semaphore,
                include_config=req.include_config,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ha_search dashboards leg failed: %r", exc)
            # ``str(asyncio.TimeoutError())`` is "" — fall back to the type name
            # so the errors[] entry never reads "dashboards: ".
            return _DashboardLeg(records=[], error=str(exc) or type(exc).__name__)
        return _DashboardLeg(records=list(records), failed=failed)

    async def _send_component_search(
        self,
        req: _ResolvedSearch,
        visibility: VisibilityWire | None = None,
        *,
        dashboard_split: bool = False,
    ) -> dict[str, Any]:
        """Send one ``ha_mcp_tools/search`` command over the per-client WebSocket.

        ``visibility`` (the serialized hide config) is attached only when set, so
        a plain-``search`` component (which lacks the param in its schema) never
        receives it. ``dashboard_split`` asks for the window fetch the dashboard
        merge pages server-side.
        """
        ws = await get_websocket_client(
            url=self._client.base_url,
            token=self._client.token,
            verify_ssl=getattr(self._client, "verify_ssl", None),
        )
        request = _build_component_search_request(req, dashboard_split=dashboard_split)
        if visibility is not None:
            request["visibility"] = visibility
        return await ws.send_command("ha_mcp_tools/search", **request)
