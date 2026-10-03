"""Entity search helpers for ``ha_search``.

Result-field validation, membership and registry enrichment, the exact-match
entity search, and the enrichment methods of the search tool class.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from ...errors import create_validation_error
from ...utils.device_registry_semantics import (
    build_device_registry_snapshot,
    effective_entity_area_id,
)
from ...utils.entity_membership import normalize_member_entity_ids
from ...utils.fuzzy_search import apply_hidden_penalty
from ...visibility.resolver import (
    device_registry_needed_for_visibility,
    load_hidden_set,
)
from ..helpers import (
    raise_tool_error,
)
from ..util_helpers import (
    merge_visibility_warnings,
    parse_string_list_param,
)
from .base import SearchToolsBase
from .response import (
    _build_hidden_ids,
    _build_pagination_metadata,
    _effective_result_fields,
    _project_records,
)

logger = logging.getLogger(__name__)


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


_MEMBERSHIP_FIELDS: tuple[str, ...] = ("is_group", "member_entity_ids")


# Every field name result_fields= accepts — base record keys plus opt-in
# enrichment and membership keys. Unknown names are rejected up front rather
# than silently projecting to empty records.
_ALLOWED_RESULT_FIELDS: frozenset[str] = (
    frozenset(_ENTITY_RECORD_KEYS)
    | frozenset(_ENRICHMENT_FIELDS)
    | frozenset(_MEMBERSHIP_FIELDS)
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


def _requested_membership(parsed_result_fields: list[str] | None) -> tuple[str, ...]:
    """Return requested membership fields, retaining the group discriminator.

    A member-only projection still includes is_group so a redacted group,
    an empty group, and a leaf entity remain distinguishable.
    """
    if not parsed_result_fields:
        return ()
    requested = set(parsed_result_fields)
    if "member_entity_ids" in requested:
        requested.add("is_group")
    return tuple(field for field in _MEMBERSHIP_FIELDS if field in requested)


def _add_membership_fields(
    record: dict[str, Any],
    attributes: Any,
    requested: tuple[str, ...],
    *,
    denied_member_ids: set[str],
) -> None:
    """Add requested generic membership metadata to one search record.

    Smart-search paths may mark attributes in _redact_hidden_memberships before
    this final projection. Callers that consume that sentinel pass an explicit
    empty denied set.
    """
    if not requested:
        return
    members = normalize_member_entity_ids(attributes)
    redacted = bool(
        isinstance(attributes, Mapping)
        and attributes.get("_ha_mcp_membership_redacted")
    ) or bool(
        members is not None
        and denied_member_ids
        and denied_member_ids.intersection(members)
    )
    if "is_group" in requested:
        record["is_group"] = members is not None
    if "member_entity_ids" in requested and members is not None and not redacted:
        record["member_entity_ids"] = members


def _ws_result_map(resp: Any) -> dict[str, dict[str, Any]]:
    """The ``{entity_id: entry}`` map from a ``config/entity_registry/get_entries`` reply."""
    if isinstance(resp, dict) and resp.get("success"):
        result = resp.get("result")
        if isinstance(result, dict):
            return {k: v for k, v in result.items() if isinstance(v, dict)}
    return {}


def _ws_registry_rows(resp: Any) -> list[Any]:
    """Return rows from a successful registry-list response, else an empty list."""
    if isinstance(resp, dict) and resp.get("success"):
        result = resp.get("result")
        if isinstance(result, list):
            return result
    return []


def _ws_registry_index(resp: Any, key: str) -> dict[str, dict[str, Any]]:
    """Index a ``config/*_registry/list`` reply by its id field (area_id/floor_id/…).

    A failed / malformed reply (the ``return_exceptions=True`` gather may hand back
    an exception) yields an empty index so the enrichment degrades that field to
    empty rather than raising.
    """
    out: dict[str, dict[str, Any]] = {}
    for item in _ws_registry_rows(resp):
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
    device_areas: dict[str, str | None],
    requested: tuple[str, ...],
) -> dict[str, Any]:
    """Compute the requested enrichment fields for one entity from registry data.

    Mirrors the component's ``_registry_enrichment`` so the legacy and
    component-served ``result_fields`` values agree: device-inherited area/labels
    (the entity's own value wins, else the device's direct-or-parent effective
    area), area→floor resolution, and
    label id→name (falling back to the id when a label has no name). ``aliases``
    pass through from the registry entry. Only the requested keys are returned.

    String aliases only: HA core's aliases can carry the COMPUTED_NAME sentinel,
    which serializes as ``null`` over the WS registry read; a blind ``str()`` would
    publish it as the literal alias ``"None"``. The component's join filters the
    same way, so dropping non-strings keeps the two paths byte-identical (the name
    the sentinel stands for is already matched via the friendly name).
    """
    aliases = sorted(a for a in (entry.get("aliases") or []) if isinstance(a, str))
    area_id = effective_entity_area_id(entry, device_areas)
    label_ids = set(entry.get("labels") or [])
    device_id = entry.get("device_id")
    device = devices.get(device_id) if device_id else None
    if device:
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
        items = _project_records(items, _effective_result_fields(parsed_result_fields))
    return {domain: items}


def _normalize_state_filter(state_filter: str | None) -> str | None:
    """Strip whitespace and lowercase a state_filter; collapse empty strings to None."""
    if state_filter is not None:
        state_filter = state_filter.strip().lower()
        if not state_filter:
            state_filter = None
    return state_filter


def _state_matches(record: dict[str, Any], state_filter: str) -> bool:
    """True when ``record``'s state equals ``state_filter``, case-insensitively.

    ``state_filter`` is already lowercased by ``_normalize_state_filter``; the
    record side is lowered here so an uppercase entity state (e.g. an
    input_select holding "Vacation") still matches the documented
    case-insensitive contract.
    """
    return (record.get("state") or "").lower() == state_filter


def _validate_entity_search_params(
    query: str | None,
    domain_filter: str | None,
    area_filter: str | None,
    result_fields: Any,
    state_filter: str | None = None,
) -> tuple[str, str | None, str | None, list[str] | None]:
    """Validate and normalise inputs for entity search; returns (query, domain_filter, area_filter, parsed_result_fields).

    ``state_filter`` is not returned (the caller normalises it separately via
    ``_normalize_state_filter``); it participates here only in the
    at-least-one-criterion check so a ``state_filter``-only call is accepted and
    enumerates every entity in that state (issue #2002)."""
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
    if (
        not query.strip()
        and not domain_filter
        and not area_filter
        and not _normalize_state_filter(state_filter)
    ):
        raise_tool_error(
            create_validation_error(
                "At least one of 'query', 'domain_filter', 'area_filter', or "
                "'state_filter' must be set.",
                parameter="query",
            )
        )
    return query, domain_filter, area_filter, parsed_result_fields


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
    *,
    membership_fields: tuple[str, ...] = (),
    denied_member_ids: set[str],
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
    record = {
        "entity_id": entity_id,
        "friendly_name": friendly_name,
        "domain": domain,
        "state": entity.get("state", "unknown"),
        "score": score,
        "match_type": "exact_match",
    }
    _add_membership_fields(
        record,
        attributes,
        membership_fields,
        denied_member_ids=denied_member_ids,
    )
    return record


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
    membership_fields: tuple[str, ...] = (),
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
    denied_member_ids = visibility_hidden | (
        hidden_ids if not include_hidden else set()
    )

    results = []
    for entity in all_entities:
        match = _match_exact_search_entity(
            entity,
            query_lower,
            domain_filter,
            visibility_hidden,
            hidden_ids,
            include_hidden,
            membership_fields=membership_fields,
            denied_member_ids=denied_member_ids,
        )
        if match is not None:
            results.append(match)

    if state_filter:
        results = [r for r in results if _state_matches(r, state_filter)]

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


class EntityEnrichmentMixin(SearchToolsBase):
    """Registry enrichment for entity search results."""

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
        device_rows = _ws_registry_rows(names[3]) if need_names else []
        device_snapshot = build_device_registry_snapshot(device_rows)
        devices = device_snapshot.by_id
        device_areas = device_snapshot.effective_area_by_id
        enrichment = {
            eid: _entity_enrichment_fields(
                entries.get(eid) or {},
                areas,
                floors,
                labels,
                devices,
                device_areas,
                requested,
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
