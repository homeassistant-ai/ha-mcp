"""Entity fuzzy search and area-grouped entity listing."""

import asyncio
import logging
from typing import Any

from ...utils.fuzzy_search import calculate_partial_ratio
from ..helpers import exception_to_structured_error
from ._base import _SearchBase

logger = logging.getLogger(__name__)


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

        Returns:
            Dictionary with search results and metadata
        """
        try:
            # HA domains are canonically lowercase and unpadded; defend the
            # service layer so internal callers get the same normalization the
            # tool layer applies (strip + lowercase before the prefix match).
            if domain_filter:
                domain_filter = domain_filter.strip().lower()

            entities = await self._fetch_search_entities(domain_filter, include_hidden)

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

            return response

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
            slim = registry_slim.get(eid, {})
            hidden_by = slim.get("hidden_by")
            if hidden_by is not None and not include_hidden:
                continue
            survivor_ids.append(eid)
            survivor_states.append(entity)
        return survivor_ids, survivor_states

    async def _fetch_entity_aliases(
        self, survivor_ids: list[str]
    ) -> dict[str, list[str]]:
        """Batch-fetch full registry entries for aliases.

        ``config/entity_registry/list`` deliberately omits ``aliases``;
        ``get_entries`` includes them. One extra round-trip enriches the
        survivor set without N+1 fan-out.
        """
        aliases_map: dict[str, list[str]] = {}
        if not survivor_ids:
            return aliases_map
        try:
            entries_resp = await self.client.send_websocket_message(
                {
                    "type": "config/entity_registry/get_entries",
                    "entity_ids": survivor_ids,
                }
            )
            if isinstance(entries_resp, dict) and entries_resp.get("success"):
                for eid, entry in (entries_resp.get("result", {}) or {}).items():
                    if isinstance(entry, dict):
                        aliases_map[eid] = entry.get("aliases", []) or []
            else:
                logger.warning(
                    "alias_enrichment_failed: get_entries returned non-success "
                    "for %d entities (resp=%r)",
                    len(survivor_ids),
                    entries_resp,
                )
        except (KeyError, TypeError, AttributeError) as alias_err:
            logger.warning(
                "alias_enrichment_failed: malformed payload for %d entities (err=%r)",
                len(survivor_ids),
                alias_err,
            )
        return aliases_map

    async def _fetch_search_entities(
        self, domain_filter: str | None, include_hidden: bool
    ) -> list[dict[str, Any]]:
        """Fetch + enrich the entity set fed into the fuzzy search layer.

        Fetches states + the slim entity-registry list in parallel (the slim
        view gives ``hidden_by`` and the ids needed for the alias batch fetch;
        aliases live only in ``get_entries``), filters hidden entities, enriches
        survivors with aliases + hidden_by, then applies the optional domain
        filter.
        """
        results = await asyncio.gather(
            self.client.get_states(),
            self.client.send_websocket_message({"type": "config/entity_registry/list"}),
            return_exceptions=True,
        )
        # States-fetch failure is fatal — auth/connection errors must propagate
        # so the caller sees the real cause instead of a bogus "zero matches"
        # with success=True.
        if isinstance(results[0], BaseException):
            raise results[0]
        # CancelledError on the registry task must propagate too; gather captures
        # it like any other exception when return_exceptions=True.
        if isinstance(results[1], asyncio.CancelledError):
            raise results[1]
        entities = results[0]

        registry_slim = self._build_registry_slim(results[1])
        survivor_ids, survivor_states = self._filter_hidden_entities(
            entities, registry_slim, include_hidden
        )
        aliases_map = await self._fetch_entity_aliases(survivor_ids)

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

        if domain_filter:
            enriched = [
                e
                for e in enriched
                if e.get("entity_id", "").startswith(f"{domain_filter}.")
            ]
        return enriched

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

            entities = results[0] if not isinstance(results[0], Exception) else []
            area_registry = self._parse_area_registry(results[1])
            entity_reg_map = self._parse_entity_reg_map(results[2])
            device_area_map = self._parse_device_area_map(results[3])

            area_query_lower = area_query.lower().strip()
            matched_area_ids = self._match_area_ids(area_registry, area_query_lower)

            if not matched_area_ids:
                return {
                    "area_query": area_query,
                    "total_areas_found": 0,
                    "total_entities": 0,
                    "areas": {},
                    "available_areas": [
                        {"area_id": aid, "name": ainfo.get("name", aid)}
                        for aid, ainfo in area_registry.items()
                    ],
                }

            entity_area_resolved, hidden_entity_ids = self._resolve_entity_areas(
                entity_reg_map, device_area_map, include_hidden
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

            return {
                "area_query": area_query,
                "total_areas_found": len(formatted_areas),
                "total_entities": total_entities,
                "areas": formatted_areas,
            }

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

    @classmethod
    def _parse_area_registry(cls, result: Any) -> dict[str, dict[str, Any]]:
        """Parse the area registry into ``area_id -> area info``."""
        area_registry: dict[str, dict[str, Any]] = {}
        for area in cls._extract_registry_list(result, "area registry"):
            area_id = area.get("area_id", "")
            if area_id:
                area_registry[area_id] = area
        return area_registry

    @classmethod
    def _parse_entity_reg_map(cls, result: Any) -> dict[str, dict[str, str | None]]:
        """Parse the entity registry into ``entity_id -> {area_id, device_id, hidden_by}``."""
        entity_reg_map: dict[str, dict[str, str | None]] = {}
        for entry in cls._extract_registry_list(result, "entity registry"):
            entity_id = entry.get("entity_id")
            if entity_id:
                entity_reg_map[entity_id] = {
                    "area_id": entry.get("area_id"),
                    "device_id": entry.get("device_id"),
                    "hidden_by": entry.get("hidden_by"),
                }
        return entity_reg_map

    @classmethod
    def _parse_device_area_map(cls, result: Any) -> dict[str, str | None]:
        """Parse the device registry into ``device_id -> area_id``."""
        device_area_map: dict[str, str | None] = {}
        for device in cls._extract_registry_list(result, "device registry"):
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
