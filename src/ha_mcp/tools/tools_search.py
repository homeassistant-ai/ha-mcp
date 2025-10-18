"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

from typing import Annotated, Any, Literal, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import add_timezone_metadata


def register_search_tools(mcp, client, smart_tools, **kwargs):
    """Register search and discovery tools with the MCP server."""

    @mcp.tool
    @log_tool_usage
    async def ha_search_entities(
        query: str,
        domain_filter: str | None = None,
        area_filter: str | None = None,
        limit: int = 10,
        group_by_domain: bool = False,
    ) -> dict[str, Any]:
        """Comprehensive entity search with fuzzy matching, domain/area filtering, and optional grouping.

        BEST PRACTICE: Before performing searches or starting any task, call ha_get_overview() first to understand:
        - Smart home size and scale (total entities, domains, areas)
        - Language used in entity naming (French/English/mixed)
        - Available areas/rooms and their entity distribution
        - System capabilities (controllable devices, sensors, automations)

        Choose overview detail level based on task:
        - 'minimal': Quick orientation (10 entities per domain sample) - RECOMMENDED for searches
        - 'standard': Complete picture (all entities, friendly names only) - for comprehensive tasks
        - 'full': Maximum detail (includes states, device types, services) - for deep analysis

        This context helps tailor search strategies, understand naming conventions, and make informed decisions."""
        try:
            # If area_filter is provided, use area-based search
            if area_filter:
                area_result = await smart_tools.get_entities_by_area(
                    area_filter, group_by_domain=True
                )

                # If we also have a query, filter the area results
                if query and query.strip():
                    # Get all entities from all areas in the result
                    all_area_entities = []
                    if "areas" in area_result:
                        for area_data in area_result["areas"].values():
                            if "entities" in area_data:
                                if isinstance(
                                    area_data["entities"], dict
                                ):  # grouped by domain
                                    for domain_entities in area_data["entities"].values():
                                        all_area_entities.extend(domain_entities)
                                else:  # flat list
                                    all_area_entities.extend(area_data["entities"])

                    # Apply fuzzy search to area entities
                    from ..utils.fuzzy_search import create_fuzzy_searcher

                    fuzzy_searcher = create_fuzzy_searcher(threshold=80)

                    # Convert to format expected by fuzzy searcher
                    entities_for_search = []
                    for entity in all_area_entities:
                        entities_for_search.append(
                            {
                                "entity_id": entity.get("entity_id", ""),
                                "attributes": {
                                    "friendly_name": entity.get("friendly_name", "")
                                },
                                "state": entity.get("state", "unknown"),
                            }
                        )

                    matches = fuzzy_searcher.search_entities(
                        entities_for_search, query, limit
                    )

                    # Format matches similar to smart_entity_search
                    results = []
                    for match in matches:
                        results.append(
                            {
                                "entity_id": match["entity_id"],
                                "friendly_name": match["friendly_name"],
                                "domain": match["domain"],
                                "state": match["state"],
                                "score": match["score"],
                                "match_type": match["match_type"],
                                "area_filter": area_filter,
                            }
                        )

                    # Group by domain if requested
                    if group_by_domain:
                        by_domain: dict[str, list[dict[str, Any]]] = {}
                        for result in results:
                            domain = result["domain"]
                            if domain not in by_domain:
                                by_domain[domain] = []
                            by_domain[domain].append(result)

                        search_data = {
                            "success": True,
                            "query": query,
                            "area_filter": area_filter,
                            "total_matches": len(results),
                            "results": results,
                            "by_domain": by_domain,
                            "search_type": "area_filtered_query",
                        }
                        return await add_timezone_metadata(client, search_data)
                    else:
                        search_data = {
                            "success": True,
                            "query": query,
                            "area_filter": area_filter,
                            "total_matches": len(results),
                            "results": results,
                            "search_type": "area_filtered_query",
                        }
                        return await add_timezone_metadata(client, search_data)
                else:
                    # Just area filter, return area results with enhanced format
                    if "areas" in area_result and area_result["areas"]:
                        first_area = next(iter(area_result["areas"].values()))
                        by_domain = first_area.get("entities", {})

                        # Flatten for results while keeping by_domain structure
                        all_results = []
                        for domain, entities in by_domain.items():
                            for entity in entities:
                                entity["domain"] = domain
                                all_results.append(entity)

                        area_search_data = {
                            "success": True,
                            "area_filter": area_filter,
                            "total_matches": len(all_results),
                            "results": all_results,
                            "by_domain": by_domain,
                            "search_type": "area_only",
                            "area_name": first_area.get("area_name", area_filter),
                        }
                        return await add_timezone_metadata(client, area_search_data)
                    else:
                        empty_area_data = {
                            "success": True,
                            "area_filter": area_filter,
                            "total_matches": 0,
                            "results": [],
                            "by_domain": {},
                            "search_type": "area_only",
                            "message": f"No entities found in area: {area_filter}",
                        }
                        return await add_timezone_metadata(client, empty_area_data)

            # Regular entity search (no area filter)
            result = await smart_tools.smart_entity_search(query, limit)

            # Convert 'matches' to 'results' for backward compatibility
            if "matches" in result:
                result["results"] = result.pop("matches")

            # Apply domain filter if provided
            if domain_filter and "results" in result:
                filtered_results = [
                    r for r in result["results"] if r.get("domain") == domain_filter
                ]
                result["results"] = filtered_results
                result["total_matches"] = len(filtered_results)
                result["domain_filter"] = domain_filter

            # Group by domain if requested
            if group_by_domain and "results" in result:
                by_domain = {}
                for entity in result["results"]:
                    domain = entity.get("domain", entity["entity_id"].split(".")[0])
                    if domain not in by_domain:
                        by_domain[domain] = []
                    by_domain[domain].append(entity)
                result["by_domain"] = by_domain

            result["search_type"] = "fuzzy_search"
            return await add_timezone_metadata(client, result)

        except Exception as e:
            error_data = {
                "error": str(e),
                "query": query,
                "domain_filter": domain_filter,
                "area_filter": area_filter,
                "suggestions": [
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                    "Check area/domain filter spelling",
                ],
            }
            return await add_timezone_metadata(client, error_data)

    @mcp.tool
    @log_tool_usage
    async def ha_get_overview(
        detail_level: Annotated[
            Literal["minimal", "standard", "full"],
            Field(
                default="standard",
                description=(
                    "Level of detail - "
                    "'minimal': 10 random entities per domain (friendly_name only); "
                    "'standard': ALL entities per domain (friendly_name only, default); "
                    "'full': ALL entities with entity_id + friendly_name + state"
                ),
            ),
        ] = "standard",
        max_entities_per_domain: Annotated[
            int | None,
            Field(
                default=None,
                description="Override max entities per domain (None = all). Minimal defaults to 10.",
            ),
        ] = None,
        include_state: Annotated[
            bool | None,
            Field(
                default=None,
                description="Include state field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
        include_entity_id: Annotated[
            bool | None,
            Field(
                default=None,
                description="Include entity_id field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get AI-friendly system overview with intelligent categorization.

        Returns comprehensive system information at the requested detail level.
        Use 'standard' (default) for most queries. Optionally customize entity fields and limits.
        """
        result = await smart_tools.get_system_overview(
            detail_level, max_entities_per_domain, include_state, include_entity_id
        )
        return cast(dict[str, Any], result)

    @mcp.tool
    @log_tool_usage
    async def ha_deep_search(
        query: str,
        search_types: Annotated[
            list[str] | None,
            Field(
                default=None,
                description="Types to search in: 'automation', 'script', 'helper'. Default: all types",
            ),
        ] = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Deep search across automation, script, and helper definitions.

        Searches not only entity names but also within configuration definitions including
        triggers, actions, sequences, and other config fields. Perfect for finding automations
        that use specific services, helpers referenced in scripts, or tracking down where
        particular entities are being used.

        Args:
            query: Search query (can be partial, with typos)
            search_types: Types to search (default: ["automation", "script", "helper"])
            limit: Maximum total results to return (default: 20)

        Examples:
            - Find automations using a service: ha_deep_search("light.turn_on")
            - Find scripts with delays: ha_deep_search("delay")
            - Find helpers with specific options: ha_deep_search("option_a")
            - Search all types for an entity: ha_deep_search("sensor.temperature")
            - Search only automations: ha_deep_search("motion", search_types=["automation"])

        Returns detailed matches with:
            - match_in_name: True if query matched the entity name
            - match_in_config: True if query matched within the configuration
            - config: Full configuration for matched items
            - score: Match quality score (higher is better)
        """
        result = await smart_tools.deep_search(query, search_types, limit)
        return cast(dict[str, Any], result)

    @mcp.tool
    @log_tool_usage
    async def ha_get_state(entity_id: str) -> dict[str, Any]:
        """Get detailed state information for a Home Assistant entity with timezone metadata."""
        try:
            result = await client.get_entity_state(entity_id)
            return await add_timezone_metadata(client, result)
        except Exception as e:
            error_data = {
                "entity_id": entity_id,
                "error": str(e),
                "suggestions": [
                    f"Verify entity {entity_id} exists",
                    "Check Home Assistant connection",
                    "Try ha_search_entities() to find correct entity",
                ],
            }
            return await add_timezone_metadata(client, error_data)
