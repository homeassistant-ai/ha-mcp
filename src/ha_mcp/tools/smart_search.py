"""
Smart search tools for Home Assistant MCP server.
"""

import logging
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings
from ..utils.fuzzy_search import calculate_partial_ratio, create_fuzzy_searcher

logger = logging.getLogger(__name__)


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
        self, query: str, limit: int = 10, include_attributes: bool = False
    ) -> dict[str, Any]:
        """
        Advanced entity search with fuzzy matching and typo tolerance.

        Args:
            query: Search query (can be partial, with typos)
            limit: Maximum number of results
            include_attributes: Whether to include full entity attributes

        Returns:
            Dictionary with search results and metadata
        """
        try:
            # Get all entities
            entities = await self.client.get_states()

            # Perform fuzzy search
            matches = self.fuzzy_searcher.search_entities(entities, query, limit)

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

            return {
                "success": True,
                "query": query,
                "total_matches": len(matches),
                "matches": results,  # Changed from 'results' to 'matches' for consistency
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
        Get entities grouped by area/room with fuzzy matching.

        Args:
            area_query: Area/room name to search for
            group_by_domain: Whether to group results by domain within each area

        Returns:
            Dictionary with area-grouped entities
        """
        try:
            # Get all entities
            entities = await self.client.get_states()

            # Search by area
            area_matches = self.fuzzy_searcher.search_by_area(entities, area_query)

            # Format results
            formatted_areas = {}
            total_entities = 0

            for area_name, area_entities in area_matches.items():
                area_data = {
                    "area_name": area_name,
                    "entity_count": len(area_entities),
                    "entities": {},
                }

                if group_by_domain:
                    # Group by domain
                    domains: dict[str, list[dict[str, Any]]] = {}
                    for entity in area_entities:
                        domain = entity["entity_id"].split(".")[0]
                        if domain not in domains:
                            domains[domain] = []
                        domains[domain].append(
                            {
                                "entity_id": entity["entity_id"],
                                "friendly_name": entity.get("attributes", {}).get(
                                    "friendly_name", entity["entity_id"]
                                ),
                                "state": entity.get("state", "unknown"),
                            }
                        )
                    area_data["entities"] = domains
                else:
                    # Flat list
                    area_data["entities"] = [
                        {
                            "entity_id": entity["entity_id"],
                            "friendly_name": entity.get("attributes", {}).get(
                                "friendly_name", entity["entity_id"]
                            ),
                            "domain": entity["entity_id"].split(".")[0],
                            "state": entity.get("state", "unknown"),
                        }
                        for entity in area_entities
                    ]

                formatted_areas[area_name] = area_data
                total_entities += len(area_entities)

            return {
                "area_query": area_query,
                "total_areas_found": len(formatted_areas),
                "total_entities": total_entities,
                "areas": formatted_areas,
                "search_metadata": {
                    "grouped_by_domain": group_by_domain,
                    "area_inference_method": "fuzzy_name_matching",
                },
                "usage_tips": [
                    "Try room names: 'salon', 'chambre', 'cuisine'",
                    "English names: 'living', 'bedroom', 'kitchen'",
                    "Partial matches: 'bed' finds 'bedroom' entities",
                    "Use get_all_states to see all area_id attributes",
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
            # Get all entities and services
            entities = await self.client.get_states()
            services = await self.client.get_services()

            # Get area registry and entity registry for proper area mapping
            # Areas exist in the registry, not in entity state attributes
            area_registry: list[dict[str, Any]] = []
            entity_registry: list[dict[str, Any]] = []
            try:
                area_result = await self.client.send_websocket_message(
                    {"type": "config/area_registry/list"}
                )
                if area_result.get("success"):
                    area_registry = area_result.get("result", [])
            except Exception as e:
                logger.debug(f"Could not fetch area registry: {e}")

            try:
                entity_result = await self.client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
                if entity_result.get("success"):
                    entity_registry = entity_result.get("result", [])
            except Exception as e:
                logger.debug(f"Could not fetch entity registry: {e}")

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
                if max_entities_per_domain and len(all_entities) > max_entities_per_domain:
                    # Random selection for minimal
                    if detail_level == "minimal":
                        selected_entities = random.sample(all_entities, max_entities_per_domain)
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
    ) -> dict[str, Any]:
        """
        Deep search across automation, script, and helper definitions.

        Searches not just entity names but also within configuration definitions
        including triggers, actions, sequences, and other config fields.

        Args:
            query: Search query (can be partial, with typos)
            search_types: Types to search (default: ["automation", "script", "helper"])
            limit: Maximum total results to return

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

            # Search automations
            if "automation" in search_types:
                entities = await self.client.get_states()
                automation_entities = [
                    e for e in entities if e.get("entity_id", "").startswith("automation.")
                ]

                for entity in automation_entities:
                    entity_id = entity.get("entity_id", "")
                    friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)

                    # Check if query matches in name first
                    name_match_score = self.fuzzy_searcher._calculate_entity_score(
                        entity_id, friendly_name, "automation", query_lower
                    )

                    # Get automation config and search in definition
                    try:
                        config_response = await self.client.get_automation_config(entity_id)
                        config_match_score = self._search_in_dict(config_response, query_lower)

                        # Combined score
                        total_score = max(name_match_score, config_match_score)

                        if total_score >= self.settings.fuzzy_threshold:
                            results["automations"].append({
                                "entity_id": entity_id,
                                "friendly_name": friendly_name,
                                "score": total_score,
                                "match_in_name": name_match_score >= self.settings.fuzzy_threshold,
                                "match_in_config": config_match_score >= self.settings.fuzzy_threshold,
                                "config": config_response,
                            })
                    except Exception as e:
                        logger.debug(f"Could not get config for {entity_id}: {e}")
                        # Still include if name matches
                        if name_match_score >= self.settings.fuzzy_threshold:
                            results["automations"].append({
                                "entity_id": entity_id,
                                "friendly_name": friendly_name,
                                "score": name_match_score,
                                "match_in_name": True,
                                "match_in_config": False,
                            })

            # Search scripts
            if "script" in search_types:
                entities = await self.client.get_states()
                script_entities = [
                    e for e in entities if e.get("entity_id", "").startswith("script.")
                ]

                for entity in script_entities:
                    entity_id = entity.get("entity_id", "")
                    friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)
                    script_id = entity_id.replace("script.", "")

                    # Check if query matches in name
                    name_match_score = self.fuzzy_searcher._calculate_entity_score(
                        entity_id, friendly_name, "script", query_lower
                    )

                    # Get script config and search in definition
                    try:
                        config_response = await self.client.get_script_config(script_id)
                        script_config = config_response.get("config", {})
                        config_match_score = self._search_in_dict(script_config, query_lower)

                        # Combined score
                        total_score = max(name_match_score, config_match_score)

                        if total_score >= self.settings.fuzzy_threshold:
                            results["scripts"].append({
                                "entity_id": entity_id,
                                "script_id": script_id,
                                "friendly_name": friendly_name,
                                "score": total_score,
                                "match_in_name": name_match_score >= self.settings.fuzzy_threshold,
                                "match_in_config": config_match_score >= self.settings.fuzzy_threshold,
                                "config": script_config,
                            })
                    except Exception as e:
                        logger.debug(f"Could not get config for {script_id}: {e}")
                        # Still include if name matches
                        if name_match_score >= self.settings.fuzzy_threshold:
                            results["scripts"].append({
                                "entity_id": entity_id,
                                "script_id": script_id,
                                "friendly_name": friendly_name,
                                "score": name_match_score,
                                "match_in_name": True,
                                "match_in_config": False,
                            })

            # Search helpers
            if "helper" in search_types:
                helper_types = [
                    "input_boolean",
                    "input_number",
                    "input_select",
                    "input_text",
                    "input_datetime",
                    "input_button",
                ]

                for helper_type in helper_types:
                    try:
                        # Use WebSocket to list helpers
                        message = {"type": f"{helper_type}/list"}
                        helper_list_response = await self.client.send_websocket_message(message)

                        if not helper_list_response.get("success"):
                            continue

                        helpers = helper_list_response.get("result", [])

                        for helper in helpers:
                            helper_id = helper.get("id", "")
                            entity_id = f"{helper_type}.{helper_id}"
                            name = helper.get("name", helper_id)

                            # Check if query matches in name or config
                            name_match_score = self.fuzzy_searcher._calculate_entity_score(
                                entity_id, name, helper_type, query_lower
                            )
                            config_match_score = self._search_in_dict(helper, query_lower)

                            # Combined score
                            total_score = max(name_match_score, config_match_score)

                            if total_score >= self.settings.fuzzy_threshold:
                                results["helpers"].append({
                                    "entity_id": entity_id,
                                    "helper_type": helper_type,
                                    "name": name,
                                    "score": total_score,
                                    "match_in_name": name_match_score >= self.settings.fuzzy_threshold,
                                    "match_in_config": config_match_score >= self.settings.fuzzy_threshold,
                                    "config": helper,
                                })
                    except Exception as e:
                        logger.debug(f"Could not list {helper_type}: {e}")

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
                    "best_match_score": limited_results[0]["score"] if limited_results else 0,
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

    def _search_in_dict(self, data: dict[str, Any] | list[Any] | Any, query: str) -> int:
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
