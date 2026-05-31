"""
Smart search tools for Home Assistant MCP server.
"""

import asyncio
import logging
import os
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings
from ..utils.fuzzy_search import (
    BM25Scorer,
    calculate_partial_ratio,
    calculate_ratio,
    create_fuzzy_searcher,
    tokenize,
)
from .helpers import exception_to_structured_error, safe_info, safe_progress
from .tools_config_dashboards import fetch_dashboards_list
from .tools_config_entry_flow import FLOW_HELPER_TYPES
from .tools_integrations import fetch_entry_options

logger = logging.getLogger(__name__)

# Default concurrency limit for parallel operations
DEFAULT_CONCURRENCY_LIMIT = 20

# Bulk fetch timeouts (in seconds)
BULK_REST_TIMEOUT = 5.0  # Timeout for bulk REST endpoint calls
BULK_WEBSOCKET_TIMEOUT = 3.0  # Timeout for bulk WebSocket calls
INDIVIDUAL_CONFIG_TIMEOUT = 5.0  # Timeout for individual config fetches


# Time budgets for fallback individual fetching (in seconds).
# Configurable via env vars for instances with many automations/scripts.
def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.warning(f"Invalid value for {key}={raw!r}, using default {default}")
        return default


AUTOMATION_CONFIG_TIME_BUDGET = _env_float("HAMCP_AUTOMATION_CONFIG_TIME_BUDGET", 30.0)
SCRIPT_CONFIG_TIME_BUDGET = _env_float("HAMCP_SCRIPT_CONFIG_TIME_BUDGET", 20.0)
SCENE_CONFIG_TIME_BUDGET = _env_float("HAMCP_SCENE_CONFIG_TIME_BUDGET", 20.0)

# Batch size for parallel individual config fetches (Attempt C fallback)
INDIVIDUAL_FETCH_BATCH_SIZE = 10


def _simplify_states_summary(
    states_summary: dict[str, int],
    detail_level: str,
    max_states: int | None = None,
) -> dict[str, int]:
    """Keep only the most common states, aggregate the rest into _other.

    Args:
        states_summary: Original {state: count} mapping.
        detail_level: "minimal", "standard", or "full".
        max_states: Override cap (None = 5 for minimal, 10 for standard).

    Returns:
        Capped states_summary with ``_other`` count when truncated.
    """
    if detail_level == "full":
        return states_summary

    if max_states is None:
        max_states = 5 if detail_level == "minimal" else 10

    if len(states_summary) <= max_states:
        return states_summary

    sorted_states = sorted(states_summary.items(), key=lambda x: x[1], reverse=True)
    top = dict(sorted_states[:max_states])
    other_count = sum(count for _, count in sorted_states[max_states:])
    if other_count > 0:
        top["_other"] = other_count
    return top


