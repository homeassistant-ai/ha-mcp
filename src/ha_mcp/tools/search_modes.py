"""Entity search modes for the legacy ``ha_search`` route.

Area, domain, state and regular entity searches.
"""

import asyncio
import logging
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..utils.fuzzy_search import apply_hidden_penalty
from ..visibility.resolver import (
    device_registry_needed_for_visibility,
    load_hidden_set,
)
from .search_entities import (
    EntityEnrichmentMixin,
    _add_membership_fields,
    _build_domain_only_by_domain,
    _exact_match_search,
    _normalize_regular_search_result,
    _requested_membership,
    _state_matches,
)
from .search_response import (
    _apply_by_domain_grouping,
    _apply_result_fields_to_response,
    _build_hidden_ids,
    _build_pagination_metadata,
)
from .util_helpers import (
    merge_visibility_warnings,
    public_fields,
)

logger = logging.getLogger(__name__)


class EntityModesMixin(EntityEnrichmentMixin):
    """The per-mode entity searches behind ``_ha_search_entities``."""

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
                "attributes": entity.get("_attributes", {}),
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

        membership_fields = _requested_membership(parsed_result_fields)
        attributes_by_id = {
            entity.get("entity_id"): entity.get("_attributes")
            for entity in all_area_entities
        }
        for record in results:
            # smart_entity_search already applied visibility redaction and
            # preserved its sentinel on the private attributes payload.
            _add_membership_fields(
                record,
                attributes_by_id.get(record["entity_id"]),
                membership_fields,
                denied_member_ids=set(),
            )

        if state_filter:
            results = [r for r in results if _state_matches(r, state_filter)]

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
                        "attributes": entity.get("_attributes"),
                    }
                    for entity in entities
                )

        membership_fields = _requested_membership(parsed_result_fields)
        for record in all_results:
            # search_entities_by_area already applied visibility redaction and
            # preserved its sentinel on the private attributes payload.
            _add_membership_fields(
                record,
                record.pop("attributes", None),
                membership_fields,
                denied_member_ids=set(),
            )
        all_results.sort(key=lambda x: (-x["score"], x["entity_id"]))
        if state_filter:
            all_results = [r for r in all_results if _state_matches(r, state_filter)]
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

    async def _fetch_listing_snapshot(
        self,
    ) -> tuple[list[dict[str, Any]], set[str], set[str], list[str]]:
        """Fetch states + registries and resolve hidden/visibility sets for listing modes.

        Shared by ``_search_domain_only`` and ``_search_state_only``: both
        enumerate the full state machine and need the same ``/api/states``
        snapshot, registry-derived hidden ids, and visibility-excluded set.
        Returns ``(states, hidden_ids, visibility_hidden, visibility_warnings)``.

        Fetches states + the entity registry in parallel. Registry-list failure is
        tolerated (we just lose the hidden filter); states-fetch failure is fatal —
        auth/connection errors must propagate. The device registry is gated: it
        only feeds the visibility area/label dimensions, so a default/area-free
        config skips the fetch entirely.
        """
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
        return states_result, hidden_ids, visibility_hidden, visibility_warnings

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
        (
            states_result,
            hidden_ids,
            visibility_hidden,
            visibility_warnings,
        ) = await self._fetch_listing_snapshot()

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
        membership_fields = _requested_membership(parsed_result_fields)
        denied_member_ids = visibility_hidden | (
            hidden_ids if not include_hidden_bool else set()
        )

        # Score: 100 baseline for domain membership (exact, not fuzzy);
        # penalised for hidden entries so they sort below visible peers.
        scored_entities = []
        for entity in filtered_entities:
            entity_id = entity.get("entity_id", "")
            attributes = entity.get("attributes", {})
            score = apply_hidden_penalty(
                100, "_hidden" if entity_id in hidden_ids else None
            )
            record = {
                "entity_id": entity_id,
                "friendly_name": attributes.get("friendly_name", entity_id),
                "domain": domain_filter,
                "state": entity.get("state", "unknown"),
                "score": score,
                "match_type": "domain_listing",
            }
            _add_membership_fields(
                record,
                attributes,
                membership_fields,
                denied_member_ids=denied_member_ids,
            )
            scored_entities.append(record)
        scored_entities.sort(key=lambda x: (-x["score"], x["entity_id"]))
        if state_filter:
            scored_entities = [
                e for e in scored_entities if _state_matches(e, state_filter)
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

    async def _search_state_only(
        self,
        state_filter: str,
        limit: int,
        offset: int,
        include_hidden_bool: bool,
        group_by_domain_bool: bool,
        per_domain_limit_int: int | None,
        parsed_result_fields: list[str] | None,
    ) -> dict[str, Any]:
        """List all entities in a given state (state_filter with no query/domain/area).

        Mirrors ``_search_domain_only`` but enumerates across every domain,
        filtering on exact state equality (``state_filter`` is already lowercased
        by ``_normalize_state_filter``) so a single call answers "all unavailable
        entities" (issue #2002). The state filter is applied before pagination so
        ``total_matches`` reflects the filtered count.
        """
        (
            states_result,
            hidden_ids,
            visibility_hidden,
            visibility_warnings,
        ) = await self._fetch_listing_snapshot()

        # Exact state match — identical semantics to the exact/domain paths.
        # Hidden entities are kept by default (score-penalised below);
        # ``include_hidden=False`` filters them out. The visibility exclude is
        # applied before pagination so the counts below stay coherent.
        filtered_entities = [
            e
            for e in states_result
            if (eid := e.get("entity_id", "")) not in visibility_hidden
            and _state_matches(e, state_filter)
            and (include_hidden_bool or eid not in hidden_ids)
        ]
        membership_fields = _requested_membership(parsed_result_fields)
        denied_member_ids = visibility_hidden | (
            hidden_ids if not include_hidden_bool else set()
        )

        # Score: 100 baseline for state membership (exact, not fuzzy); penalised
        # for hidden entries so they sort below visible peers. ``domain`` is
        # derived per record since results span every domain.
        scored_entities = []
        for entity in filtered_entities:
            entity_id = entity.get("entity_id", "")
            attributes = entity.get("attributes", {})
            score = apply_hidden_penalty(
                100, "_hidden" if entity_id in hidden_ids else None
            )
            record = {
                "entity_id": entity_id,
                "friendly_name": attributes.get("friendly_name", entity_id),
                "domain": entity_id.split(".")[0],
                "state": entity.get("state", "unknown"),
                "score": score,
                "match_type": "state_listing",
            }
            _add_membership_fields(
                record,
                attributes,
                membership_fields,
                denied_member_ids=denied_member_ids,
            )
            scored_entities.append(record)
        scored_entities.sort(key=lambda x: (-x["score"], x["entity_id"]))
        results = scored_entities[offset : offset + limit]

        state_list_data: dict[str, Any] = {
            "success": True,
            "query": None,
            "state_filter": state_filter,
            **_build_pagination_metadata(len(scored_entities), offset, limit, results),
            "results": results,
            "search_type": "state_listing",
            "note": (
                f"Listing all entities in state '{state_filter}' "
                "(state_filter with no query/domain/area)"
            ),
        }

        enrich_warnings = await self._maybe_enrich_entity_records(
            results, parsed_result_fields
        )
        _apply_result_fields_to_response(state_list_data, parsed_result_fields)
        _apply_by_domain_grouping(
            state_list_data,
            results,
            group_by_domain_bool,
            per_domain_limit_int,
            parsed_result_fields,
        )

        # No add_timezone_metadata — see _search_area_with_query.
        return merge_visibility_warnings(
            state_list_data, [*visibility_warnings, *enrich_warnings]
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
                membership_fields=_requested_membership(parsed_result_fields),
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
                    include_attributes=bool(
                        _requested_membership(parsed_result_fields)
                    ),
                    **(
                        {"include_membership": True}
                        if _requested_membership(parsed_result_fields)
                        else {}
                    ),
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
            except Exception as fuzzy_error:  # noqa: BLE001
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
                    membership_fields=_requested_membership(parsed_result_fields),
                )
                warning = "Fuzzy search unavailable, using substring match"
                search_type = "exact_match"

        _normalize_regular_search_result(
            result, search_type, domain_filter, offset, limit
        )
        if search_type == "fuzzy_search":
            membership_fields = _requested_membership(parsed_result_fields)
            for record in result.get("results", []):
                # smart_entity_search already applied visibility redaction and
                # preserved its sentinel on the private attributes payload.
                _add_membership_fields(
                    record,
                    record.pop("attributes", None),
                    membership_fields,
                    denied_member_ids=set(),
                )

        # Apply state_filter to fuzzy results BEFORE grouping so by_domain
        # stays consistent with results[]. For fuzzy_search, state_filter is
        # page-only — smart_entity_search already paginated internally, so
        # total_matches/has_more reflect the unfiltered dataset.
        if state_filter and "results" in result and search_type == "fuzzy_search":
            filtered = [r for r in result["results"] if _state_matches(r, state_filter)]
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
