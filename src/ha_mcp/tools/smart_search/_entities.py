"""Entity fuzzy search and area-grouped entity listing."""

import asyncio
import logging
from typing import Any

from ...utils.fuzzy_search import calculate_partial_ratio
from ...visibility.resolver import (
    device_registry_needed_for_visibility,
    load_hidden_set,
)
from ..helpers import exception_to_structured_error
from ..util_helpers import merge_visibility_warnings
from ._base import _SearchBase

logger = logging.getLogger(__name__)

# Bounds the per-frame response size of config/entity_registry/get_entries
# (extended entries, ~1KB typical each) so alias enrichment can't produce an
# over-cap WebSocket frame on large instances (#1721).
_GET_ENTRIES_CHUNK_SIZE = 500


class EntitySearchMixin(_SearchBase):
    """``smart_entity_search`` and ``get_entities_by_area`` plus helpers."""

    async def smart_entity_search(
        self,
        query: str,
        limit: int = 10,
        offset: int = 0,
        include_attributes: bool = False,
        domain_filter: str | None = None,
        include_hidden: bool = True,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
    ) -> dict[str, Any]:
        """
        Search entities with fuzzy matching and typo tolerance.

        Args:
            query: Search query (can be partial, with typos)
            limit: Maximum number of results
            offset: Number of results to skip for pagination
            include_attributes: Whether to include full entity attributes
            domain_filter: Optional domain to filter entities before search (e.g., "light", "sensor")
            include_hidden: When True (default), entities with ``hidden_by``
                set in the entity registry are still returned but receive
                a score penalty so they sort below comparable visible
                matches. Pass False to filter them out entirely.
            prefetched_states: Pre-fetched ``get_states()`` list shared by the
                ha_search orchestrator when both search branches run; ``None``
                means fetch here.
            prefetched_registry: Pre-fetched ``config/entity_registry/list``
                response shared the same way; ``None`` means fetch here.

        Returns:
            Dictionary with search results and metadata
        """
        try:
            # HA domains are canonically lowercase and unpadded; defend the
            # service layer so internal callers get the same normalization the
            # tool layer applies (strip + lowercase before the prefix match).
            if domain_filter:
                domain_filter = domain_filter.strip().lower()

            entities, visibility_warnings = await self._fetch_search_entities(
                domain_filter,
                include_hidden,
                prefetched_states=prefetched_states,
                prefetched_registry=prefetched_registry,
            )

            # Perform fuzzy search - returns (paginated_results, total_count)
            matches, total_matches = self.fuzzy_searcher.search_entities(
                entities, query, limit, offset
            )
            results = self._format_entity_matches(matches, include_attributes)

            has_more = (offset + len(results)) < total_matches
            response: dict[str, Any] = {
                "success": True,
                "query": query,
                "total_matches": total_matches,
                "offset": offset,
                "limit": limit,
                "count": len(results),
                "has_more": has_more,
                "next_offset": offset + limit if has_more else None,
                "matches": results,
            }

            if not matches or matches[0]["score"] < 80:
                response["suggestions"] = self.fuzzy_searcher.get_smart_suggestions(
                    entities, query
                )

            return merge_visibility_warnings(response, visibility_warnings)

        except Exception as e:
            logger.error(f"Error in smart_entity_search: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify entity exists with get_all_states",
                    "Try simpler search terms",
                ],
                context={
                    "query": query,
                    "matches": [],
                    "error_source": "smart_entity_search",
                },
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise

    @staticmethod
    def _build_registry_slim(reg_result: Any) -> dict[str, dict[str, Any]]:
        """Map entity_id -> slim entity-registry entry (for hidden_by lookup).

        Registry-list failure is tolerated: search continues without alias /
        hidden awareness rather than failing the whole call.
        """
        registry_slim: dict[str, dict[str, Any]] = {}
        if isinstance(reg_result, dict) and reg_result.get("success"):
            for entry in reg_result.get("result", []):
                eid = entry.get("entity_id")
                if eid:
                    registry_slim[eid] = entry
        return registry_slim

    @staticmethod
    def _filter_hidden_entities(
        entities: list[dict[str, Any]],
        registry_slim: dict[str, dict[str, Any]],
        include_hidden: bool,
        visibility_hidden: set[str],
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Drop UI-hidden entities (unless include_hidden); collect survivor ids.

        Pre-filtering shrinks the get_entries payload on installations with
        thousands of entities. Returns (survivor_ids, survivor_states).
        """
        survivor_ids: list[str] = []
        survivor_states: list[dict[str, Any]] = []
        for entity in entities:
            eid = entity.get("entity_id", "")
            if not eid:
                continue
            if eid in visibility_hidden:
                continue
            slim = registry_slim.get(eid, {})
            hidden_by = slim.get("hidden_by")
            if hidden_by is not None and not include_hidden:
                continue
            survivor_ids.append(eid)
            survivor_states.append(entity)
        return survivor_ids, survivor_states

    async def _fetch_entity_aliases(
        self, survivor_ids: list[str]
    ) -> tuple[dict[str, list[str]], list[str]]:
        """Batch-fetch full registry entries for aliases.

        ``config/entity_registry/list`` deliberately omits ``aliases``;
        ``get_entries`` includes them. Survivors are split into
        ``_GET_ENTRIES_CHUNK_SIZE``-sized chunks fetched concurrently: a
        bounded number of round-trips (one per chunk), not N+1 fan-out, and
        each response frame stays bounded regardless of instance size.
        Enrichment is best-effort per chunk — one chunk failing does not
        drop aliases fetched by the others; failures are reported in the
        returned warnings so the caller can tell "no aliases exist" apart
        from "the alias fetch failed".

        Returns:
            ``(aliases_map, warnings)`` — warnings has one entry when any
            chunk failed, empty otherwise.
        """
        aliases_map: dict[str, list[str]] = {}
        if not survivor_ids:
            return aliases_map, []

        chunks: list[list[str]] = [
            survivor_ids[i : i + _GET_ENTRIES_CHUNK_SIZE]
            for i in range(0, len(survivor_ids), _GET_ENTRIES_CHUNK_SIZE)
        ]
        responses: list[Any] = await asyncio.gather(
            *(
                self.client.send_websocket_message(
                    {
                        "type": "config/entity_registry/get_entries",
                        "entity_ids": chunk,
                    }
                )
                for chunk in chunks
            ),
            return_exceptions=True,
        )

        failed_chunks = 0
        for chunk, entries_resp in zip(chunks, responses, strict=True):
            # Same convention as _fetch_search_entities: a captured
            # CancelledError means the surrounding task is being torn
            # down — propagate it instead of degrading to a warning.
            if isinstance(entries_resp, asyncio.CancelledError):
                raise entries_resp
            if isinstance(entries_resp, BaseException):
                failed_chunks += 1
                logger.warning(
                    "alias_enrichment_failed: get_entries chunk of %d entities "
                    "raised (err=%r)",
                    len(chunk),
                    entries_resp,
                )
                continue
            try:
                if isinstance(entries_resp, dict) and entries_resp.get("success"):
                    for eid, entry in (entries_resp.get("result", {}) or {}).items():
                        if isinstance(entry, dict):
                            # String entries only: HA serializes the
                            # COMPUTED_NAME alias sentinel ("the computed
                            # entity name is an alias") as null over the
                            # websocket API; ingesting it would put None in
                            # every downstream alias string op.
                            aliases_map[eid] = [
                                a
                                for a in (entry.get("aliases") or [])
                                if isinstance(a, str)
                            ]
                else:
                    failed_chunks += 1
                    logger.warning(
                        "alias_enrichment_failed: get_entries returned non-success "
                        "for a chunk of %d entities (resp=%r)",
                        len(chunk),
                        entries_resp,
                    )
            except (KeyError, TypeError, AttributeError) as alias_err:
                failed_chunks += 1
                logger.warning(
                    "alias_enrichment_failed: malformed payload for a chunk of "
                    "%d entities (err=%r)",
                    len(chunk),
                    alias_err,
                )
        warnings: list[str] = []
        if failed_chunks:
            warnings.append(
                f"Alias enrichment incomplete: {failed_chunks} of {len(chunks)} "
                "entity-registry lookups failed; alias-based matches may be "
                "missing from these results."
            )
        return aliases_map, warnings

    async def _fetch_search_entities(
        self,
        domain_filter: str | None,
        include_hidden: bool,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Fetch + enrich the entity set fed into the fuzzy search layer.

        Fetches states + the slim entity-registry list in parallel (the slim
        view gives ``hidden_by`` and the ids needed for the alias batch fetch;
        aliases live only in ``get_entries``), filters hidden entities, applies
        the optional domain filter, then enriches the survivors with aliases +
        hidden_by. The domain filter runs *before* the alias fetch so the chunked
        alias WS calls only cover entities that survive it. states/registry may be
        pre-fetched and shared by the ha_search orchestrator (``None`` = fetch
        here); the device registry is fetched only when a visibility area/label
        dimension will consume it.
        """
        need_device = await device_registry_needed_for_visibility()
        fetch_coros: list[Any] = []
        fetch_slots: list[str] = []
        if prefetched_states is None:
            fetch_coros.append(self.client.get_states())
            fetch_slots.append("states")
        if prefetched_registry is None:
            fetch_coros.append(
                self.client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
            )
            fetch_slots.append("registry")
        if need_device:
            fetch_coros.append(
                self.client.send_websocket_message(
                    {"type": "config/device_registry/list"}
                )
            )
            fetch_slots.append("device")
        fetched = (
            await asyncio.gather(*fetch_coros, return_exceptions=True)
            if fetch_coros
            else []
        )
        slots = dict(zip(fetch_slots, fetched, strict=True))
        states_result: Any = (
            prefetched_states if prefetched_states is not None else slots.get("states")
        )
        registry_result: Any = (
            prefetched_registry
            if prefetched_registry is not None
            else slots.get("registry")
        )
        device_result: Any = slots.get("device")
        # States-fetch failure is fatal — auth/connection errors must propagate
        # so the caller sees the real cause instead of a bogus "zero matches"
        # with success=True.
        if isinstance(states_result, BaseException):
            raise states_result
        # CancelledError on the registry tasks must propagate too; gather captures
        # it like any other exception when return_exceptions=True.
        if isinstance(registry_result, asyncio.CancelledError):
            raise registry_result
        if isinstance(device_result, asyncio.CancelledError):
            raise device_result
        entities = states_result

        # Opt-in visibility filter. registry_result is the unprojected registry,
        # so entity_category/hidden_by/area_id/labels are present (the slim map
        # below drops them); device_result is the device registry (``None`` when
        # no area/label dimension needs it), which lets the area/label dimensions
        # match a device-bound entity by its device's area/labels. Fails open; do
        # NOT wrap in try/except or the failure mode inverts.
        visibility_hidden, visibility_warnings = await load_hidden_set(
            registry_result, states_result, self.client, device_result
        )
        registry_slim = self._build_registry_slim(registry_result)
        survivor_ids, survivor_states = self._filter_hidden_entities(
            entities, registry_slim, include_hidden, visibility_hidden
        )

        # Apply the domain filter before the alias fetch. The old order fetched
        # aliases for every survivor and filtered at the very end; the final set
        # and its order are identical (each survivor keeps its own aliases), so
        # this only shrinks the alias WS fan-out.
        if domain_filter:
            domain_prefix = f"{domain_filter}."
            kept = [
                (eid, state)
                for eid, state in zip(survivor_ids, survivor_states, strict=True)
                if eid.startswith(domain_prefix)
            ]
            survivor_ids = [eid for eid, _ in kept]
            survivor_states = [state for _, state in kept]

        aliases_map, alias_warnings = await self._fetch_entity_aliases(survivor_ids)
        visibility_warnings = [*visibility_warnings, *alias_warnings]

        # Enrich with aliases + hidden_by for the fuzzy layer. Shallow copy +
        # private-prefixed keys so downstream consumers that round-trip these
        # dicts don't ship internal fields back to clients.
        enriched: list[dict[str, Any]] = []
        for entity, eid in zip(survivor_states, survivor_ids, strict=True):
            slim = registry_slim.get(eid, {})
            enriched.append(
                {
                    **entity,
                    "_aliases": aliases_map.get(eid, []),
                    "_hidden_by": slim.get("hidden_by"),
                }
            )

        return enriched, visibility_warnings

    @staticmethod
    def _format_entity_matches(
        matches: list[dict[str, Any]], include_attributes: bool
    ) -> list[dict[str, Any]]:
        """Project fuzzy-search matches into the public result shape.

        No ``essential_attributes`` fallback — the other search-type branches
        never emit it, so surfacing it only here was a shape asymmetry. Callers
        needing full state should follow up with ``ha_get_state``.
        """
        results: list[dict[str, Any]] = []
        for match in matches:
            result = {
                "entity_id": match["entity_id"],
                "friendly_name": match["friendly_name"],
                "domain": match["domain"],
                "state": match["state"],
                "score": match["score"],
                "match_type": match["match_type"],
            }
            if include_attributes:
                result["attributes"] = match["attributes"]
            results.append(result)
        return results

    async def get_entities_by_area(
        self,
        area_query: str,
        group_by_domain: bool = True,
        include_hidden: bool = True,
    ) -> dict[str, Any]:
        """
        Get entities grouped by area/room using the HA registries for accurate area resolution.

        Uses entity registry, device registry, and area registry to determine
        which area each entity belongs to. Fuzzy matches the query against
        area names, IDs, and area-registry aliases to find the target area(s).

        Args:
            area_query: Area/room name (or alias) to search for
            group_by_domain: Whether to group results by domain within each area
            include_hidden: When True (default), entities with ``hidden_by``
                set in the entity registry are still grouped under their
                area but receive a score penalty when ranked. Pass False
                to filter them out entirely.

        Returns:
            Dictionary with area-grouped entities
        """
        try:
            # Fetch all registries and states in parallel
            results = await asyncio.gather(
                self.client.get_states(),
                self.client.send_websocket_message(
                    {"type": "config/area_registry/list"}
                ),
                self.client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                ),
                self.client.send_websocket_message(
                    {"type": "config/device_registry/list"}
                ),
                return_exceptions=True,
            )

            # States are mandatory — surface connection/auth errors instead of a
            # bogus empty area result with success=True (mirrors
            # _fetch_search_entities). The registry results below still fail open.
            if isinstance(results[0], BaseException):
                raise results[0]
            entities = results[0]
            # A registry sub-task that was individually cancelled must propagate,
            # not be silently degraded to an empty registry by the parsers below
            # (mirrors _fetch_search_entities). Non-cancellation registry errors
            # still fail open.
            for reg_result in results[1:]:
                if isinstance(reg_result, asyncio.CancelledError):
                    raise reg_result
            # Opt-in visibility filter. results[2] is the unprojected entity
            # registry, so entity_category/hidden_by/area_id/labels are present;
            # results[3] is the device registry, so the area/label dimensions
            # match a device-bound entity by its device's area/labels.
            # Fails open (empty set on any error / non-dict payload).
            visibility_hidden, visibility_warnings = await load_hidden_set(
                results[2], results[0], self.client, results[3]
            )
            # Registry failures still fail open, but each names itself: an area
            # search that answers "no match found" because the area registry
            # never arrived must not look like a search that genuinely found
            # nothing (#1947).
            registry_warnings: list[str] = []
            area_registry = self._parse_area_registry(results[1], registry_warnings)
            entity_reg_map = self._parse_entity_reg_map(results[2], registry_warnings)
            device_area_map = self._parse_device_area_map(results[3], registry_warnings)
            degraded_warnings = registry_warnings + visibility_warnings

            area_query_lower = area_query.lower().strip()
            matched_area_ids = self._match_area_ids(area_registry, area_query_lower)

            if not matched_area_ids:
                return merge_visibility_warnings(
                    {
                        "area_query": area_query,
                        "total_areas_found": 0,
                        "total_entities": 0,
                        "areas": {},
                        "available_areas": [
                            {"area_id": aid, "name": ainfo.get("name", aid)}
                            for aid, ainfo in area_registry.items()
                        ],
                    },
                    degraded_warnings,
                )

            entity_area_resolved, hidden_entity_ids = self._resolve_entity_areas(
                entity_reg_map, device_area_map, include_hidden, visibility_hidden
            )
            state_map = self._build_state_map(entities)
            formatted_areas, total_entities = self._format_area_entities(
                matched_area_ids,
                area_registry,
                entity_area_resolved,
                state_map,
                hidden_entity_ids,
                group_by_domain,
            )

            return merge_visibility_warnings(
                {
                    "area_query": area_query,
                    "total_areas_found": len(formatted_areas),
                    "total_entities": total_entities,
                    "areas": formatted_areas,
                },
                degraded_warnings,
            )

        except Exception as e:
            logger.error(f"Error in get_entities_by_area: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Try common room names: salon, chambre, cuisine",
                    "Use smart_entity_search to find entities first",
                ],
                context={"area_query": area_query},
            )
            # ``exception_to_structured_error`` always raises (NoReturn); this
            # explicit raise makes the function's exit unambiguous (no implicit
            # ``return None`` fall-through) and is never reached at runtime.
            raise

    @classmethod
    def _parse_area_registry(
        cls, result: Any, warnings: list[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Parse the area registry into ``area_id -> area info``."""
        area_registry: dict[str, dict[str, Any]] = {}
        for area in cls._extract_registry_list(result, "area registry", warnings):
            area_id = area.get("area_id", "")
            if area_id:
                area_registry[area_id] = area
        return area_registry

    @classmethod
    def _parse_entity_reg_map(
        cls, result: Any, warnings: list[str] | None = None
    ) -> dict[str, dict[str, str | None]]:
        """Parse the entity registry into ``entity_id -> {area_id, device_id, hidden_by}``."""
        entity_reg_map: dict[str, dict[str, str | None]] = {}
        for entry in cls._extract_registry_list(result, "entity registry", warnings):
            entity_id = entry.get("entity_id")
            if entity_id:
                entity_reg_map[entity_id] = {
                    "area_id": entry.get("area_id"),
                    "device_id": entry.get("device_id"),
                    "hidden_by": entry.get("hidden_by"),
                }
        return entity_reg_map

    @classmethod
    def _parse_device_area_map(
        cls, result: Any, warnings: list[str] | None = None
    ) -> dict[str, str | None]:
        """Parse the device registry into ``device_id -> area_id``."""
        device_area_map: dict[str, str | None] = {}
        for device in cls._extract_registry_list(result, "device registry", warnings):
            device_id = device.get("id", "")
            if device_id:
                device_area_map[device_id] = device.get("area_id")
        return device_area_map

    @staticmethod
    def _match_area_ids(
        area_registry: dict[str, dict[str, Any]], area_query_lower: str
    ) -> set[str]:
        """Resolve the query to area_ids: exact id/name/alias match, else fuzzy.

        Two-pass: pass 1 collects exact id / name / alias matches; if any are
        found, fuzzy aggregation is skipped entirely. This makes ``area_filter``
        honor a literal area_id from ``ha_list_floors_areas`` — a query like
        ``"bedroom_kids"`` would otherwise also fuzzy-match its parent
        ``"bedroom"`` (partial_ratio=100) and aggregate sibling areas' entities.
        Aliases (per-area registry, used by HA voice config) mirror the
        entity-side enrichment in smart_entity_search.
        """
        exact_area_ids: set[str] = set()
        fuzzy_area_ids: set[str] = set()

        for area_id, area_info in area_registry.items():
            area_name = area_info.get("name", "")
            area_aliases = area_info.get("aliases", []) or []
            # Exact match on area_id, name, or any alias (case-insensitive)
            if (
                area_query_lower == area_id.lower()
                or area_query_lower == area_name.lower()
                or any(
                    area_query_lower == a.lower()
                    for a in area_aliases
                    if isinstance(a, str)
                )
            ):
                exact_area_ids.add(area_id)
                continue
            # Fuzzy match on area name, id, or any alias
            name_score = calculate_partial_ratio(area_query_lower, area_name.lower())
            id_score = calculate_partial_ratio(area_query_lower, area_id.lower())
            alias_score = max(
                (
                    calculate_partial_ratio(area_query_lower, a.lower())
                    for a in area_aliases
                    if isinstance(a, str)
                ),
                default=0,
            )
            if max(name_score, id_score, alias_score) >= 80:
                fuzzy_area_ids.add(area_id)

        # Exact matches win — fuzzy aggregation only runs when no area_query is
        # itself an area_id / name / alias.
        return exact_area_ids or fuzzy_area_ids

    @staticmethod
    def _resolve_entity_areas(
        entity_reg_map: dict[str, dict[str, str | None]],
        device_area_map: dict[str, str | None],
        include_hidden: bool,
        visibility_hidden: set[str],
    ) -> tuple[dict[str, str], set[str]]:
        """Map entity_id -> resolved area_id (entity area > device area).

        Hidden entities are filtered only when include_hidden is False;
        otherwise they pass through and downstream applies the score penalty so
        they sort below visible matches. Returns (entity_area_resolved,
        hidden_entity_ids).
        """
        entity_area_resolved: dict[str, str] = {}
        hidden_entity_ids: set[str] = set()
        for entity_id, reg_info in entity_reg_map.items():
            if entity_id in visibility_hidden:
                continue
            is_hidden = reg_info.get("hidden_by") is not None
            if is_hidden and not include_hidden:
                continue
            if is_hidden:
                hidden_entity_ids.add(entity_id)
            area_id = reg_info.get("area_id")
            device_id = reg_info.get("device_id")
            if not area_id and device_id:
                area_id = device_area_map.get(device_id)
            if area_id:
                entity_area_resolved[entity_id] = area_id
        return entity_area_resolved, hidden_entity_ids

    @staticmethod
    def _build_state_map(
        entities: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Map entity_id -> state object for detail lookups."""
        state_map: dict[str, dict[str, Any]] = {}
        for entity in entities:
            eid = entity.get("entity_id", "")
            if eid:
                state_map[eid] = entity
        return state_map

    @staticmethod
    def _build_area_entity_record(
        entity_id: str,
        state_map: dict[str, dict[str, Any]],
        hidden_entity_ids: set[str],
        include_domain: bool,
    ) -> dict[str, Any]:
        """Build one area entity record.

        ``_hidden_by`` is carried as a sentinel ("hidden" or None) so downstream
        branches can apply the score penalty without a second registry lookup.
        """
        state_info = state_map.get(entity_id, {})
        record: dict[str, Any] = {
            "entity_id": entity_id,
            "friendly_name": state_info.get("attributes", {}).get(
                "friendly_name", entity_id
            ),
            "state": state_info.get("state", "unknown"),
            "_hidden_by": "hidden" if entity_id in hidden_entity_ids else None,
        }
        if include_domain:
            record["domain"] = entity_id.split(".")[0]
        return record

    @classmethod
    def _group_area_entities_by_domain(
        cls,
        area_entities: list[str],
        state_map: dict[str, dict[str, Any]],
        hidden_entity_ids: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        """Group an area's entity records by domain."""
        domains: dict[str, list[dict[str, Any]]] = {}
        for entity_id in area_entities:
            domain = entity_id.split(".")[0]
            domains.setdefault(domain, []).append(
                cls._build_area_entity_record(
                    entity_id, state_map, hidden_entity_ids, include_domain=False
                )
            )
        return domains

    @classmethod
    def _format_area_entities(
        cls,
        matched_area_ids: set[str],
        area_registry: dict[str, dict[str, Any]],
        entity_area_resolved: dict[str, str],
        state_map: dict[str, dict[str, Any]],
        hidden_entity_ids: set[str],
        group_by_domain: bool,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Collect matched areas' entities into the response shape.

        Alias data is NOT enriched here — exposing private ``_aliases`` on a
        public method would leak through any caller that round-trips this
        response. The area+query consumer in tools_search.py fetches aliases on
        its own when needed. Returns (formatted_areas, total_entities).
        """
        formatted_areas: dict[str, dict[str, Any]] = {}
        total_entities = 0
        for area_id in matched_area_ids:
            area_info = area_registry.get(area_id, {})
            area_name = area_info.get("name", area_id)
            area_entities = [
                entity_id
                for entity_id, resolved_area in entity_area_resolved.items()
                if resolved_area == area_id
            ]

            if group_by_domain:
                entities_payload: Any = cls._group_area_entities_by_domain(
                    area_entities, state_map, hidden_entity_ids
                )
            else:
                entities_payload = [
                    cls._build_area_entity_record(
                        entity_id, state_map, hidden_entity_ids, include_domain=True
                    )
                    for entity_id in area_entities
                ]

            formatted_areas[area_id] = {
                "area_name": area_name,
                "area_id": area_id,
                "entity_count": len(area_entities),
                "entities": entities_payload,
            }
            total_entities += len(area_entities)
        return formatted_areas, total_entities
