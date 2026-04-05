"""
Smart search tools for Home Assistant MCP server.
"""

import asyncio
import logging
import os
import random
import time
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings
from ..utils.fuzzy_search import calculate_partial_ratio, create_fuzzy_searcher
from .helpers import exception_to_structured_error

logger = logging.getLogger(__name__)

# Default concurrency limit for parallel operations
DEFAULT_CONCURRENCY_LIMIT = 20

# Bulk fetch timeouts (in seconds)
BULK_REST_TIMEOUT = 5.0  # Timeout for bulk REST endpoint calls
BULK_WEBSOCKET_TIMEOUT = 3.0  # Timeout for bulk WebSocket calls
INDIVIDUAL_CONFIG_TIMEOUT = 5.0  # Timeout for individual config fetches

# Time budgets for fallback individual fetching (in seconds).
# Configurable via env vars for instances with many automations/scripts.
AUTOMATION_CONFIG_TIME_BUDGET = float(
    os.environ.get("HAMCP_AUTOMATION_CONFIG_TIME_BUDGET", "30")
)
SCRIPT_CONFIG_TIME_BUDGET = float(
    os.environ.get("HAMCP_SCRIPT_CONFIG_TIME_BUDGET", "20")
)

# Batch size for parallel individual config fetches (Tier 3 fallback)
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
    ) -> dict[str, Any]:
        """
        Advanced entity search with fuzzy matching and typo tolerance.

        Args:
            query: Search query (can be partial, with typos)
            limit: Maximum number of results
            offset: Number of results to skip for pagination
            include_attributes: Whether to include full entity attributes
            domain_filter: Optional domain to filter entities before search (e.g., "light", "sensor")

        Returns:
            Dictionary with search results and metadata
        """
        try:
            # Get all entities
            entities = await self.client.get_states()

            # Filter by domain BEFORE fuzzy search if domain_filter provided
            # This ensures fuzzy search only looks at entities in the target domain
            if domain_filter:
                entities = [
                    e
                    for e in entities
                    if e.get("entity_id", "").startswith(f"{domain_filter}.")
                ]

            # Perform fuzzy search - returns (paginated_results, total_count)
            matches, total_matches = self.fuzzy_searcher.search_entities(
                entities, query, limit, offset
            )

            # Format results
            results = []
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
                else:
                    # Include only essential attributes
                    attrs = match["attributes"]
                    essential_attrs = {}
                    for key in [
                        "unit_of_measurement",
                        "device_class",
                        "icon",
                        "area_id",
                    ]:
                        if key in attrs:
                            essential_attrs[key] = attrs[key]
                    result["essential_attributes"] = essential_attrs

                results.append(result)

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

            if not matches or (matches and matches[0]["score"] < 80):
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

    async def get_entities_by_area(
        self, area_query: str, group_by_domain: bool = True
    ) -> dict[str, Any]:
        """
        Get entities grouped by area/room using the HA registries for accurate area resolution.

        Uses entity registry, device registry, and area registry to determine
        which area each entity belongs to. Fuzzy matches the query against
        area names/IDs to find the target area(s).

        Args:
            area_query: Area/room name to search for
            group_by_domain: Whether to group results by domain within each area

        Returns:
            Dictionary with area-grouped entities
        """
        try:
            # Fetch all registries and states in parallel
            entities_task = self.client.get_states()
            area_registry_task = self.client.send_websocket_message(
                {"type": "config/area_registry/list"}
            )
            entity_registry_task = self.client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
            device_registry_task = self.client.send_websocket_message(
                {"type": "config/device_registry/list"}
            )

            results = await asyncio.gather(
                entities_task,
                area_registry_task,
                entity_registry_task,
                device_registry_task,
                return_exceptions=True,
            )

            entities = results[0] if not isinstance(results[0], Exception) else []

            # Parse area registry: area_id -> area info
            area_registry: dict[str, dict[str, Any]] = {}
            if isinstance(results[1], dict) and results[1].get("success"):
                for area in results[1].get("result", []):
                    area_id = area.get("area_id", "")
                    if area_id:
                        area_registry[area_id] = area

            # Parse entity registry: entity_id -> {area_id, device_id}
            entity_reg_map: dict[str, dict[str, str | None]] = {}
            if isinstance(results[2], dict) and results[2].get("success"):
                for entry in results[2].get("result", []):
                    entity_id = entry.get("entity_id")
                    if entity_id:
                        entity_reg_map[entity_id] = {
                            "area_id": entry.get("area_id"),
                            "device_id": entry.get("device_id"),
                        }

            # Parse device registry: device_id -> area_id
            device_area_map: dict[str, str | None] = {}
            if isinstance(results[3], dict) and results[3].get("success"):
                for device in results[3].get("result", []):
                    device_id = device.get("id", "")
                    if device_id:
                        device_area_map[device_id] = device.get("area_id")

            # Fuzzy match area_query against known area names and IDs
            area_query_lower = area_query.lower().strip()
            matched_area_ids: set[str] = set()

            for area_id, area_info in area_registry.items():
                area_name = area_info.get("name", "")
                # Exact match on area_id or name (case-insensitive)
                if (
                    area_query_lower == area_id.lower()
                    or area_query_lower == area_name.lower()
                ):
                    matched_area_ids.add(area_id)
                    continue
                # Fuzzy match on area name
                name_score = calculate_partial_ratio(
                    area_query_lower, area_name.lower()
                )
                id_score = calculate_partial_ratio(area_query_lower, area_id.lower())
                best_score = max(name_score, id_score)
                if best_score >= 80:
                    matched_area_ids.add(area_id)

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

            # Build entity_id -> resolved area_id mapping
            # Priority: entity direct area_id > device area_id
            entity_area_resolved: dict[str, str] = {}
            for entity_id, reg_info in entity_reg_map.items():
                area_id = reg_info.get("area_id")
                device_id = reg_info.get("device_id")
                if not area_id and device_id:
                    area_id = device_area_map.get(device_id)
                if area_id:
                    entity_area_resolved[entity_id] = area_id

            # Build state lookup for entity details
            state_map: dict[str, dict[str, Any]] = {}
            for entity in entities:
                eid = entity.get("entity_id", "")
                if eid:
                    state_map[eid] = entity

            # Collect entities belonging to matched areas
            formatted_areas: dict[str, dict[str, Any]] = {}
            total_entities = 0

            for area_id in matched_area_ids:
                area_info = area_registry.get(area_id, {})
                area_name = area_info.get("name", area_id)

                # Find all entities in this area
                area_entities = [
                    entity_id
                    for entity_id, resolved_area in entity_area_resolved.items()
                    if resolved_area == area_id
                ]

                area_data: dict[str, Any] = {
                    "area_name": area_name,
                    "area_id": area_id,
                    "entity_count": len(area_entities),
                    "entities": {},
                }

                if group_by_domain:
                    domains: dict[str, list[dict[str, Any]]] = {}
                    for entity_id in area_entities:
                        domain = entity_id.split(".")[0]
                        state_info = state_map.get(entity_id, {})
                        if domain not in domains:
                            domains[domain] = []
                        domains[domain].append(
                            {
                                "entity_id": entity_id,
                                "friendly_name": state_info.get("attributes", {}).get(
                                    "friendly_name", entity_id
                                ),
                                "state": state_info.get("state", "unknown"),
                            }
                        )
                    area_data["entities"] = domains
                else:
                    area_data["entities"] = [
                        {
                            "entity_id": entity_id,
                            "friendly_name": (
                                state_info := state_map.get(entity_id, {})
                            )
                            .get("attributes", {})
                            .get("friendly_name", entity_id),
                            "domain": entity_id.split(".")[0],
                            "state": state_info.get("state", "unknown"),
                        }
                        for entity_id in area_entities
                    ]

                formatted_areas[area_id] = area_data
                total_entities += len(area_entities)

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
            # Fetch all data in parallel for better performance
            # Using asyncio.gather with return_exceptions=True to handle failures gracefully
            entities_task = self.client.get_states()
            services_task = self.client.get_services()
            area_registry_task = self.client.send_websocket_message(
                {"type": "config/area_registry/list"}
            )
            entity_registry_task = self.client.send_websocket_message(
                {"type": "config/entity_registry/list"}
            )
            device_registry_task = self.client.send_websocket_message(
                {"type": "config/device_registry/list"}
            )

            results = await asyncio.gather(
                entities_task,
                services_task,
                area_registry_task,
                entity_registry_task,
                device_registry_task,
                return_exceptions=True,
            )

            # Entities are mandatory — surface connection/auth errors immediately.
            # Services failure is logged at warning (affects total count and service catalog).
            # Registry failures are logged at debug (area enrichment only).
            if isinstance(results[0], Exception):
                raise results[0]

            entities = results[0]
            partial_warnings: list[str] = []
            if isinstance(results[1], Exception):
                logger.warning(f"Could not fetch services: {results[1]}")
                partial_warnings.append(f"Services unavailable: {results[1]}")
                services = []
            else:
                services = results[1]

            # Handle area registry result
            area_registry: list[dict[str, Any]] = []
            if isinstance(results[2], Exception):
                logger.debug(f"Could not fetch area registry: {results[2]}")
            elif isinstance(results[2], dict) and results[2].get("success"):
                area_registry = results[2].get("result", [])

            # Handle entity registry result
            entity_registry: list[dict[str, Any]] = []
            if isinstance(results[3], Exception):
                logger.debug(f"Could not fetch entity registry: {results[3]}")
            elif isinstance(results[3], dict) and results[3].get("success"):
                entity_registry = results[3].get("result", [])

            # Handle device registry result
            device_area_map: dict[str, str | None] = {}
            if isinstance(results[4], Exception):
                logger.debug(f"Could not fetch device registry: {results[4]}")
            elif isinstance(results[4], dict) and results[4].get("success"):
                for device in results[4].get("result", []):
                    device_id = device.get("id", "")
                    if device_id:
                        device_area_map[device_id] = device.get("area_id")

            # Build entity_id -> area_id mapping from entity + device registries
            # Priority: entity direct area_id > device area_id
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

            # Determine defaults based on detail_level
            # max_entities_per_domain=0 means "uncap everything" (entities + states)
            uncap_all = max_entities_per_domain == 0
            if max_entities_per_domain is None:
                if detail_level == "minimal":
                    max_entities_per_domain = 10
                # standard and full: no default cap (None = all entities)
            if include_state is None:
                include_state = detail_level == "full"
            if include_entity_id is None:
                include_entity_id = detail_level == "full"

            # Pre-populate area_stats to include empty areas
            area_stats: dict[str, dict[str, Any]] = {}
            for area in area_registry:
                area_id = area.get("area_id", "")
                if area_id:
                    area_stats[area_id] = {
                        "name": area.get("name", area_id),
                        "count": 0,
                        "domains": {},
                    }

            # Normalize domains filter
            domains_filter_set: set[str] | None = None
            if domains_filter:
                domains_filter_set = {d.strip().lower() for d in domains_filter}

            # Count all domains before filtering (for system_summary)
            all_domains = {e["entity_id"].split(".")[0] for e in entities}

            # Analyze entities by domain
            domain_stats: dict[str, dict[str, Any]] = {}
            device_types: dict[str, int] = {}

            for entity in entities:
                entity_id = entity["entity_id"]
                domain = entity_id.split(".")[0]

                # Skip domains not in the filter
                if domains_filter_set and domain not in domains_filter_set:
                    continue

                attributes = entity.get("attributes", {})
                state = entity.get("state", "unknown")

                # Domain statistics
                if domain not in domain_stats:
                    domain_stats[domain] = {
                        "count": 0,
                        "states_summary": {},
                        "all_entities": [],  # Store all entities
                    }

                domain_stats[domain]["count"] += 1

                # State distribution
                if state not in domain_stats[domain]["states_summary"]:
                    domain_stats[domain]["states_summary"][state] = 0
                domain_stats[domain]["states_summary"][state] += 1

                # Store all entities (we'll filter later)
                entity_data = {
                    "friendly_name": attributes.get("friendly_name", entity_id),
                }
                if include_entity_id:
                    entity_data["entity_id"] = entity_id
                if include_state:
                    entity_data["state"] = state

                domain_stats[domain]["all_entities"].append(entity_data)

                # Area analysis - use entity + device registry mapping
                area_id = entity_area_map.get(entity_id)
                if area_id and area_id in area_stats:
                    area_stats[area_id]["count"] += 1
                    if domain not in area_stats[area_id]["domains"]:
                        area_stats[area_id]["domains"][domain] = 0
                    area_stats[area_id]["domains"][domain] += 1

                # Device type analysis
                device_class = attributes.get("device_class")
                if device_class:
                    if device_class not in device_types:
                        device_types[device_class] = 0
                    device_types[device_class] += 1

            # Sort domains by count
            sorted_domains = sorted(
                domain_stats.items(), key=lambda x: x[1]["count"], reverse=True
            )

            # Get top services - services is a list of domain objects
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
            else:
                # Fallback for unexpected format
                total_services = 0

            # Build AI insights
            ai_insights = {
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

            # Prepare domain stats with entity filtering and truncation info
            formatted_domain_stats = {}
            for domain, stats in sorted_domains:
                all_entities = stats["all_entities"]

                # Apply max_entities_per_domain limit
                if (
                    max_entities_per_domain
                    and len(all_entities) > max_entities_per_domain
                ):
                    # Random selection for minimal
                    if detail_level == "minimal":
                        selected_entities = random.sample(
                            all_entities, max_entities_per_domain
                        )
                    else:
                        # Take first N for other levels
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

            # Apply global entity pagination (limit/offset across all domains)
            # Default limit: None for minimal (already capped per-domain), 200 for standard/full
            effective_limit = limit
            if effective_limit is None and detail_level != "minimal":
                effective_limit = 200

            pagination_metadata: dict[str, Any] | None = None
            if effective_limit is not None:
                total_entity_count = sum(
                    len(ds["entities"]) for ds in formatted_domain_stats.values()
                )

                if offset == 0:
                    # Page 1: fair distribution — give each domain a minimum
                    # allocation so the LLM sees entities from every domain,
                    # then distribute the remaining budget proportionally.
                    min_per_domain = 3
                    num_domains = len(formatted_domain_stats)
                    reserved = min(min_per_domain * num_domains, effective_limit)
                    remaining_budget = effective_limit - reserved

                    entities_included = 0
                    for domain_data in formatted_domain_stats.values():
                        domain_entities = domain_data["entities"]
                        domain_len = len(domain_entities)
                        # Base allocation: min_per_domain or all if domain is smaller
                        base = min(min_per_domain, domain_len)
                        # Proportional share of remaining budget
                        if total_entity_count > 0 and remaining_budget > 0:
                            extra = int(
                                remaining_budget * domain_len / total_entity_count
                            )
                        else:
                            extra = 0
                        take = min(base + extra, domain_len)
                        if take < domain_len:
                            domain_data["entities"] = domain_entities[:take]
                            domain_data["truncated"] = True
                        entities_included += len(domain_data["entities"])
                else:
                    # Pages 2+: sequential skip/take across domains
                    entities_skipped = 0
                    entities_included = 0
                    for domain_data in formatted_domain_stats.values():
                        domain_entities = domain_data["entities"]
                        domain_len = len(domain_entities)

                        skip_from_domain = max(
                            0, min(domain_len, offset - entities_skipped)
                        )
                        budget_left = effective_limit - entities_included
                        take_from_domain = max(
                            0, min(domain_len - skip_from_domain, budget_left)
                        )

                        if skip_from_domain > 0 or take_from_domain < domain_len:
                            domain_data["entities"] = domain_entities[
                                skip_from_domain : skip_from_domain + take_from_domain
                            ]
                            if take_from_domain < domain_len:
                                domain_data["truncated"] = True

                        entities_skipped += skip_from_domain
                        entities_included += take_from_domain

                has_more = (offset + entities_included) < total_entity_count
                pagination_metadata = {
                    "total_entity_results": total_entity_count,
                    "offset": offset,
                    "limit": effective_limit,
                    "entities_returned": entities_included,
                    "has_more": has_more,
                    "next_offset": offset + effective_limit if has_more else None,
                }

            # Build base response — totals always reflect full system
            system_summary: dict[str, Any] = {
                "total_entities": len(entities),
                "total_domains": len(all_domains),
                "total_services": total_services,
                "total_areas": len(area_registry),
            }
            if domains_filter_set:
                system_summary["filtered_domains"] = sorted(domains_filter_set)

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

            # Add level-specific fields
            if detail_level == "full":
                # Full: Add device types and service catalog
                base_response["device_types"] = device_types
                base_response["service_availability"] = service_stats

            return base_response

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

    async def deep_search(
        self,
        query: str,
        search_types: list[str] | None = None,
        limit: int = 5,
        offset: int = 0,
        include_config: bool = False,
        concurrency_limit: int = DEFAULT_CONCURRENCY_LIMIT,
        exact_match: bool = True,
    ) -> dict[str, Any]:
        """
        Deep search across automation, script, helper, and dashboard definitions.

        Searches not just entity names but also within configuration definitions
        including triggers, actions, sequences, and other config fields.

        Args:
            query: Search query (can be partial, with typos when exact_match=False)
            search_types: Types to search (default: ["automation", "script", "helper"])
            limit: Maximum total results to return (default: 5)
            offset: Number of results to skip for pagination (default: 0)
            include_config: Include full config in results (default: False)
            concurrency_limit: Max concurrent API calls for config fetching
            exact_match: Use exact substring matching (default: True). Set False for fuzzy.

        Returns:
            Dictionary with search results grouped by type
        """
        if search_types is None:
            search_types = ["automation", "script", "helper"]

        try:
            results: dict[str, list[dict[str, Any]]] = {
                "automations": [],
                "scripts": [],
                "helpers": [],
                "dashboards": [],
            }

            query_lower = query.lower().strip()

            # Fetch all entities once at the beginning to avoid repeated calls
            all_entities = await self.client.get_states()

            # Pre-resolve unique_ids from cached entity states to avoid redundant API calls
            automation_unique_id_map = {}
            for e in all_entities:
                eid = e.get("entity_id", "")
                if eid.startswith("automation."):
                    uid = e.get("attributes", {}).get("id")
                    if uid:
                        automation_unique_id_map[eid] = uid

            # Create semaphore for limiting concurrent API calls
            semaphore = asyncio.Semaphore(concurrency_limit)

            # ================================================================
            # AUTOMATION SEARCH
            # Uses a 3-tier strategy to fetch configs within the MCP timeout:
            #   A) Try REST bulk endpoint (single call for all configs)
            #   B) Try WebSocket bulk endpoints
            #   C) Fall back to individual REST calls with a time budget,
            #      prioritizing automations that best match the query by name
            # ================================================================
            if "automation" in search_types:
                automation_entities = [
                    e
                    for e in all_entities
                    if e.get("entity_id", "").startswith("automation.")
                ]

                # Phase 1: Score ALL automations by name (instant, no API calls)
                name_scored: list[tuple[str, str, int, str | None]] = []
                for entity in automation_entities:
                    entity_id = entity.get("entity_id", "")
                    friendly_name = entity.get("attributes", {}).get(
                        "friendly_name", entity_id
                    )
                    name_score = self.fuzzy_searcher._calculate_entity_score(
                        entity_id, friendly_name, "automation", query_lower
                    )
                    unique_id = automation_unique_id_map.get(entity_id)
                    name_scored.append(
                        (entity_id, friendly_name, name_score, unique_id)
                    )

                # Phase 2: Try to bulk-fetch ALL automation configs with a single API call
                all_automation_configs: dict[str, dict[str, Any]] = {}
                bulk_fetched = False

                # Attempt A: REST bulk endpoint /config/automation/config (no ID)
                try:
                    resp = await asyncio.wait_for(
                        self.client._request("GET", "/config/automation/config"),
                        timeout=BULK_REST_TIMEOUT,
                    )
                    if isinstance(resp, list):
                        for item in resp:
                            uid = item.get("id")
                            if uid:
                                all_automation_configs[uid] = item
                        bulk_fetched = True
                except Exception as e:
                    logger.debug(f"Automation REST bulk fetch failed: {e}")

                # Attempt B: WebSocket bulk endpoints
                if not bulk_fetched:
                    for ws_type in [
                        "config/automation/config/list",
                        "automation/config/list",
                    ]:
                        if bulk_fetched:
                            break
                        try:
                            ws_resp = await asyncio.wait_for(
                                self.client.send_websocket_message({"type": ws_type}),
                                timeout=BULK_WEBSOCKET_TIMEOUT,
                            )
                            if isinstance(ws_resp, dict) and ws_resp.get("success"):
                                for item in ws_resp.get("result", []):
                                    uid = item.get("id")
                                    if uid:
                                        all_automation_configs[uid] = item
                                bulk_fetched = True
                        except Exception as e:
                            logger.debug(
                                f"Automation WebSocket bulk fetch ({ws_type}) failed: {e}"
                            )

                # Attempt C: Parallel individual REST calls with time budget (LAST RESORT)
                # Fetch ALL configs in parallel batches — don't prioritize by name score.
                # Name score is only used for result ranking, not fetch order, because
                # deep_search's purpose is to find matches INSIDE configs (conditions/actions),
                # not just by name. Prioritizing by name would skip the configs most likely
                # to contain non-obvious matches. See #879.
                if not bulk_fetched:
                    budget_start = time.perf_counter()
                    uids_to_fetch = [
                        uid
                        for _, _, _, uid in name_scored
                        if uid and uid not in all_automation_configs
                    ]
                    total_to_fetch = len(uids_to_fetch)
                    fetched_count = 0

                    async def _fetch_automation_config(uid: str) -> tuple[str, dict[str, Any] | None]:
                        try:
                            config = await asyncio.wait_for(
                                self.client._request(
                                    "GET", f"/config/automation/config/{uid}"
                                ),
                                timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                            )
                            return (uid, config)
                        except Exception as e:
                            logger.debug(
                                f"Automation individual config fetch ({uid}) failed: {e}"
                            )
                            return (uid, None)

                    for i in range(0, len(uids_to_fetch), INDIVIDUAL_FETCH_BATCH_SIZE):
                        if (
                            time.perf_counter() - budget_start
                            > AUTOMATION_CONFIG_TIME_BUDGET
                        ):
                            skipped = total_to_fetch - fetched_count
                            logger.warning(
                                f"Automation config fetch budget exhausted "
                                f"({AUTOMATION_CONFIG_TIME_BUDGET}s). "
                                f"Fetched {fetched_count}/{total_to_fetch}, "
                                f"skipped {skipped} automations."
                            )
                            break
                        batch = uids_to_fetch[i : i + INDIVIDUAL_FETCH_BATCH_SIZE]
                        batch_results = await asyncio.gather(
                            *[_fetch_automation_config(uid) for uid in batch],
                            return_exceptions=True,
                        )
                        for br in batch_results:
                            if isinstance(br, tuple) and br[1] is not None:
                                all_automation_configs[br[0]] = br[1]
                                fetched_count += 1

                # Phase 3: Score with whatever configs we have
                for entity_id, friendly_name, name_score, unique_id in name_scored:
                    config = (
                        all_automation_configs.get(unique_id, {}) if unique_id else {}
                    )
                    config_match_score = (
                        self._search_in_dict(config, query_lower, exact_match)
                        if config
                        else 0
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
                        results["automations"].append(
                            {
                                "entity_id": entity_id,
                                "friendly_name": friendly_name,
                                "score": total_score,
                                "match_in_name": match_in_name,
                                "match_in_config": config_match_score >= threshold,
                                "config": config if config else None,
                            }
                        )

            # ================================================================
            # SCRIPT SEARCH (same 3-tier strategy: REST bulk -> WS bulk -> individual)
            # ================================================================
            if "script" in search_types:
                script_entities = [
                    e
                    for e in all_entities
                    if e.get("entity_id", "").startswith("script.")
                ]

                # Phase 1: Score all scripts by name (instant)
                script_name_scored: list[tuple[str, str, str, int]] = []
                for entity in script_entities:
                    entity_id = entity.get("entity_id", "")
                    friendly_name = entity.get("attributes", {}).get(
                        "friendly_name", entity_id
                    )
                    script_id = entity_id.replace("script.", "")
                    name_score = self.fuzzy_searcher._calculate_entity_score(
                        entity_id, friendly_name, "script", query_lower
                    )
                    script_name_scored.append(
                        (entity_id, friendly_name, script_id, name_score)
                    )

                # Phase 2: Try bulk fetch for scripts
                all_script_configs: dict[str, dict[str, Any]] = {}
                script_bulk_fetched = False

                # Attempt A: REST bulk endpoint
                try:
                    resp = await asyncio.wait_for(
                        self.client._request("GET", "/config/script/config"),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    if isinstance(resp, list):
                        for item in resp:
                            sid = item.get("id") or item.get(
                                "alias", ""
                            ).lower().replace(" ", "_")
                            if sid:
                                all_script_configs[sid] = item
                        script_bulk_fetched = True
                except Exception as e:
                    logger.debug(f"Script REST bulk fetch failed: {e}")

                # Attempt B: WebSocket bulk endpoints
                if not script_bulk_fetched:
                    for ws_type in [
                        "config/script/config/list",
                        "script/config/list",
                    ]:
                        if script_bulk_fetched:
                            break
                        try:
                            ws_resp = await asyncio.wait_for(
                                self.client.send_websocket_message({"type": ws_type}),
                                timeout=BULK_WEBSOCKET_TIMEOUT,
                            )
                            if isinstance(ws_resp, dict) and ws_resp.get("success"):
                                for item in ws_resp.get("result", []):
                                    sid = item.get("id") or item.get(
                                        "alias", ""
                                    ).lower().replace(" ", "_")
                                    if sid:
                                        all_script_configs[sid] = item
                                script_bulk_fetched = True
                        except Exception as e:
                            logger.debug(
                                f"Script WebSocket bulk fetch ({ws_type}) failed: {e}"
                            )

                # Attempt C: Parallel individual fetch with budget (see #879)
                if not script_bulk_fetched:
                    budget_start = time.perf_counter()
                    sids_to_fetch = [
                        sid
                        for _, _, sid, _ in script_name_scored
                        if sid and sid not in all_script_configs
                    ]
                    total_to_fetch = len(sids_to_fetch)
                    fetched_count = 0

                    async def _fetch_script_config(sid: str) -> tuple[str, dict[str, Any] | None]:
                        try:
                            config_resp = await asyncio.wait_for(
                                self.client.get_script_config(sid),
                                timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                            )
                            return (sid, config_resp.get("config", {}))
                        except Exception as e:
                            logger.debug(
                                f"Script individual config fetch ({sid}) failed: {e}"
                            )
                            return (sid, None)

                    for i in range(0, len(sids_to_fetch), INDIVIDUAL_FETCH_BATCH_SIZE):
                        if (
                            time.perf_counter() - budget_start
                            > SCRIPT_CONFIG_TIME_BUDGET
                        ):
                            skipped = total_to_fetch - fetched_count
                            logger.warning(
                                f"Script config fetch budget exhausted "
                                f"({SCRIPT_CONFIG_TIME_BUDGET}s). "
                                f"Fetched {fetched_count}/{total_to_fetch}, "
                                f"skipped {skipped} scripts."
                            )
                            break
                        batch = sids_to_fetch[i : i + INDIVIDUAL_FETCH_BATCH_SIZE]
                        batch_results = await asyncio.gather(
                            *[_fetch_script_config(sid) for sid in batch],
                            return_exceptions=True,
                        )
                        for br in batch_results:
                            if isinstance(br, tuple) and br[1] is not None:
                                all_script_configs[br[0]] = br[1]
                                fetched_count += 1

                # Phase 3: Score scripts
                for (
                    entity_id,
                    friendly_name,
                    script_id,
                    name_score,
                ) in script_name_scored:
                    script_config = all_script_configs.get(script_id, {})
                    config_match_score = (
                        self._search_in_dict(script_config, query_lower, exact_match)
                        if script_config
                        else 0
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
                        results["scripts"].append(
                            {
                                "entity_id": entity_id,
                                "script_id": script_id,
                                "friendly_name": friendly_name,
                                "score": total_score,
                                "match_in_name": match_in_name,
                                "match_in_config": config_match_score >= threshold,
                                "config": script_config if script_config else None,
                            }
                        )

            # Search helpers with parallel WebSocket calls
            if "helper" in search_types:
                helper_types = [
                    "input_boolean",
                    "input_number",
                    "input_select",
                    "input_text",
                    "input_datetime",
                    "input_button",
                ]

                async def fetch_helper_list(helper_type: str) -> list[dict[str, Any]]:
                    """Fetch helper list for a specific type."""
                    async with semaphore:
                        try:
                            message = {"type": f"{helper_type}/list"}
                            helper_list_response = (
                                await self.client.send_websocket_message(message)
                            )

                            if not helper_list_response.get("success"):
                                return []

                            helper_results = []
                            helpers = helper_list_response.get("result", [])

                            for helper in helpers:
                                helper_id = helper.get("id", "")
                                entity_id = f"{helper_type}.{helper_id}"
                                name = helper.get("name", helper_id)

                                # Check if query matches in name or config
                                name_match_score = (
                                    self.fuzzy_searcher._calculate_entity_score(
                                        entity_id, name, helper_type, query_lower
                                    )
                                )
                                config_match_score = self._search_in_dict(
                                    helper, query_lower, exact_match
                                )
                                total_score, threshold, match_in_name = (
                                    self._score_deep_match(
                                        entity_id,
                                        name,
                                        name_match_score,
                                        config_match_score,
                                        query_lower,
                                        exact_match,
                                    )
                                )

                                if total_score >= threshold:
                                    helper_results.append(
                                        {
                                            "entity_id": entity_id,
                                            "helper_type": helper_type,
                                            "name": name,
                                            "score": total_score,
                                            "match_in_name": match_in_name,
                                            "match_in_config": config_match_score
                                            >= threshold,
                                            "config": helper,
                                        }
                                    )

                            return helper_results
                        except Exception as e:
                            logger.debug(f"Could not list {helper_type}: {e}")
                            return []

                # Fetch all helper types in parallel
                helper_type_results = await asyncio.gather(
                    *[fetch_helper_list(ht) for ht in helper_types],
                    return_exceptions=True,
                )

                # Flatten helper results
                for result in helper_type_results:
                    if isinstance(result, list):
                        results["helpers"].extend(result)
                    elif isinstance(result, Exception):
                        logger.debug(f"Helper list fetch failed: {result}")

            # ================================================================
            # DASHBOARD SEARCH
            # Fetches all storage-mode dashboards and the default dashboard,
            # then searches their configs (cards, badges, views) for the query.
            # ================================================================
            if "dashboard" in search_types:
                try:
                    # List all storage-mode dashboards
                    dash_list_resp = await self.client.send_websocket_message(
                        {"type": "lovelace/dashboards/list"}
                    )
                    dashboard_entries: list[dict[str, Any]] = []
                    if isinstance(dash_list_resp, dict) and dash_list_resp.get(
                        "success"
                    ):
                        dashboard_entries = dash_list_resp.get("result", [])

                    # Build list of dashboards to search (include default)
                    dashboards_to_search: list[tuple[str, str]] = [
                        ("default", "Default Dashboard")
                    ]
                    for dash in dashboard_entries:
                        url_path = dash.get("url_path", "")
                        title = dash.get("title", url_path)
                        if url_path:
                            dashboards_to_search.append((url_path, title))

                    async def search_dashboard(
                        url_path: str, title: str
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
                                config = (
                                    resp.get("result", resp)
                                    if isinstance(resp, dict)
                                    else resp
                                )
                                if not isinstance(config, dict):
                                    return []

                                # Search the entire dashboard config
                                config_score = self._search_in_dict(
                                    config, query_lower, exact_match
                                )
                                threshold = (
                                    100
                                    if exact_match
                                    else self.settings.fuzzy_threshold
                                )
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
                                logger.debug(
                                    f"Dashboard search failed ({url_path}): {e}"
                                )
                                return []

                    # Search all dashboards in parallel
                    dash_results = await asyncio.gather(
                        *[
                            search_dashboard(url_path, title)
                            for url_path, title in dashboards_to_search
                        ],
                        return_exceptions=True,
                    )
                    for dash_result in dash_results:
                        if isinstance(dash_result, list):
                            results["dashboards"].extend(dash_result)
                        elif isinstance(dash_result, Exception):
                            logger.debug(f"Dashboard search failed: {dash_result}")

                except Exception as e:
                    logger.error(f"Dashboard search error: {e}")
                    raise

            # Merge all results with their category, sort by score, and paginate
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
                "helpers": final_results["helpers"],
                "search_types": search_types,
            }

            # Only include dashboards key when dashboard search was requested
            if "dashboard" in search_types:
                response["dashboards"] = final_results["dashboards"]

            return response

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
        """
        Recursively search for query string in nested dictionary/list structures.

        When exact_match is True, uses substring matching (returns 100 if found, 0 if not).
        When exact_match is False, uses fuzzy matching with partial ratio scoring.
        """
        max_score = 0

        if isinstance(data, dict):
            for key, value in data.items():
                if exact_match:
                    if query in str(key).lower():
                        return 100
                else:
                    key_score = calculate_partial_ratio(query, str(key).lower())
                    max_score = max(max_score, key_score)

                value_score = self._search_in_dict(value, query, exact_match)
                max_score = max(max_score, value_score)
                if exact_match and max_score >= 100:
                    return 100

        elif isinstance(data, list):
            for item in data:
                item_score = self._search_in_dict(item, query, exact_match)
                max_score = max(max_score, item_score)
                if exact_match and max_score >= 100:
                    return 100

        elif isinstance(data, str):
            if exact_match:
                if query in data.lower():
                    return 100
            else:
                max_score = max(max_score, calculate_partial_ratio(query, data.lower()))

        elif data is not None:
            if exact_match:
                if query in str(data).lower():
                    return 100
            else:
                max_score = max(
                    max_score,
                    calculate_partial_ratio(query, str(data).lower()),
                )

        return max_score


def create_smart_search_tools(
    client: HomeAssistantClient | None = None,
) -> SmartSearchTools:
    """Create smart search tools instance."""
    return SmartSearchTools(client)
