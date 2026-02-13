"""
Smart search tools for Home Assistant MCP server.
"""

import asyncio
import logging
import time
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings
from ..utils.fuzzy_search import calculate_partial_ratio, create_fuzzy_searcher

logger = logging.getLogger(__name__)

# Default concurrency limit for parallel operations
DEFAULT_CONCURRENCY_LIMIT = 20

# Bulk fetch timeouts (in seconds)
BULK_REST_TIMEOUT = 5.0  # Timeout for bulk REST endpoint calls
BULK_WEBSOCKET_TIMEOUT = 3.0  # Timeout for bulk WebSocket calls
INDIVIDUAL_CONFIG_TIMEOUT = 5.0  # Timeout for individual config fetches

# Time budgets for fallback individual fetching (in seconds)
AUTOMATION_CONFIG_TIME_BUDGET = 15.0  # Max time for fetching automation configs individually
SCRIPT_CONFIG_TIME_BUDGET = 10.0  # Max time for fetching script configs individually


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
        include_attributes: bool = False,
        domain_filter: str | None = None,
    ) -> dict[str, Any]:
        """
        Advanced entity search with fuzzy matching and typo tolerance.

        Args:
            query: Search query (can be partial, with typos)
            limit: Maximum number of results
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

            # Perform fuzzy search - returns (limited_results, total_count)
            matches, total_matches = self.fuzzy_searcher.search_entities(
                entities, query, limit
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

            # Get suggestions if no good matches
            suggestions = []
            if not matches or (matches and matches[0]["score"] < 80):
                suggestions = self.fuzzy_searcher.get_smart_suggestions(entities, query)

            response = {
                "success": True,
                "query": query,
                "total_matches": total_matches,
                "matches": results,  # Changed from 'results' to 'matches' for consistency
                "is_truncated": total_matches > len(results),
                "search_metadata": {
                    "fuzzy_threshold": self.settings.fuzzy_threshold,
                    "best_match_score": matches[0]["score"] if matches else 0,
                    "search_suggestions": suggestions,
                },
                "usage_tips": [
                    "Try partial names: 'living' finds 'Living Room Light'",
                    "Domain search: 'light' finds all light entities",
                    "French/English: 'salon' or 'living' both work",
                    "Typo tolerant: 'lihgt' finds 'light' entities",
                ],
            }

            # Add hint if results are truncated
            if response["is_truncated"]:
                response["usage_tips"].insert(
                    0,
                    f"Showing {len(results)} of {total_matches} matches. Increase 'limit' parameter to see more.",
                )

            return response

        except Exception as e:
            logger.error(f"Error in smart_entity_search: {e}")
            return {
                "success": False,
                "query": query,
                "error": str(e),
                "matches": [],
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify entity exists with get_all_states",
                    "Try simpler search terms",
                ],
            }

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
                    "search_metadata": {
                        "grouped_by_domain": group_by_domain,
                        "area_inference_method": "registry_lookup",
                        "available_areas": [
                            {"area_id": aid, "name": ainfo.get("name", aid)}
                            for aid, ainfo in area_registry.items()
                        ],
                    },
                    "usage_tips": [
                        "No areas matched your query. See available_areas for valid area names.",
                        "Try exact area names from Home Assistant",
                        "Use ha_list_areas() to see all areas",
                    ],
                }

            # Build entity_id -> resolved area_id mapping
            # Priority: entity direct area_id > device area_id
            entity_area_resolved: dict[str, str] = {}
            for entity_id, reg_info in entity_reg_map.items():
                area_id = reg_info.get("area_id")
                if not area_id and reg_info.get("device_id"):
                    area_id = device_area_map.get(reg_info["device_id"])
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
                "search_metadata": {
                    "grouped_by_domain": group_by_domain,
                    "area_inference_method": "registry_lookup",
                },
                "usage_tips": [
                    "Try room names: 'salon', 'chambre', 'cuisine'",
                    "English names: 'living', 'bedroom', 'kitchen'",
                    "Use ha_list_areas() to see all available areas",
                ],
            }

        except Exception as e:
            logger.error(f"Error in get_entities_by_area: {e}")
            return {
                "area_query": area_query,
                "error": str(e),
                "suggestions": [
                    "Check Home Assistant connection",
                    "Try common room names: salon, chambre, cuisine",
                    "Use smart_entity_search to find entities first",
                ],
            }

    async def get_system_overview(
        self,
        detail_level: str = "standard",
        max_entities_per_domain: int | None = None,
        include_state: bool | None = None,
        include_entity_id: bool | None = None,
    ) -> dict[str, Any]:
        """
        Get AI-friendly system overview with intelligent categorization.

        Args:
            detail_level: Level of detail to return:
                - "minimal": 10 random entities per domain (friendly_name only)
                - "standard": ALL entities per domain (friendly_name only) [DEFAULT]
                - "full": ALL entities with full details (entity_id, friendly_name, state)
            max_entities_per_domain: Override max entities per domain (None = all)
            include_state: Override whether to include state field
            include_entity_id: Override whether to include entity_id field

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

            results = await asyncio.gather(
                entities_task,
                services_task,
                area_registry_task,
                entity_registry_task,
                return_exceptions=True,
            )

            # Process results, handling any exceptions gracefully
            entities = results[0] if not isinstance(results[0], Exception) else []
            services = results[1] if not isinstance(results[1], Exception) else []

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

            # Build entity_id -> area_id mapping from entity registry
            entity_area_map: dict[str, str | None] = {}
            for entry in entity_registry:
                entity_id = entry.get("entity_id")
                area_id = entry.get("area_id")
                if entity_id:
                    entity_area_map[entity_id] = area_id

            # Determine defaults based on detail_level
            if max_entities_per_domain is None:
                max_entities_per_domain = 10 if detail_level == "minimal" else None
            if include_state is None:
                include_state = detail_level == "full"
            if include_entity_id is None:
                include_entity_id = detail_level == "full"

            # Analyze entities by domain
            domain_stats: dict[str, dict[str, Any]] = {}
            area_stats: dict[str, dict[str, Any]] = {}
            device_types: dict[str, int] = {}

            for entity in entities:
                entity_id = entity["entity_id"]
                domain = entity_id.split(".")[0]
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

                # Area analysis - use entity registry mapping, not state attributes
                area_id = entity_area_map.get(entity_id)
                if area_id:
                    if area_id not in area_stats:
                        area_stats[area_id] = {"count": 0, "domains": {}}
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
                    for domain in domain_stats.keys()
                    if domain in ["light", "switch", "climate", "media_player", "cover"]
                ],
                "monitoring_sensors": [
                    domain
                    for domain in domain_stats.keys()
                    if domain in ["sensor", "binary_sensor", "camera"]
                ],
                "automation_ready": "automation" in domain_stats
                and domain_stats["automation"]["count"] > 0,
            }

            # Prepare domain stats with entity filtering and truncation info
            import random

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
                    "states_summary": stats["states_summary"],
                    "entities": selected_entities,
                    "truncated": truncated,
                }

            # Build base response
            base_response = {
                "success": True,
                "system_summary": {
                    "total_entities": len(entities),
                    "total_domains": len(domain_stats),
                    "total_services": total_services,
                    "total_areas": len(area_registry),
                },
                "domain_stats": formatted_domain_stats,
                "area_analysis": area_stats,  # Now included in all detail levels
                "ai_insights": ai_insights,
            }

            # Add level-specific fields
            if detail_level == "full":
                # Full: Add device types and service catalog
                base_response["device_types"] = device_types
                base_response["service_availability"] = service_stats

            return base_response

        except Exception as e:
            logger.error(f"Error in get_system_overview: {e}")
            return {
                "success": False,
                "error": str(e),
                "total_entities": 0,
                "entity_summary": {},
                "controllable_devices": {},
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify API token permissions",
                    "Try test_connection first",
                ],
            }

    async def deep_search(
        self,
        query: str,
        search_types: list[str] | None = None,
        limit: int = 20,
        concurrency_limit: int = DEFAULT_CONCURRENCY_LIMIT,
    ) -> dict[str, Any]:
        """
        Deep search across automation, script, and helper definitions.

        Searches not just entity names but also within configuration definitions
        including triggers, actions, sequences, and other config fields.

        Args:
            query: Search query (can be partial, with typos)
            search_types: Types to search (default: ["automation", "script", "helper"])
            limit: Maximum total results to return
            concurrency_limit: Max concurrent API calls for config fetching (default: 5)

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
                            logger.debug(f"Automation WebSocket bulk fetch ({ws_type}) failed: {e}")

                # Attempt C: Individual REST calls with time budget (LAST RESORT)
                # Prioritize name-matched automations so we at least get their configs
                if not bulk_fetched:
                    budget_start = time.perf_counter()
                    sorted_by_score = sorted(
                        name_scored, key=lambda x: x[2], reverse=True
                    )

                    for (
                        entity_id,
                        friendly_name,
                        name_score,
                        unique_id,
                    ) in sorted_by_score:
                        if time.perf_counter() - budget_start > AUTOMATION_CONFIG_TIME_BUDGET:
                            break
                        if not unique_id or unique_id in all_automation_configs:
                            continue
                        try:
                            config = await asyncio.wait_for(
                                self.client._request(
                                    "GET", f"/config/automation/config/{unique_id}"
                                ),
                                timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                            )
                            all_automation_configs[unique_id] = config
                        except Exception as e:
                            logger.debug(f"Automation individual config fetch ({unique_id}) failed: {e}")

                # Phase 3: Score with whatever configs we have
                for entity_id, friendly_name, name_score, unique_id in name_scored:
                    config = (
                        all_automation_configs.get(unique_id, {}) if unique_id else {}
                    )
                    config_match_score = (
                        self._search_in_dict(config, query_lower) if config else 0
                    )
                    total_score = max(name_score, config_match_score)

                    if total_score >= self.settings.fuzzy_threshold:
                        results["automations"].append(
                            {
                                "entity_id": entity_id,
                                "friendly_name": friendly_name,
                                "score": total_score,
                                "match_in_name": name_score
                                >= self.settings.fuzzy_threshold,
                                "match_in_config": config_match_score
                                >= self.settings.fuzzy_threshold,
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
                            logger.debug(f"Script WebSocket bulk fetch ({ws_type}) failed: {e}")

                # Attempt C: Individual fetch with budget
                if not script_bulk_fetched:
                    budget_start = time.perf_counter()
                    sorted_scripts = sorted(
                        script_name_scored, key=lambda x: x[3], reverse=True
                    )
                    for (
                        entity_id,
                        friendly_name,
                        script_id,
                        name_score,
                    ) in sorted_scripts:
                        if time.perf_counter() - budget_start > SCRIPT_CONFIG_TIME_BUDGET:
                            break
                        if script_id in all_script_configs:
                            continue
                        try:
                            config_resp = await asyncio.wait_for(
                                self.client.get_script_config(script_id),
                                timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                            )
                            all_script_configs[script_id] = config_resp.get(
                                "config", {}
                            )
                        except Exception as e:
                            logger.debug(f"Script individual config fetch ({script_id}) failed: {e}")

                # Phase 3: Score scripts
                for (
                    entity_id,
                    friendly_name,
                    script_id,
                    name_score,
                ) in script_name_scored:
                    script_config = all_script_configs.get(script_id, {})
                    config_match_score = (
                        self._search_in_dict(script_config, query_lower)
                        if script_config
                        else 0
                    )
                    total_score = max(name_score, config_match_score)

                    if total_score >= self.settings.fuzzy_threshold:
                        results["scripts"].append(
                            {
                                "entity_id": entity_id,
                                "script_id": script_id,
                                "friendly_name": friendly_name,
                                "score": total_score,
                                "match_in_name": name_score
                                >= self.settings.fuzzy_threshold,
                                "match_in_config": config_match_score
                                >= self.settings.fuzzy_threshold,
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
                                    helper, query_lower
                                )

                                total_score = max(name_match_score, config_match_score)

                                if total_score >= self.settings.fuzzy_threshold:
                                    helper_results.append(
                                        {
                                            "entity_id": entity_id,
                                            "helper_type": helper_type,
                                            "name": name,
                                            "score": total_score,
                                            "match_in_name": name_match_score
                                            >= self.settings.fuzzy_threshold,
                                            "match_in_config": config_match_score
                                            >= self.settings.fuzzy_threshold,
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

            # Sort all results by score and apply limit
            all_results = []
            for result_type, items in results.items():
                for item in items:
                    item["result_type"] = result_type.rstrip("s")  # singular form
                    all_results.append(item)

            all_results.sort(key=lambda x: x["score"], reverse=True)
            limited_results = all_results[:limit]

            # Re-group by type
            final_results: dict[str, list[dict[str, Any]]] = {
                "automations": [],
                "scripts": [],
                "helpers": [],
            }
            for item in limited_results:
                result_type = item.pop("result_type")
                final_results[f"{result_type}s"].append(item)

            total_matches = len(limited_results)

            # Return automations/scripts/helpers at top level for easy access
            return {
                "success": True,
                "query": query,
                "total_matches": total_matches,
                "automations": final_results["automations"],
                "scripts": final_results["scripts"],
                "helpers": final_results["helpers"],
                "search_types": search_types,
                "search_metadata": {
                    "fuzzy_threshold": self.settings.fuzzy_threshold,
                    "best_match_score": limited_results[0]["score"]
                    if limited_results
                    else 0,
                    "truncated": len(all_results) > limit,
                },
                "usage_tips": [
                    "Deep search finds matches in automation triggers, actions, and conditions",
                    "Script sequences and service calls are also searched",
                    "Helper configurations including options and constraints are included",
                    "Use match_in_name and match_in_config to understand where the match occurred",
                ],
            }

        except Exception as e:
            logger.error(f"Error in deep_search: {e}")
            return {
                "success": False,
                "query": query,
                "error": str(e),
                "automations": [],
                "scripts": [],
                "helpers": [],
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify automation/script/helper entities exist",
                    "Try simpler search terms",
                ],
            }

    def _search_in_dict(
        self, data: dict[str, Any] | list[Any] | Any, query: str
    ) -> int:
        """
        Recursively search for query string in nested dictionary/list structures.

        Returns a fuzzy match score based on how well the query matches values in the data.
        """
        max_score = 0

        if isinstance(data, dict):
            for key, value in data.items():
                # Score the key itself
                key_score = calculate_partial_ratio(query, str(key).lower())
                max_score = max(max_score, key_score)

                # Recursively score the value
                value_score = self._search_in_dict(value, query)
                max_score = max(max_score, value_score)

        elif isinstance(data, list):
            for item in data:
                item_score = self._search_in_dict(item, query)
                max_score = max(max_score, item_score)

        elif isinstance(data, str):
            # Direct fuzzy match on string values
            max_score = max(max_score, calculate_partial_ratio(query, data.lower()))

        elif data is not None:
            # Convert to string and match
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
