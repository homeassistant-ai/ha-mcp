"""
Smart search tools for Home Assistant MCP server.
"""

import logging
from typing import Any

from ..client.rest_client import HomeAssistantClient
from ..config import get_global_settings
from ..utils.fuzzy_search import create_fuzzy_searcher

logger = logging.getLogger(__name__)


class SmartSearchTools:
    """Smart search tools with fuzzy matching and AI optimization."""

    def __init__(self, client: HomeAssistantClient | None = None):
        """Initialize with Home Assistant client."""
        self.settings = get_global_settings()
        self.client = client or HomeAssistantClient()
        self.fuzzy_searcher = create_fuzzy_searcher(
            threshold=self.settings.fuzzy_threshold
        )

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
        self, detail_level: str = "standard"
    ) -> dict[str, Any]:
        """
        Get AI-friendly system overview with intelligent categorization.

        Args:
            detail_level: Level of detail to return:
                - "minimal": Just counts and AI insights (~300 tokens)
                - "standard": Counts + controllable devices + top domains (~700 tokens) [DEFAULT]
                - "detailed": Full domain stats without service catalog (~5,500 tokens)
                - "full": Everything including service catalog (~8,800 tokens)

        Returns:
            System overview optimized for AI understanding at requested detail level
        """
        try:
            # Get all entities and services
            entities = await self.client.get_states()
            services = await self.client.get_services()

            # Analyze entities by domain
            domain_stats: dict[str, dict[str, Any]] = {}
            area_stats: dict[str, dict[str, Any]] = {}
            device_types: dict[str, int] = {}

            for entity in entities:
                entity_id = entity["entity_id"]
                domain = entity_id.split(".")[0]
                attributes = entity.get("attributes", {})

                # Domain statistics
                if domain not in domain_stats:
                    domain_stats[domain] = {
                        "count": 0,
                        "states": {},
                        "sample_entities": [],
                    }

                domain_stats[domain]["count"] += 1

                # State distribution
                state = entity.get("state", "unknown")
                if state not in domain_stats[domain]["states"]:
                    domain_stats[domain]["states"][state] = 0
                domain_stats[domain]["states"][state] += 1

                # Sample entities (first 3 per domain)
                if len(domain_stats[domain]["sample_entities"]) < 3:
                    domain_stats[domain]["sample_entities"].append(
                        {
                            "entity_id": entity_id,
                            "friendly_name": attributes.get("friendly_name", entity_id),
                            "state": state,
                        }
                    )

                # Area analysis
                area_id = attributes.get("area_id")
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

            # Domain counts (sorted by count)
            domain_counts = {
                domain: stats["count"]
                for domain, stats in sorted_domains
            }

            # Controllable devices summary
            controllable_devices = {
                k: {
                    "count": v["count"],
                    "states_summary": v["states"],
                }
                for k, v in domain_stats.items()
                if k in ["light", "switch", "climate", "media_player", "cover"]
            }

            # Return based on detail level
            if detail_level == "minimal":
                return {
                    "success": True,
                    "total_entities": len(entities),
                    "total_domains": len(domain_stats),
                    "total_areas": len(area_stats),
                    "domain_counts": domain_counts,
                    "controllable_domains": sorted(controllable_devices.keys()),
                    "ai_insights": ai_insights,
                }

            elif detail_level == "standard":
                return {
                    "success": True,
                    "system_summary": {
                        "total_entities": len(entities),
                        "total_domains": len(domain_stats),
                        "total_services": total_services,
                        "total_areas": len(area_stats),
                    },
                    "domain_counts": domain_counts,
                    "controllable_devices": controllable_devices,
                    "top_domains": [
                        {
                            "domain": domain,
                            "count": stats["count"],
                            "sample_entity": stats["sample_entities"][0]
                            if stats["sample_entities"]
                            else None,
                        }
                        for domain, stats in sorted_domains[:5]
                    ],
                    "ai_insights": ai_insights,
                }

            elif detail_level == "detailed":
                # Add sample entities back to controllable devices
                controllable_detailed = {
                    k: {
                        "count": v["count"],
                        "states_summary": v["states"],
                        "sample_entities": v["sample_entities"][:2],  # 2 samples
                    }
                    for k, v in domain_stats.items()
                    if k in ["light", "switch", "climate", "media_player", "cover"]
                }

                return {
                    "success": True,
                    "system_summary": {
                        "total_entities": len(entities),
                        "total_domains": len(domain_stats),
                        "total_services": total_services,
                        "total_areas": len(area_stats),
                    },
                    "domain_stats": {
                        domain: {
                            "count": stats["count"],
                            "states_summary": stats["states"],
                            "sample_entities": stats["sample_entities"][:2],  # 2 samples
                        }
                        for domain, stats in sorted_domains
                    },
                    "controllable_devices": controllable_detailed,
                    "area_analysis": area_stats,
                    "device_types": device_types,
                    "ai_insights": ai_insights,
                }

            else:  # "full"
                return {
                    "success": True,
                    "system_summary": {
                        "total_entities": len(entities),
                        "total_domains": len(domain_stats),
                        "total_services": total_services,
                        "total_areas": len(area_stats),
                    },
                    "domain_stats": domain_stats,  # Full stats with 3 samples
                    "area_analysis": area_stats,
                    "device_types": device_types,
                    "service_availability": service_stats,
                    "ai_insights": ai_insights,
                }

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


def create_smart_search_tools(
    client: HomeAssistantClient | None = None,
) -> SmartSearchTools:
    """Create smart search tools instance."""
    return SmartSearchTools(client)