class SmartSearchTools:
    """Smart search tools with fuzzy matching and AI optimization."""

    def __init__(
        self, client: HomeAssistantClient | None = None, fuzzy_threshold: int = 60
    ):
        """Initialize with Home Assistant client."""
        # Always load settings for configuration access
        self.settings = get_global_settings()

        # Use provided client or create new one
        if client is None:
            self.client = HomeAssistantClient()
            fuzzy_threshold = self.settings.fuzzy_threshold
        else:
            self.client = client

        self.fuzzy_searcher = create_fuzzy_searcher(threshold=fuzzy_threshold)

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

    async def get_system_overview(
        self,
        detail_level: str = "standard",
        max_entities_per_domain: int | None = None,
        include_state: bool | None = None,
        include_entity_id: bool | None = None,
        domains_filter: list[str] | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Get AI-friendly system overview with intelligent categorization.

        Args:
            detail_level: Level of detail to return:
                - "minimal": 10 entities/domain sample, top-5 states (friendly_name only)
                - "standard": ALL entities, top-10 states (friendly_name only)
                - "full": ALL entities with entity_id + friendly_name + state + full states
            max_entities_per_domain: Override default entity cap (0 = no limit)
            include_state: Override whether to include state field
            include_entity_id: Override whether to include entity_id field
            domains_filter: Only include these domains (None = all)
            limit: Max total entities to include across all domains.
                Defaults to None (no limit) for minimal, 200 for standard/full.
                Domain counts and states_summary are always complete regardless.
            offset: Number of entities to skip for pagination (default: 0)

        Returns:
            System overview optimized for AI understanding at requested detail level
        """
        try:
            # Fetch all data in parallel. return_exceptions=True so a degraded
            # registry/service fetch doesn't abort the whole overview.
            results = await asyncio.gather(
                self.client.get_states(),
                self.client.get_services(),
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

            # Entities are mandatory — surface connection/auth errors immediately.
            if isinstance(results[0], Exception):
                raise results[0]
            entities = results[0]

            # Services failure affects total count + catalog; log at warning.
            partial_warnings: list[str] = []
            if isinstance(results[1], Exception):
                logger.warning(f"Could not fetch services: {results[1]}")
                partial_warnings.append(f"Services unavailable: {results[1]}")
                services: Any = []
            else:
                services = results[1]

            # Registry failures degrade area enrichment only; logged at debug.
            area_registry = self._extract_registry_list(results[2], "area registry")
            entity_registry = self._extract_registry_list(results[3], "entity registry")
            device_registry = self._extract_registry_list(results[4], "device registry")
            entity_area_map = self._build_entity_area_map(
                entity_registry, device_registry
            )

            (
                max_entities_per_domain,
                uncap_all,
                include_state,
                include_entity_id,
            ) = self._resolve_overview_display_opts(
                detail_level, max_entities_per_domain, include_state, include_entity_id
            )

            # Pre-populate area_stats so empty areas still appear
            area_stats = self._init_area_stats(area_registry)

            domains_filter_set: set[str] | None = None
            if domains_filter:
                domains_filter_set = {d.strip().lower() for d in domains_filter}

            # Count all domains before filtering (for system_summary)
            all_domains = {e["entity_id"].split(".")[0] for e in entities}

            domain_stats, device_types = self._analyze_entities_by_domain(
                entities,
                domains_filter_set,
                area_stats,
                entity_area_map,
                include_state,
                include_entity_id,
            )

            sorted_domains = sorted(
                domain_stats.items(), key=lambda x: x[1]["count"], reverse=True
            )
            service_stats, total_services = self._build_service_stats(services)
            ai_insights = self._build_ai_insights(domain_stats, sorted_domains)
            formatted_domain_stats = self._format_domain_stats(
                sorted_domains, max_entities_per_domain, detail_level, uncap_all
            )
            pagination_metadata = self._paginate_overview_entities(
                formatted_domain_stats, limit, offset, detail_level
            )

            # totals always reflect the full system, regardless of filtering
            system_summary: dict[str, Any] = {
                "total_entities": len(entities),
                "total_domains": len(all_domains),
                "total_services": total_services,
                "total_areas": len(area_registry),
            }
            if domains_filter_set:
                system_summary["filtered_domains"] = sorted(domains_filter_set)

            return self._assemble_overview_response(
                system_summary=system_summary,
                formatted_domain_stats=formatted_domain_stats,
                area_stats=area_stats,
                ai_insights=ai_insights,
                detail_level=detail_level,
                pagination_metadata=pagination_metadata,
                partial_warnings=partial_warnings,
                device_types=device_types,
                service_stats=service_stats,
            )

        except Exception as e:
            logger.error(f"Error in get_system_overview: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify API token permissions",
                    "Try test_connection first",
                ],
                context={
                    "total_entities": 0,
                    "entity_summary": {},
                    "controllable_devices": {},
                },
            )

    @staticmethod
    def _extract_registry_list(result: Any, label: str) -> list[dict[str, Any]]:
        """Unwrap a WS registry-list result, returning ``[]`` on error/failure.

        Exceptions are logged at debug because every caller treats missing
        registry data as non-fatal rather than raising: the overview degrades
        its area enrichment, and the area search degrades to "no match found".
        """
        if isinstance(result, Exception):
            logger.debug(f"Could not fetch {label}: {result}")
            return []
        if isinstance(result, dict) and result.get("success"):
            registry: list[dict[str, Any]] = result.get("result", [])
            return registry
        return []

    @staticmethod
    def _build_entity_area_map(
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]],
    ) -> dict[str, str | None]:
        """Map entity_id -> area_id. Priority: entity direct area_id > device area_id."""
        device_area_map: dict[str, str | None] = {}
        for device in device_registry:
            device_id = device.get("id", "")
            if device_id:
                device_area_map[device_id] = device.get("area_id")

        entity_area_map: dict[str, str | None] = {}
        for entry in entity_registry:
            entity_id = entry.get("entity_id")
            area_id = entry.get("area_id")
            if not area_id:
                device_id = entry.get("device_id")
                if device_id:
                    area_id = device_area_map.get(device_id)
            if entity_id:
                entity_area_map[entity_id] = area_id
        return entity_area_map

    @staticmethod
    def _resolve_overview_display_opts(
        detail_level: str,
        max_entities_per_domain: int | None,
        include_state: bool | None,
        include_entity_id: bool | None,
    ) -> tuple[int | None, bool, bool, bool]:
        """Resolve detail-level display defaults.

        ``max_entities_per_domain == 0`` means "uncap everything" (entities +
        states). standard/full keep no default cap (None = all entities).
        """
        uncap_all = max_entities_per_domain == 0
        if max_entities_per_domain is None and detail_level == "minimal":
            max_entities_per_domain = 10
        if include_state is None:
            include_state = detail_level == "full"
        if include_entity_id is None:
            include_entity_id = detail_level == "full"
        return max_entities_per_domain, uncap_all, include_state, include_entity_id

    @staticmethod
    def _init_area_stats(
        area_registry: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """Pre-populate per-area stats so areas with no entities still appear."""
        area_stats: dict[str, dict[str, Any]] = {}
        for area in area_registry:
            area_id = area.get("area_id", "")
            if area_id:
                area_stats[area_id] = {
                    "name": area.get("name", area_id),
                    "count": 0,
                    "domains": {},
                }
        return area_stats

    @staticmethod
    def _record_entity_area(
        area_stats: dict[str, dict[str, Any]],
        entity_area_map: dict[str, str | None],
        entity_id: str,
        domain: str,
    ) -> None:
        """Increment per-area + per-area-domain counts for one entity."""
        area_id = entity_area_map.get(entity_id)
        if area_id and area_id in area_stats:
            area_stats[area_id]["count"] += 1
            domains = area_stats[area_id]["domains"]
            domains[domain] = domains.get(domain, 0) + 1

    @staticmethod
    def _record_device_type(
        device_types: dict[str, int], attributes: dict[str, Any]
    ) -> None:
        """Increment the device_class tally for one entity, if it has one."""
        device_class = attributes.get("device_class")
        if device_class:
            device_types[device_class] = device_types.get(device_class, 0) + 1

    def _analyze_entities_by_domain(
        self,
        entities: list[dict[str, Any]],
        domains_filter_set: set[str] | None,
        area_stats: dict[str, dict[str, Any]],
        entity_area_map: dict[str, str | None],
        include_state: bool,
        include_entity_id: bool,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
        """Tally per-domain stats, area stats (mutated in place), and device types."""
        domain_stats: dict[str, dict[str, Any]] = {}
        device_types: dict[str, int] = {}

        for entity in entities:
            entity_id = entity["entity_id"]
            domain = entity_id.split(".")[0]
            if domains_filter_set and domain not in domains_filter_set:
                continue

            attributes = entity.get("attributes", {})
            state = entity.get("state", "unknown")

            stats = domain_stats.setdefault(
                domain, {"count": 0, "states_summary": {}, "all_entities": []}
            )
            stats["count"] += 1
            stats["states_summary"][state] = stats["states_summary"].get(state, 0) + 1

            entity_data: dict[str, Any] = {
                "friendly_name": attributes.get("friendly_name", entity_id),
            }
            if include_entity_id:
                entity_data["entity_id"] = entity_id
            if include_state:
                entity_data["state"] = state
            stats["all_entities"].append(entity_data)

            self._record_entity_area(area_stats, entity_area_map, entity_id, domain)
            self._record_device_type(device_types, attributes)

        return domain_stats, device_types

    @staticmethod
    def _build_service_stats(
        services: Any,
    ) -> tuple[dict[str, dict[str, Any]], int]:
        """Summarize the service catalog into per-domain counts and a grand total."""
        service_stats: dict[str, dict[str, Any]] = {}
        total_services = 0
        if isinstance(services, list):
            for domain_obj in services:
                domain = domain_obj.get("domain", "unknown")
                domain_services = domain_obj.get("services", {})
                service_stats[domain] = {
                    "count": len(domain_services),
                    "services": list(domain_services.keys()),
                }
                total_services += len(domain_services)
        return service_stats, total_services

    @staticmethod
    def _build_ai_insights(
        domain_stats: dict[str, dict[str, Any]],
        sorted_domains: list[tuple[str, dict[str, Any]]],
    ) -> dict[str, Any]:
        """Derive coarse AI-facing hints (common/controllable/monitoring domains)."""
        return {
            "most_common_domains": [domain for domain, _ in sorted_domains[:5]],
            "controllable_devices": [
                domain
                for domain in domain_stats
                if domain in ["light", "switch", "climate", "media_player", "cover"]
            ],
            "monitoring_sensors": [
                domain
                for domain in domain_stats
                if domain in ["sensor", "binary_sensor", "camera"]
            ],
            "automation_ready": "automation" in domain_stats
            and domain_stats["automation"]["count"] > 0,
        }

    @staticmethod
    def _format_domain_stats(
        sorted_domains: list[tuple[str, dict[str, Any]]],
        max_entities_per_domain: int | None,
        detail_level: str,
        uncap_all: bool,
    ) -> dict[str, dict[str, Any]]:
        """Apply the per-domain entity cap and simplify state summaries."""
        formatted_domain_stats: dict[str, dict[str, Any]] = {}
        for domain, stats in sorted_domains:
            all_entities = stats["all_entities"]
            if max_entities_per_domain and len(all_entities) > max_entities_per_domain:
                if detail_level == "minimal":
                    # Random sample so minimal isn't biased to early entities
                    selected_entities = random.sample(
                        all_entities, max_entities_per_domain
                    )
                else:
                    selected_entities = all_entities[:max_entities_per_domain]
                truncated = True
            else:
                selected_entities = all_entities
                truncated = False

            formatted_domain_stats[domain] = {
                "count": stats["count"],
                "states_summary": _simplify_states_summary(
                    stats["states_summary"],
                    "full" if uncap_all else detail_level,
                ),
                "entities": selected_entities,
                "truncated": truncated,
            }
        return formatted_domain_stats

    @staticmethod
    def _allocate_page_one(
        formatted_domain_stats: dict[str, dict[str, Any]],
        effective_limit: int,
        total_entity_count: int,
    ) -> int:
        """Distribute the page-1 budget: a min allocation per domain, rest proportional.

        Gives each domain a minimum slice so the LLM sees entities from every
        domain, then distributes the remaining budget proportionally. Mutates
        ``formatted_domain_stats`` in place; returns the count included.
        """
        min_per_domain = 3
        num_domains = len(formatted_domain_stats)
        reserved = min(min_per_domain * num_domains, effective_limit)
        remaining_budget = effective_limit - reserved

        entities_included = 0
        for domain_data in formatted_domain_stats.values():
            domain_entities = domain_data["entities"]
            domain_len = len(domain_entities)
            base = min(min_per_domain, domain_len)
            if total_entity_count > 0 and remaining_budget > 0:
                extra = int(remaining_budget * domain_len / total_entity_count)
            else:
                extra = 0
            take = min(base + extra, domain_len)
            if take < domain_len:
                domain_data["entities"] = domain_entities[:take]
                domain_data["truncated"] = True
            entities_included += len(domain_data["entities"])
        return entities_included

    @staticmethod
    def _allocate_subsequent_pages(
        formatted_domain_stats: dict[str, dict[str, Any]],
        effective_limit: int,
        offset: int,
    ) -> int:
        """Apply pages-2+ sequential skip/take across domains. Mutates in place."""
        entities_skipped = 0
        entities_included = 0
        for domain_data in formatted_domain_stats.values():
            domain_entities = domain_data["entities"]
            domain_len = len(domain_entities)

            skip_from_domain = max(0, min(domain_len, offset - entities_skipped))
            budget_left = effective_limit - entities_included
            take_from_domain = max(0, min(domain_len - skip_from_domain, budget_left))

            if skip_from_domain > 0 or take_from_domain < domain_len:
                domain_data["entities"] = domain_entities[
                    skip_from_domain : skip_from_domain + take_from_domain
                ]
                if take_from_domain < domain_len:
                    domain_data["truncated"] = True

            entities_skipped += skip_from_domain
            entities_included += take_from_domain
        return entities_included

    def _paginate_overview_entities(
        self,
        formatted_domain_stats: dict[str, dict[str, Any]],
        limit: int | None,
        offset: int,
        detail_level: str,
    ) -> dict[str, Any] | None:
        """Apply global entity pagination across domains; returns metadata or None.

        Default limit: None for minimal (already capped per-domain), 200 for
        standard/full. Domain counts/states_summary stay complete regardless.
        """
        effective_limit = limit
        if effective_limit is None and detail_level != "minimal":
            effective_limit = 200
        if effective_limit is None:
            return None

        total_entity_count = sum(
            len(ds["entities"]) for ds in formatted_domain_stats.values()
        )
        if offset == 0:
            entities_included = self._allocate_page_one(
                formatted_domain_stats, effective_limit, total_entity_count
            )
        else:
            entities_included = self._allocate_subsequent_pages(
                formatted_domain_stats, effective_limit, offset
            )

        has_more = (offset + entities_included) < total_entity_count
        return {
            "total_entity_results": total_entity_count,
            "offset": offset,
            "limit": effective_limit,
            "entities_returned": entities_included,
            "has_more": has_more,
            "next_offset": offset + effective_limit if has_more else None,
        }

    @staticmethod
    def _assemble_overview_response(
        *,
        system_summary: dict[str, Any],
        formatted_domain_stats: dict[str, dict[str, Any]],
        area_stats: dict[str, dict[str, Any]],
        ai_insights: dict[str, Any],
        detail_level: str,
        pagination_metadata: dict[str, Any] | None,
        partial_warnings: list[str],
        device_types: dict[str, int],
        service_stats: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Assemble the final overview response and attach level-specific fields."""
        base_response: dict[str, Any] = {
            "success": True,
            "system_summary": system_summary,
            "domain_stats": formatted_domain_stats,
            "area_analysis": (
                {area: {"count": info["count"]} for area, info in area_stats.items()}
                if detail_level == "minimal"
                else area_stats
            ),
            "ai_insights": ai_insights,
        }

        if pagination_metadata:
            base_response["pagination"] = pagination_metadata

        if partial_warnings:
            base_response["partial"] = True
            base_response["warnings"] = partial_warnings

        # Full: add device types and service catalog
        if detail_level == "full":
            base_response["device_types"] = device_types
            base_response["service_availability"] = service_stats

        return base_response

    async def deep_search(
        self,
        query: str,
        search_types: list[str] | None = None,
        limit: int = 5,
        offset: int = 0,
        include_config: bool = False,
        concurrency_limit: int = DEFAULT_CONCURRENCY_LIMIT,
        exact_match: bool = True,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Deep search across automation, script, scene, helper, and dashboard
        definitions.

        Searches not just entity names but also within configuration definitions
        including triggers, actions, sequences, scene entity sets, and other
        config fields.

        Args:
            query: Search query (can be partial, with typos when exact_match=False)
            search_types: Types to search (default: ["automation", "script", "scene", "helper"])
            limit: Maximum total results to return (default: 5)
            offset: Number of results to skip for pagination (default: 0)
            include_config: Include full config in results (default: False)
            concurrency_limit: Max concurrent API calls for config fetching
            exact_match: Use exact substring matching (default: True). Set False for fuzzy.

        Returns:
            Dictionary with search results grouped by type
        """
        if search_types is None:
            search_types = ["automation", "script", "scene", "helper"]

        try:
            results: dict[str, list[dict[str, Any]]] = {
                "automations": [],
                "scripts": [],
                "scenes": [],
                "helpers": [],
                "dashboards": [],
            }

            query_lower = query.lower().strip()

            total_phases = len(search_types) + 1  # +1 for initial state fetch
            await safe_info(
                ctx, f"deep_search starting: query={query!r} types={search_types}"
            )
            await safe_progress(
                ctx,
                progress=0,
                total=total_phases,
                message="fetching entity states",
            )

            # Fetch all entities once at the beginning to avoid repeated calls
            all_entities = await self.client.get_states()
            phase_done = 1
            await safe_progress(
                ctx,
                progress=phase_done,
                total=total_phases,
                message=f"fetched {len(all_entities)} entity states",
            )

            # Pre-resolve unique_ids from cached entity states to avoid redundant API calls
            automation_unique_id_map = self._build_automation_uid_map(all_entities)

            # Create semaphore for limiting concurrent API calls
            semaphore = asyncio.Semaphore(concurrency_limit)

            # Scene Attempt-C signals that drive the optional ``partial`` flag.
            # Defaulted here so the tail builds a clean response when scene
            # search is not requested.
            scene_stats: dict[str, Any] = {
                "failed": 0,
                "skipped": 0,
                "integration_skipped": 0,
                "registry_failed": False,
            }

            if "automation" in search_types:
                results["automations"] = await self._deep_search_automations(
                    all_entities, automation_unique_id_map, query_lower, exact_match
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"automations searched ({len(results['automations'])} matches)",
                )

            if "script" in search_types:
                results["scripts"] = await self._deep_search_scripts(
                    all_entities, query_lower, exact_match
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"scripts searched ({len(results['scripts'])} matches)",
                )

            if "scene" in search_types:
                (
                    results["scenes"],
                    scene_stats["failed"],
                    scene_stats["skipped"],
                    scene_stats["integration_skipped"],
                    scene_stats["registry_failed"],
                ) = await self._deep_search_scenes(
                    all_entities, query_lower, exact_match
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"scenes searched ({len(results['scenes'])} matches)",
                )

            if "helper" in search_types:
                results["helpers"] = await self._deep_search_helpers(
                    query_lower, exact_match, semaphore, include_config
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"helpers searched ({len(results['helpers'])} matches)",
                )

            if "dashboard" in search_types:
                results["dashboards"] = await self._deep_search_dashboards(
                    query_lower, exact_match, semaphore
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"dashboards searched ({len(results['dashboards'])} matches)",
                )

            return self._paginate_and_build_response(
                results,
                query,
                search_types,
                offset,
                limit,
                include_config,
                scene_stats,
            )

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error in deep_search: {e}")
            exception_to_structured_error(
                e,
                suggestions=[
                    "Check Home Assistant connection",
                    "Verify automation/script/helper entities exist",
                    "Try simpler search terms",
                ],
                context={
                    "query": query,
                    "automations": [],
                    "scripts": [],
                    "helpers": [],
                },
            )

    @staticmethod
    def _build_automation_uid_map(
        all_entities: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Map automation entity_id -> unique_id from cached states (no API calls)."""
        uid_map: dict[str, str] = {}
        for e in all_entities:
            eid = e.get("entity_id", "")
            if eid.startswith("automation."):
                uid = e.get("attributes", {}).get("id")
                if uid:
                    uid_map[eid] = uid
        return uid_map

    @staticmethod
    def _index_configs(
        items: list[dict[str, Any]],
        id_of: Callable[[dict[str, Any]], str | None],
    ) -> dict[str, dict[str, Any]]:
        """Build a ``{id: config}`` map, skipping items with no usable id."""
        configs: dict[str, dict[str, Any]] = {}
        for item in items:
            key = id_of(item)
            if key:
                configs[key] = item
        return configs

    async def _bulk_fetch_configs(
        self,
        rest_endpoint: str,
        ws_types: list[str],
        id_of: Callable[[dict[str, Any]], str | None],
        rest_timeout: float,
        label: str,
    ) -> dict[str, dict[str, Any]] | None:
        """Bulk-fetch all configs of one domain: REST endpoint, then WS list endpoints.

        Returns ``{id: config}`` (possibly empty) on the first successful
        attempt, or ``None`` when every attempt failed. An empty-but-successful
        REST list returns ``{}`` (not ``None``) so the caller skips the
        individual-fetch fallback exactly as it would for a populated response.
        """
        try:
            resp = await asyncio.wait_for(
                self.client._request("GET", rest_endpoint),
                timeout=rest_timeout,
            )
            if isinstance(resp, list):
                return self._index_configs(resp, id_of)
        except Exception as e:
            logger.debug(f"{label} REST bulk fetch failed: {e}")

        for ws_type in ws_types:
            try:
                ws_resp = await asyncio.wait_for(
                    self.client.send_websocket_message({"type": ws_type}),
                    timeout=BULK_WEBSOCKET_TIMEOUT,
                )
                if isinstance(ws_resp, dict) and ws_resp.get("success"):
                    return self._index_configs(ws_resp.get("result", []), id_of)
            except Exception as e:
                logger.debug(f"{label} WebSocket bulk fetch ({ws_type}) failed: {e}")
        return None

    async def _individual_fetch_budgeted(
        self,
        ids: list[str],
        fetch_one: Callable[[str], Awaitable[tuple[str, dict[str, Any] | None]]],
        budget: float,
        label: str,
        plural: str,
    ) -> tuple[dict[str, dict[str, Any]], int, int]:
        """Fetch configs individually in parallel batches under a wall-clock budget.

        ``fetch_one(id)`` returns ``(id, config | None)``. New batches stop
        launching once ``budget`` seconds elapse. Returns
        ``(configs, failed_count, skipped_count)``.

        Fetch order is NOT prioritized by name score: deep_search's purpose is
        to find matches INSIDE configs (conditions/actions), not just by name,
        so name-prioritizing would skip the configs most likely to contain
        non-obvious matches. See #879.
        """
        configs: dict[str, dict[str, Any]] = {}
        budget_start = time.perf_counter()
        total_to_fetch = len(ids)
        fetched_count = 0
        failed_count = 0
        skipped_count = 0
        for i in range(0, len(ids), INDIVIDUAL_FETCH_BATCH_SIZE):
            if time.perf_counter() - budget_start > budget:
                skipped_count = total_to_fetch - fetched_count - failed_count
                logger.warning(
                    f"{label} config fetch budget exhausted ({budget}s). "
                    f"Fetched {fetched_count}/{total_to_fetch} "
                    f"({failed_count} failed), skipped {skipped_count} {plural}."
                )
                break
            batch = ids[i : i + INDIVIDUAL_FETCH_BATCH_SIZE]
            batch_results = await asyncio.gather(*[fetch_one(x) for x in batch])
            for key, config in batch_results:
                if config is not None:
                    configs[key] = config
                    fetched_count += 1
                else:
                    failed_count += 1
        return configs, failed_count, skipped_count

    def _score_config_entries(
        self,
        scored: list[tuple[str, str, str | None, int]],
        configs: dict[str, dict[str, Any]],
        query_lower: str,
        exact_match: bool,
    ) -> list[dict[str, Any]]:
        """Score each ``(entity_id, friendly_name, key, name_score)`` against its config.

        Returns one raw match record per entry clearing its threshold. Each
        per-type caller maps these records into its own result shape.
        """
        matches: list[dict[str, Any]] = []
        for entity_id, friendly_name, key, name_score in scored:
            config = configs.get(key, {}) if key else {}
            config_match_score = (
                self._search_in_dict(config, query_lower, exact_match) if config else 0
            )
            total_score, threshold, match_in_name = self._score_deep_match(
                entity_id,
                friendly_name,
                name_score,
                config_match_score,
                query_lower,
                exact_match,
            )
            if total_score >= threshold:
                matches.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "key": key,
                        "config": config,
                        "score": total_score,
                        "match_in_name": match_in_name,
                        "match_in_config": config_match_score >= threshold,
                    }
                )
        return matches

    async def _deep_search_automations(
        self,
        all_entities: list[dict[str, Any]],
        automation_unique_id_map: dict[str, str],
        query_lower: str,
        exact_match: bool,
    ) -> list[dict[str, Any]]:
        """Deep-search automations: 3-tier config fetch (REST bulk -> WS bulk -> individual)."""
        automation_entities = [
            e for e in all_entities if e.get("entity_id", "").startswith("automation.")
        ]

        # Phase 1: Score ALL automations by name (instant, no API calls)
        scored: list[tuple[str, str, str | None, int]] = []
        for entity in automation_entities:
            entity_id = entity.get("entity_id", "")
            friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)
            name_score = self.fuzzy_searcher._calculate_entity_score(
                entity_id, friendly_name, "automation", query_lower
            )
            scored.append(
                (
                    entity_id,
                    friendly_name,
                    automation_unique_id_map.get(entity_id),
                    name_score,
                )
            )

        # Phase 2: bulk fetch (Attempt A REST, Attempt B WebSocket)
        configs = await self._bulk_fetch_configs(
            "/config/automation/config",
            ["config/automation/config/list", "automation/config/list"],
            lambda item: item.get("id"),
            BULK_REST_TIMEOUT,
            "Automation",
        )
        bulk_fetched = configs is not None
        if configs is None:
            configs = {}

        # Attempt C: parallel individual REST calls with time budget (LAST RESORT)
        if not bulk_fetched:
            uids_to_fetch = [
                uid for _, _, uid, _ in scored if uid and uid not in configs
            ]

            async def _fetch_automation_config(
                uid: str,
            ) -> tuple[str, dict[str, Any] | None]:
                try:
                    config = await asyncio.wait_for(
                        self.client._request("GET", f"/config/automation/config/{uid}"),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    return (uid, config)
                except Exception as e:
                    logger.debug(
                        f"Automation individual config fetch ({uid}) failed: {e}"
                    )
                    return (uid, None)

            fetched_configs, _, _ = await self._individual_fetch_budgeted(
                uids_to_fetch,
                _fetch_automation_config,
                AUTOMATION_CONFIG_TIME_BUDGET,
                "Automation",
                "automations",
            )
            configs.update(fetched_configs)

        # Phase 3: Score with whatever configs we have
        return [
            {
                "entity_id": m["entity_id"],
                "friendly_name": m["friendly_name"],
                "score": m["score"],
                "match_in_name": m["match_in_name"],
                "match_in_config": m["match_in_config"],
                "config": m["config"] if m["config"] else None,
            }
            for m in self._score_config_entries(
                scored, configs, query_lower, exact_match
            )
        ]

    async def _deep_search_scripts(
        self,
        all_entities: list[dict[str, Any]],
        query_lower: str,
        exact_match: bool,
    ) -> list[dict[str, Any]]:
        """Deep-search scripts: same 3-tier strategy as automations."""
        script_entities = [
            e for e in all_entities if e.get("entity_id", "").startswith("script.")
        ]

        # Phase 1: Score all scripts by name (instant)
        scored: list[tuple[str, str, str | None, int]] = []
        for entity in script_entities:
            entity_id = entity.get("entity_id", "")
            friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)
            script_id = entity_id.replace("script.", "")
            name_score = self.fuzzy_searcher._calculate_entity_score(
                entity_id, friendly_name, "script", query_lower
            )
            scored.append((entity_id, friendly_name, script_id, name_score))

        # Phase 2: bulk fetch
        configs = await self._bulk_fetch_configs(
            "/config/script/config",
            ["config/script/config/list", "script/config/list"],
            lambda item: (
                item.get("id") or item.get("alias", "").lower().replace(" ", "_")
            ),
            INDIVIDUAL_CONFIG_TIMEOUT,
            "Script",
        )
        bulk_fetched = configs is not None
        if configs is None:
            configs = {}

        # Attempt C: parallel individual fetch with budget (see #879)
        if not bulk_fetched:
            sids_to_fetch = [
                sid for _, _, sid, _ in scored if sid and sid not in configs
            ]

            async def _fetch_script_config(
                sid: str,
            ) -> tuple[str, dict[str, Any] | None]:
                try:
                    config_resp = await asyncio.wait_for(
                        self.client.get_script_config(sid),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    return (sid, config_resp.get("config", {}))
                except Exception as e:
                    logger.debug(f"Script individual config fetch ({sid}) failed: {e}")
                    return (sid, None)

            fetched_configs, _, _ = await self._individual_fetch_budgeted(
                sids_to_fetch,
                _fetch_script_config,
                SCRIPT_CONFIG_TIME_BUDGET,
                "Script",
                "scripts",
            )
            configs.update(fetched_configs)

        # Phase 3: Score scripts
        return [
            {
                "entity_id": m["entity_id"],
                "script_id": m["key"],
                "friendly_name": m["friendly_name"],
                "score": m["score"],
                "match_in_name": m["match_in_name"],
                "match_in_config": m["match_in_config"],
                "config": m["config"] if m["config"] else None,
            }
            for m in self._score_config_entries(
                scored, configs, query_lower, exact_match
            )
        ]

    async def _walk_scene_registry(
        self, configs: dict[str, dict[str, Any]]
    ) -> tuple[set[str], dict[str, str], bool]:
        """Walk the entity registry once for scene metadata (Phase 2.5).

        Returns ``(homeassistant_scene_uids, slug_to_storage_id, registry_failed)``
        and mutates ``configs`` in place, aliasing each bulk-fetched config under
        its entity-id slug. Two outputs:

        1. ``homeassistant_scene_uids`` -- unique_ids backed by
           ``platform == "homeassistant"`` (HA's storage collection).
           Integration-managed scenes (Hue, IKEA, deCONZ, ...) are entity-only;
           the per-id REST endpoint ``/config/scene/config/<id>`` can't fetch
           them and treating their 404s as ``failed_count`` produces a
           misleading ``partial: true`` flag (issue #1168 R3 blocker 2).
        2. Slug-keyed aliases pointing at the bulk-fetched config. HA derives a
           scene's entity_id from the ``name`` field via its own slugify
           (collapsing runs of underscores, replacing all non-alnum with
           underscores, etc.); approximating that with ``.replace()`` chains
           produces near-misses.

        Run unconditionally so the platform filter is available even when the
        bulk fetch returned nothing (the common Hue-only case).
        """
        homeassistant_scene_uids: set[str] = set()
        # Issue #1168 R7 blocker 17/21: registry-derived slug->storage map for
        # the result-builder fallback, keeping the storage key correct for any
        # scene the registry knows about regardless of bulk-fetch coverage.
        slug_to_storage_id: dict[str, str] = {}
        try:
            reg_resp = await asyncio.wait_for(
                self.client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                ),
                timeout=BULK_WEBSOCKET_TIMEOUT,
            )
            if isinstance(reg_resp, dict) and reg_resp.get("success"):
                for entry in reg_resp.get("result") or []:
                    self._index_scene_registry_entry(
                        entry, configs, homeassistant_scene_uids, slug_to_storage_id
                    )
        except Exception as e:
            # Issue #1168 R5 blocker 11: promote DEBUG -> WARNING and signal the
            # fallback so partial_reason can explain why the count looks
            # elevated. A true registry outage previously looked identical to
            # the steady-state happy path on stderr.
            logger.warning(
                "Scene entity-registry augmentation failed: %s; "
                "integration-platform filter unavailable, attempting all scenes",
                e,
            )
            return homeassistant_scene_uids, slug_to_storage_id, True
        return homeassistant_scene_uids, slug_to_storage_id, False

    @staticmethod
    def _index_scene_registry_entry(
        entry: dict[str, Any],
        configs: dict[str, dict[str, Any]],
        homeassistant_scene_uids: set[str],
        slug_to_storage_id: dict[str, str],
    ) -> None:
        """Record one entity-registry scene entry into the registry-walk outputs."""
        ent_id = entry.get("entity_id") or ""
        uid = entry.get("unique_id")
        if not ent_id.startswith("scene.") or not uid:
            return
        if entry.get("platform") == "homeassistant":
            homeassistant_scene_uids.add(uid)
        slug = ent_id.removeprefix("scene.")
        if slug:
            slug_to_storage_id[slug] = uid
        if uid in configs and slug and slug != uid:
            configs[slug] = configs[uid]

    @staticmethod
    def _select_scene_ids_to_fetch(
        scored: list[tuple[str, str, str | None, int]],
        configs: dict[str, dict[str, Any]],
        homeassistant_scene_uids: set[str],
    ) -> tuple[list[str], int]:
        """Pick scene ids needing a per-id fetch, skipping integration-managed ones.

        Issue #1168 R3 blocker 2: integration-managed scenes 404 on the per-id
        REST endpoint by design, so surfacing those as fetch failures masks real
        errors. They are counted separately (returned as ``integration_skipped``).
        When the registry call failed (``homeassistant_scene_uids`` empty), fall
        back to attempting all scenes -- false partials beat dropping legitimate
        HA-managed scenes silently.

        Returns ``(sids_to_fetch, integration_skipped_count)``.
        """
        if not homeassistant_scene_uids:
            return [sid for _, _, sid, _ in scored if sid and sid not in configs], 0
        sids: list[str] = []
        integration_skipped = 0
        for _, _, sid, _ in scored:
            if not sid or sid in configs:
                continue
            if sid in homeassistant_scene_uids:
                sids.append(sid)
            else:
                integration_skipped += 1
        return sids, integration_skipped

    @staticmethod
    def _resolve_scene_storage_id(
        scene_config: dict[str, Any],
        scene_id: str | None,
        slug_to_storage_id: dict[str, str],
    ) -> str | None:
        """Resolve a scene's storage key (the contract used by ha_config_*_scene).

        Issue #1168 R6/R7 blockers 17/21: three-tier resolution:
          1. ``scene_config["id"]`` -- present whenever the bulk fetch carried it.
          2. ``slug_to_storage_id`` -- registry-derived; covers integration-
             managed scenes and any scene whose bulk record omitted ``id``.
          3. ``scene_id`` itself (the entity-id slug) -- final fallback when the
             registry walk also failed; surfaced via ``logger.warning`` so the
             silent-slug-mismatch path becomes observable.
        """
        config_id = scene_config.get("id") if isinstance(scene_config, dict) else None
        if isinstance(config_id, str):
            return config_id
        if scene_id in slug_to_storage_id:
            return slug_to_storage_id[scene_id]
        logger.warning(
            "ha_deep_search scene result fell back to entity-id slug for "
            "scene_id=%r -- neither bulk config nor registry walk produced a "
            "storage key. ``ha_config_get_scene`` will rely on its resolver "
            "remap to land on the right scene.",
            scene_id,
        )
        return scene_id

    async def _deep_search_scenes(
        self,
        all_entities: list[dict[str, Any]],
        query_lower: str,
        exact_match: bool,
    ) -> tuple[list[dict[str, Any]], int, int, int, bool]:
        """Deep-search scenes: 3-tier strategy plus registry-walk augmentation.

        Scenes have no listing primitive, so entities are enumerated from
        get_states() and configs fetched per id. Returns the scene results plus
        the four signals that drive the response ``partial`` flag:
        ``(results, failed_count, skipped_count, integration_skipped, registry_failed)``.
        """
        scene_entities = [
            e for e in all_entities if e.get("entity_id", "").startswith("scene.")
        ]

        # Phase 1: Score all scenes by name (instant)
        scored: list[tuple[str, str, str | None, int]] = []
        for entity in scene_entities:
            entity_id = entity.get("entity_id", "")
            friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)
            scene_id = entity_id.replace("scene.", "")
            name_score = self.fuzzy_searcher._calculate_entity_score(
                entity_id, friendly_name, "scene", query_lower
            )
            scored.append((entity_id, friendly_name, scene_id, name_score))

        # Phase 2: bulk fetch
        configs = await self._bulk_fetch_configs(
            "/config/scene/config",
            ["config/scene/config/list", "scene/config/list"],
            lambda item: (
                item.get("id") or item.get("name", "").lower().replace(" ", "_")
            ),
            INDIVIDUAL_CONFIG_TIMEOUT,
            "Scene",
        )
        bulk_fetched = configs is not None
        if configs is None:
            configs = {}

        # Phase 2.5: registry walk (runs unconditionally, mutates ``configs``,
        # and must precede Attempt C since the integration-skip filter depends
        # on its homeassistant_scene_uids output).
        (
            homeassistant_scene_uids,
            slug_to_storage_id,
            registry_failed,
        ) = await self._walk_scene_registry(configs)

        failed_count = 0
        skipped_count = 0
        integration_skipped = 0

        # Attempt C: parallel per-id fetch with a wall-clock budget so a few
        # slow scenes don't tank the whole search.
        if not bulk_fetched:
            sids_to_fetch, integration_skipped = self._select_scene_ids_to_fetch(
                scored, configs, homeassistant_scene_uids
            )

            async def _fetch_scene_config(
                sid: str,
            ) -> tuple[str, dict[str, Any] | None]:
                try:
                    config_resp = await asyncio.wait_for(
                        self.client.get_scene_config(sid),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    return (sid, config_resp.get("config", {}))
                except Exception as e:
                    logger.debug(f"Scene individual config fetch ({sid}) failed: {e}")
                    return (sid, None)

            (
                fetched_configs,
                failed_count,
                skipped_count,
            ) = await self._individual_fetch_budgeted(
                sids_to_fetch,
                _fetch_scene_config,
                SCENE_CONFIG_TIME_BUDGET,
                "Scene",
                "scenes",
            )
            configs.update(fetched_configs)

        # Phase 3: Score scenes, resolving each match's storage key
        scene_results: list[dict[str, Any]] = []
        for m in self._score_config_entries(scored, configs, query_lower, exact_match):
            scene_config = m["config"]
            scene_results.append(
                {
                    "entity_id": m["entity_id"],
                    "scene_id": self._resolve_scene_storage_id(
                        scene_config, m["key"], slug_to_storage_id
                    ),
                    "friendly_name": m["friendly_name"],
                    "score": m["score"],
                    "match_in_name": m["match_in_name"],
                    "match_in_config": m["match_in_config"],
                    "config": scene_config if scene_config else None,
                }
            )
        return (
            scene_results,
            failed_count,
            skipped_count,
            integration_skipped,
            registry_failed,
        )

    async def _search_helper_type(
        self,
        helper_type: str,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
    ) -> list[dict[str, Any]]:
        """Fetch one input_* helper type via WS list and return query matches."""
        async with semaphore:
            try:
                resp = await self.client.send_websocket_message(
                    {"type": f"{helper_type}/list"}
                )
                if not resp.get("success"):
                    return []

                matches: list[dict[str, Any]] = []
                for helper in resp.get("result", []):
                    helper_id = helper.get("id", "")
                    entity_id = f"{helper_type}.{helper_id}"
                    name = helper.get("name", helper_id)

                    name_match_score = self.fuzzy_searcher._calculate_entity_score(
                        entity_id, name, helper_type, query_lower
                    )
                    config_match_score = self._search_in_dict(
                        helper, query_lower, exact_match
                    )
                    total_score, threshold, match_in_name = self._score_deep_match(
                        entity_id,
                        name,
                        name_match_score,
                        config_match_score,
                        query_lower,
                        exact_match,
                    )

                    if total_score >= threshold:
                        matches.append(
                            {
                                "entity_id": entity_id,
                                "helper_type": helper_type,
                                "name": name,
                                "score": total_score,
                                "match_in_name": match_in_name,
                                "match_in_config": config_match_score >= threshold,
                                "config": helper,
                            }
                        )
                return matches
            except Exception as e:
                logger.debug(f"Could not list {helper_type}: {e}")
                return []

    async def _deep_search_helpers(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        include_config: bool,
    ) -> list[dict[str, Any]]:
        """Deep-search helpers: parallel input_* WS lists plus flow-based helpers."""
        helper_types = [
            "input_boolean",
            "input_number",
            "input_select",
            "input_text",
            "input_datetime",
            "input_button",
        ]

        results: list[dict[str, Any]] = []
        type_results = await asyncio.gather(
            *[
                self._search_helper_type(ht, query_lower, exact_match, semaphore)
                for ht in helper_types
            ],
            return_exceptions=True,
        )
        for result in type_results:
            if isinstance(result, list):
                results.extend(result)
            elif isinstance(result, Exception):
                logger.debug(f"Helper list fetch failed: {result}")

        # Flow-based helpers (template, group, utility_meter, derivative, ...)
        # are config entries, not storage records, and have no `<type>/list`
        # WebSocket endpoint. Pull them via the standard
        # /config/config_entries/entry REST surface and probe each entry's
        # options flow so the helper's current config is searchable alongside
        # the input_* helpers above.
        results.extend(
            await self._search_flow_helpers(
                query_lower,
                exact_match,
                semaphore,
                include_config=include_config,
            )
        )
        return results

    async def _search_one_dashboard(
        self,
        url_path: str,
        title: str,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
    ) -> list[dict[str, Any]]:
        """Search a single dashboard's config for the query."""
        async with semaphore:
            try:
                get_data: dict[str, Any] = {"type": "lovelace/config"}
                if url_path != "default":
                    get_data["url_path"] = url_path
                resp = await asyncio.wait_for(
                    self.client.send_websocket_message(get_data),
                    timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                )
                config = resp.get("result", resp) if isinstance(resp, dict) else resp
                if not isinstance(config, dict):
                    return []

                config_score = self._search_in_dict(config, query_lower, exact_match)
                threshold = 100 if exact_match else self.settings.fuzzy_threshold
                if config_score >= threshold:
                    return [
                        {
                            "dashboard_url": url_path,
                            "dashboard_title": title,
                            "score": config_score,
                            "match_in_config": True,
                            "config": config,
                        }
                    ]
                return []
            except Exception as e:
                logger.debug(f"Dashboard search failed ({url_path}): {e}")
                return []

    async def _deep_search_dashboards(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
    ) -> list[dict[str, Any]]:
        """Deep-search storage-mode dashboards plus the default dashboard.

        Re-raises on failure so dashboard errors bubble to deep_search's outer
        handler (this branch has no per-unit graceful degradation of its own).
        """
        try:
            dashboard_entries: list[dict[str, Any]] = (
                await fetch_dashboards_list(self.client) or []
            )

            dashboards_to_search: list[tuple[str, str]] = [
                ("default", "Default Dashboard")
            ]
            for dash in dashboard_entries:
                url_path = dash.get("url_path", "")
                title = dash.get("title", url_path)
                if url_path:
                    dashboards_to_search.append((url_path, title))

            dash_results = await asyncio.gather(
                *[
                    self._search_one_dashboard(
                        url_path, title, query_lower, exact_match, semaphore
                    )
                    for url_path, title in dashboards_to_search
                ],
                return_exceptions=True,
            )
            results: list[dict[str, Any]] = []
            for dash_result in dash_results:
                if isinstance(dash_result, list):
                    results.extend(dash_result)
                elif isinstance(dash_result, Exception):
                    logger.debug(f"Dashboard search failed: {dash_result}")
            return results

        except Exception as e:
            logger.error(f"Dashboard search error: {e}")
            raise

    def _paginate_and_build_response(
        self,
        results: dict[str, list[dict[str, Any]]],
        query: str,
        search_types: list[str],
        offset: int,
        limit: int,
        include_config: bool,
        scene_stats: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge per-type results, sort by score, paginate, and assemble the response."""
        tagged_results: list[tuple[str, dict[str, Any]]] = []
        for category, items in results.items():
            tagged_results.extend((category, item) for item in items)

        tagged_results.sort(key=lambda x: x[1]["score"], reverse=True)

        total_before_pagination = len(tagged_results)
        paginated = tagged_results[offset : offset + limit]

        # Re-group paginated results by category
        final_results: dict[str, list[dict[str, Any]]] = {
            "automations": [],
            "scripts": [],
            "scenes": [],
            "helpers": [],
            "dashboards": [],
        }
        for category, item in paginated:
            if not include_config:
                item.pop("config", None)
            final_results[category].append(item)

        has_more = (offset + len(paginated)) < total_before_pagination

        response: dict[str, Any] = {
            "success": True,
            "query": query,
            "total_matches": total_before_pagination,
            "offset": offset,
            "limit": limit,
            "count": len(paginated),
            "has_more": has_more,
            "next_offset": offset + limit if has_more else None,
            "automations": final_results["automations"],
            "scripts": final_results["scripts"],
            "scenes": final_results["scenes"],
            "helpers": final_results["helpers"],
            "search_types": search_types,
        }

        # Only include the dashboards key when dashboard search was requested.
        # ``scenes`` is in the default ``search_types`` so the bucket is
        # always-present alongside automations/scripts/helpers; gating it would
        # break test helpers that iterate the standard tuple.
        if "dashboard" in search_types:
            response["dashboards"] = final_results["dashboards"]

        self._apply_scene_partial_flag(response, scene_stats)
        return response

    @staticmethod
    def _apply_scene_partial_flag(
        response: dict[str, Any], scene_stats: dict[str, Any]
    ) -> None:
        """Set ``partial``/``partial_reason`` from the scene Attempt-C signals.

        Only set ``partial: True`` when something actually went wrong;
        downstream consumers treat absence as success. Issue #1168 R3 blocker 2:
        integration-managed scenes intentionally skip the per-id fetch and never
        raise ``partial`` on their own (the count is informational).
        """
        failed = scene_stats["failed"]
        skipped = scene_stats["skipped"]
        if not (failed or skipped):
            return
        response["partial"] = True
        reason_parts = [
            f"Scene config fetch incomplete: {failed} failed, "
            f"{skipped} skipped (time budget)."
        ]
        if scene_stats["integration_skipped"]:
            reason_parts.append(
                f" {scene_stats['integration_skipped']} integration-managed "
                "scenes are scored by attribute only (no per-id fetch)."
            )
        if scene_stats["registry_failed"]:
            # Issue #1168 R5 blocker 11: when the registry fetch errors, the
            # integration-platform filter is unavailable and Attempt C falls
            # back to attempting all scenes -- surface that so an elevated
            # failed_count isn't mistaken for a real config outage.
            reason_parts.append(
                " Entity-registry fetch failed; integration-platform filter "
                "unavailable, attempted all scenes (false-positive failures "
                "expected for integration-managed scenes)."
            )
        reason_parts.append(
            " Some scene matches may be missing config data; tune "
            "HAMCP_SCENE_CONFIG_TIME_BUDGET to raise the budget."
        )
        response["partial_reason"] = "".join(reason_parts)

    async def _search_flow_helpers(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        *,
        include_config: bool,
    ) -> list[dict[str, Any]]:
        """Search UI-created flow-based helpers (template, group, …).

        Flow-helpers live as config entries (not storage records) and have
        no ``<type>/list`` endpoint. Lists them via the standard config
        entries REST endpoint, then probes each entry's options flow so the
        helper's current config — template body, group members, source
        entity, etc. — is searchable.

        Cost: 1 REST call + one options-flow probe per flow-helper config
        entry, parallelised under ``semaphore``. The probe is skipped when
        the title alone already scores the maximum (a deeper config match can
        only raise the total, never lower it); any title that leaves headroom
        is still probed for accurate scoring and ``match_in_config``.
        """
        try:
            response = await self.client._request("GET", "/config/config_entries/entry")
        except Exception as exc:
            logger.debug(f"flow-helper search: list_entries failed: {exc}")
            return []

        if not isinstance(response, list):
            return []

        flow_entries = [e for e in response if self._is_flow_helper_entry(e)]
        if not flow_entries:
            return []

        scored = await asyncio.gather(
            *(
                self._score_flow_entry(
                    e, query_lower, exact_match, semaphore, include_config
                )
                for e in flow_entries
            ),
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        for item in scored:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, Exception):
                # The probe swallows its own transient/API errors, so anything
                # reaching here is a scoring/extraction bug (e.g. a shape
                # assumption breaking on a future HA version). Log at warning so
                # it's discoverable — one bad entry must not sink the whole
                # multi-source deep_search, so we drop it and keep going.
                logger.warning(f"flow-helper scoring failed: {item!r}")
        return out

    @staticmethod
    def _is_flow_helper_entry(entry: Any) -> bool:
        """Return True for an options-flow config entry of a flow-helper domain."""
        return (
            isinstance(entry, dict)
            and entry.get("domain") in FLOW_HELPER_TYPES
            and bool(entry.get("supports_options"))
        )

    async def _score_flow_entry(
        self,
        entry: dict[str, Any],
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        include_config: bool,
    ) -> dict[str, Any] | None:
        """Score one flow-helper config entry, probing its options flow as needed."""
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str):
            return None
        domain = entry.get("domain", "")
        title = entry.get("title") or entry_id

        # Score the name against a title-derived slug, never the opaque
        # config-entry ULID: a random ULID substring would otherwise produce
        # false-positive name matches (e.g. a 3-char query that happens to occur
        # inside the base32 id). The slug mirrors the storage-helper path, which
        # scores a name-derived id rather than an opaque key. entry_id is still
        # returned to the caller; it just isn't a search target.
        title_slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        title_pseudo_eid = f"{domain}.{title_slug}" if title_slug else domain
        name_score = self.fuzzy_searcher._calculate_entity_score(
            title_pseudo_eid, title, domain, query_lower
        )

        options: dict[str, Any] = {}
        # Only a perfect title match (score 100) makes the deeper options probe
        # redundant — the probe can only raise the total, never lower it, so
        # anything below 100 is worth probing (in both exact and fuzzy modes)
        # for accurate scoring and ``match_in_config``.
        need_probe = include_config or (
            self._score_deep_match(
                title_pseudo_eid, title, name_score, 0, query_lower, exact_match
            )[0]
            < 100
        )
        if need_probe:
            async with semaphore:
                options = await fetch_entry_options(self.client, entry_id, quiet=True)

        # Search the title, domain, and probed options — but not the opaque
        # entry_id (it would match random ULID substrings; it is returned in the
        # result for the caller regardless).
        haystack: dict[str, Any] = {
            "title": title,
            "domain": domain,
            "options": options,
        }
        config_score = self._search_in_dict(haystack, query_lower, exact_match)
        total_score, threshold, match_in_name = self._score_deep_match(
            title_pseudo_eid, title, name_score, config_score, query_lower, exact_match
        )
        if total_score < threshold:
            return None

        result: dict[str, Any] = {
            "entry_id": entry_id,
            "helper_type": domain,
            "name": title,
            "score": total_score,
            "match_in_name": match_in_name,
            "match_in_config": config_score >= threshold,
        }
        if include_config:
            result["config"] = options
        return result

    def _score_deep_match(
        self,
        entity_id: str,
        friendly_name: str,
        fuzzy_name_score: int,
        config_match_score: int,
        query_lower: str,
        exact_match: bool,
    ) -> tuple[int, int, bool]:
        """Compute total score, threshold, and match_in_name for a deep search result.

        Returns (total_score, threshold, match_in_name).
        """
        if exact_match:
            name_exact = (
                100
                if query_lower in entity_id.lower()
                or query_lower in friendly_name.lower()
                else 0
            )
            total_score = max(name_exact, config_match_score)
            return total_score, 100, name_exact >= 100
        else:
            total_score = max(fuzzy_name_score, config_match_score)
            threshold = self.settings.fuzzy_threshold
            return total_score, threshold, fuzzy_name_score >= threshold

    def _search_in_dict(
        self,
        data: dict[str, Any] | list[Any] | Any,
        query: str,
        exact_match: bool = False,
    ) -> int:
        """Search for query in nested dictionary/list structures.

        When exact_match is True, uses substring matching (returns 100 if found, 0 if not).
        When exact_match is False, collects all string leaves, tokenizes them into a
        single BM25 document, and scores against the query tokens.  Falls back to
        token-level SequenceMatcher if BM25 returns 0 (typo correction).
        """
        if exact_match:
            return self._search_in_dict_exact(data, query)

        # Fuzzy path: collect all string leaves, build a single tokenised document
        leaves: list[str] = []
        self._collect_string_leaves(data, leaves)
        if not leaves:
            return 0

        query_tokens = tokenize(query)
        if not query_tokens:
            return 0

        # Build a single flat token list from all leaves
        doc_tokens: list[str] = []
        for leaf in leaves:
            doc_tokens.extend(tokenize(leaf))

        if not doc_tokens:
            return 0

        # Use BM25 with a 1-document corpus (the config dict as a single doc)
        scorer = BM25Scorer()
        scorer.fit([doc_tokens])
        raw = scorer.score(query_tokens, 0)

        if raw > 0:
            # Normalise against the theoretical max (sum of IDF per query
            # token). With a 1-document corpus every token's IDF is identical
            # (~0.288 with smoothing), so the ratio effectively measures how
            # many query tokens the config contains. Cap at 100 for the edge
            # case where high TF pushes raw above the sum-of-IDFs baseline.
            max_possible = scorer.max_possible_score(query_tokens)
            if max_possible > 0:
                return min(100, round(raw / max_possible * 100))
            logger.warning(
                "BM25 scored > 0 but max_possible IDF is 0; "
                "query_tokens=%s, doc_tokens_len=%d",
                query_tokens,
                len(doc_tokens),
            )
            return 100

        # Tier-3 fallback: token-level SequenceMatcher for typos
        logger.debug(
            "BM25 returned 0 for query_tokens=%s; "
            "falling back to SequenceMatcher typo scoring over %d unique tokens",
            query_tokens,
            len(set(doc_tokens)),
        )
        best = 0
        for qt in query_tokens:
            for dt in set(doc_tokens):
                best = max(best, calculate_ratio(qt, dt))
        return best if best >= 70 else 0

    @staticmethod
    def _collect_string_leaves(
        data: dict[str, Any] | list[Any] | Any, out: list[str]
    ) -> None:
        """Recursively collect all string representations from nested data."""
        if isinstance(data, dict):
            for key, value in data.items():
                out.append(str(key))
                SmartSearchTools._collect_string_leaves(value, out)
        elif isinstance(data, list):
            for item in data:
                SmartSearchTools._collect_string_leaves(item, out)
        elif isinstance(data, str):
            out.append(data)
        elif data is not None:
            out.append(str(data))

    @classmethod
    def _search_in_dict_exact(
        cls,
        data: dict[str, Any] | list[Any] | Any,
        query: str,
    ) -> int:
        """Exact substring search in nested structures (returns 100 or 0)."""
        if isinstance(data, dict):
            return cls._exact_in_dict(data, query)
        if isinstance(data, list):
            return cls._exact_in_list(data, query)
        if isinstance(data, str):
            return 100 if query in data.lower() else 0
        if data is not None:
            return 100 if query in str(data).lower() else 0
        return 0

    @classmethod
    def _exact_in_dict(cls, data: dict[str, Any], query: str) -> int:
        """Exact-match scan over a dict's keys and recursively over its values."""
        for key, value in data.items():
            if query in str(key).lower():
                return 100
            if cls._search_in_dict_exact(value, query) >= 100:
                return 100
        return 0

    @classmethod
    def _exact_in_list(cls, data: list[Any], query: str) -> int:
        """Exact-match scan recursively over a list's items."""
        for item in data:
            if cls._search_in_dict_exact(item, query) >= 100:
                return 100
        return 0


def create_smart_search_tools(
    client: HomeAssistantClient | None = None,
) -> SmartSearchTools:
    """Create smart search tools instance."""
    return SmartSearchTools(client)
