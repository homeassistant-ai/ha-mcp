"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, cast

from pydantic import Field

from ..errors import create_entity_not_found_error, create_validation_error
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import (
    add_timezone_metadata,
    coerce_bool_param,
    coerce_int_param,
    parse_string_list_param,
)

logger = logging.getLogger(__name__)


def _build_pagination_metadata(
    total_matches: int, offset: int, limit: int, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build standardized pagination metadata for search responses."""
    has_more = (offset + len(results)) < total_matches
    return {
        "total_matches": total_matches,
        "offset": offset,
        "limit": limit,
        "count": len(results),
        "has_more": has_more,
        "next_offset": offset + limit if has_more else None,
    }


async def _exact_match_search(
    client: Any, query: str, domain_filter: str | None, limit: int, offset: int = 0
) -> dict[str, Any]:
    """
    Fallback exact match search when fuzzy search fails.

    Performs simple substring matching on entity_id and friendly_name.
    """
    all_entities = await client.get_states()
    query_lower = query.lower().strip()

    results = []
    for entity in all_entities:
        entity_id = entity.get("entity_id", "")
        attributes = entity.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        # Apply domain filter if provided
        if domain_filter and domain != domain_filter:
            continue

        # Check for exact substring match in entity_id or friendly_name
        if query_lower in entity_id.lower() or query_lower in friendly_name.lower():
            is_exact = query_lower == entity_id.lower() or query_lower == friendly_name.lower()
            results.append(
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "domain": domain,
                    "state": entity.get("state", "unknown"),
                    "score": 100
                    if is_exact
                    else 80,
                    "match_type": "exact_match",
                }
            )

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    paginated = results[offset:offset + limit]
    return {
        "success": True,
        "query": query,
        **_build_pagination_metadata(len(results), offset, limit, paginated),
        "results": paginated,
        "search_type": "exact_match",
    }


async def _partial_results_search(
    client: Any, query: str, domain_filter: str | None, limit: int, offset: int = 0
) -> dict[str, Any]:
    """
    Last resort fallback - return any entities that might be relevant.

    Returns entities from the specified domain (if any) or a sample of all entities.
    """
    all_entities = await client.get_states()

    results = []
    for entity in all_entities:
        entity_id = entity.get("entity_id", "")
        attributes = entity.get("attributes", {})
        friendly_name = attributes.get("friendly_name", entity_id)
        domain = entity_id.split(".")[0] if "." in entity_id else ""

        # Apply domain filter if provided
        if domain_filter and domain != domain_filter:
            continue

        results.append(
            {
                "entity_id": entity_id,
                "friendly_name": friendly_name,
                "domain": domain,
                "state": entity.get("state", "unknown"),
                "score": 0,
                "match_type": "partial_listing",
            }
        )

    paginated = results[offset:offset + limit]
    return {
        "success": True,
        "partial": True,
        "query": query,
        **_build_pagination_metadata(len(results), offset, limit, paginated),
        "results": paginated,
        "search_type": "partial_listing",
    }


def register_search_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register search and discovery tools with the MCP server."""
    smart_tools = kwargs.get("smart_tools")
    if not smart_tools:
        raise ValueError("smart_tools is required for search tools registration")

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["search"],
            "title": "Search Entities",
        }
    )
    @log_tool_usage
    async def ha_search_entities(
        query: str,
        domain_filter: str | None = None,
        area_filter: str | None = None,
        limit: int = 10,
        offset: Annotated[
            int | str,
            Field(default=0, description="Number of results to skip for pagination (default: 0)"),
        ] = 0,
        group_by_domain: bool | str = False,
    ) -> dict[str, Any]:
        """Comprehensive entity search with fuzzy matching, domain/area filtering, and optional grouping.

        **Listing Entities by Domain:**
        Use domain_filter with an empty query to list all entities of a specific type:
        - ha_search_entities(query="", domain_filter="calendar") - List all calendars
        - ha_search_entities(query="", domain_filter="todo") - List all todo lists
        - ha_search_entities(query="", domain_filter="scene") - List all scenes
        - ha_search_entities(query="", domain_filter="zone") - List all zones (as entities)

        **BEST PRACTICE:** Before performing searches, call ha_get_overview() first to understand:
        - Smart home size and scale (total entities, domains, areas)
        - Language used in entity naming (French/English/mixed)
        - Available areas/rooms and their entity distribution

        Choose overview detail level based on task:
        - 'minimal': Quick orientation (10 entities per domain sample) - RECOMMENDED for searches
        - 'standard': Complete picture (all entities, friendly names only) - for comprehensive tasks
        - 'full': Maximum detail (includes states, device types, services) - for deep analysis"""
        # Coerce boolean parameter that may come as string from XML-style calls
        group_by_domain_bool = (
            coerce_bool_param(group_by_domain, "group_by_domain", default=False)
            or False
        )

        try:
            offset = coerce_int_param(offset, "offset", default=0, min_value=0) or 0

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
                                    for domain_entities in area_data[
                                        "entities"
                                    ].values():
                                        all_area_entities.extend(domain_entities)
                                else:  # flat list
                                    all_area_entities.extend(area_data["entities"])

                    # Apply fuzzy search to area entities
                    from ..utils.fuzzy_search import create_fuzzy_searcher

                    fuzzy_searcher = create_fuzzy_searcher(threshold=80)

                    # Convert to format expected by fuzzy searcher
                    entities_for_search = [
                        {
                            "entity_id": entity.get("entity_id", ""),
                            "attributes": {
                                "friendly_name": entity.get("friendly_name", "")
                            },
                            "state": entity.get("state", "unknown"),
                        }
                        for entity in all_area_entities
                    ]

                    matches, total_matches = fuzzy_searcher.search_entities(
                        entities_for_search, query, limit, offset
                    )

                    # Format matches similar to smart_entity_search
                    results = [
                        {
                            "entity_id": match["entity_id"],
                            "friendly_name": match["friendly_name"],
                            "domain": match["domain"],
                            "state": match["state"],
                            "score": match["score"],
                            "match_type": match["match_type"],
                            "area_filter": area_filter,
                        }
                        for match in matches
                    ]

                    pagination = _build_pagination_metadata(total_matches, offset, limit, results)

                    search_data: dict[str, Any] = {
                        "success": True,
                        "query": query,
                        "area_filter": area_filter,
                        **pagination,
                        "results": results,
                        "search_type": "area_filtered_query",
                    }

                    if group_by_domain_bool:
                        by_domain: dict[str, list[dict[str, Any]]] = {}
                        for item in results:
                            domain = item["domain"]
                            if domain not in by_domain:
                                by_domain[domain] = []
                            by_domain[domain].append(item)
                        search_data["by_domain"] = by_domain

                    return await add_timezone_metadata(client, search_data)
                else:
                    # Just area filter, return area results with enhanced format
                    if area_result.get("areas"):
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
            # Handle empty query with domain_filter - list all entities of that domain
            if domain_filter and (not query or not query.strip()):
                # Get all entities directly from the client
                all_entities = await client.get_states()

                # Filter by domain
                filtered_entities = [
                    e
                    for e in all_entities
                    if e.get("entity_id", "").startswith(f"{domain_filter}.")
                ]

                # Format results to match fuzzy search output
                paginated_entities = filtered_entities[offset:offset + limit]
                results = []
                for entity in paginated_entities:
                    entity_id = entity.get("entity_id", "")
                    attributes = entity.get("attributes", {})
                    results.append(
                        {
                            "entity_id": entity_id,
                            "friendly_name": attributes.get("friendly_name", entity_id),
                            "domain": domain_filter,
                            "state": entity.get("state", "unknown"),
                            "score": 100,  # Perfect match since we're listing by domain
                            "match_type": "domain_listing",
                        }
                    )

                domain_list_data: dict[str, Any] = {
                    "success": True,
                    "query": query,
                    "domain_filter": domain_filter,
                    **_build_pagination_metadata(len(filtered_entities), offset, limit, results),
                    "results": results,
                    "search_type": "domain_listing",
                    "note": f"Listing all {domain_filter} entities (empty query with domain_filter)",
                }
                if group_by_domain_bool:
                    domain_list_data["by_domain"] = {domain_filter: results}
                return await add_timezone_metadata(client, domain_list_data)

            # Graceful degradation with fallback search methods
            # 1. Try fuzzy search (primary method)
            # 2. If that fails, try exact match
            # 3. If that fails, return partial results with warning
            # 4. Only error if all methods fail

            result: dict[str, Any] | None = None
            warning: str | None = None
            search_type = "fuzzy_search"

            # Step 1: Try fuzzy search
            try:
                result = await smart_tools.smart_entity_search(
                    query, limit, offset=offset, domain_filter=domain_filter
                )
                search_type = "fuzzy_search"
            except asyncio.CancelledError:
                raise
            except Exception as fuzzy_error:
                logger.warning(
                    f"Fuzzy search failed, trying exact match: {fuzzy_error}"
                )

                # Step 2: Try exact match fallback
                try:
                    result = await _exact_match_search(
                        client, query, domain_filter, limit, offset
                    )
                    warning = "Fuzzy search unavailable, using exact match"
                    search_type = "exact_match"
                except asyncio.CancelledError:
                    raise
                except Exception as exact_error:
                    logger.warning(
                        f"Exact match failed, trying partial results: {exact_error}"
                    )

                    # Step 3: Try partial results fallback
                    try:
                        result = await _partial_results_search(
                            client, query, domain_filter, limit, offset
                        )
                        warning = "Search degraded, returning partial results"
                        search_type = "partial_listing"
                    except asyncio.CancelledError:
                        raise
                    except Exception as partial_error:
                        # Step 4: All methods failed - raise to outer exception handler
                        logger.error(f"All search methods failed: {partial_error}")
                        raise Exception(
                            "All search methods failed"
                        ) from partial_error

            # Convert 'matches' to 'results' for backward compatibility
            if "matches" in result:
                result["results"] = result.pop("matches")

            # Remove legacy is_truncated if present (replaced by has_more)
            result.pop("is_truncated", None)

            # Add domain_filter to result if it was provided (for API consistency)
            if domain_filter:
                result["domain_filter"] = domain_filter

            # Ensure pagination metadata exists in result
            result.setdefault("offset", offset)
            result.setdefault("limit", limit)
            result.setdefault("count", len(result.get("results", [])))
            if "has_more" not in result:
                total = result.get("total_matches", 0)
                result["has_more"] = (result["offset"] + result["count"]) < total
                result["next_offset"] = result["offset"] + limit if result["has_more"] else None

            # Group by domain if requested
            if group_by_domain_bool and "results" in result:
                by_domain = {}
                for entity in result["results"]:
                    domain = entity.get("domain", entity["entity_id"].split(".")[0])
                    if domain not in by_domain:
                        by_domain[domain] = []
                    by_domain[domain].append(entity)
                result["by_domain"] = by_domain

            result["search_type"] = search_type

            # Add warning and partial flag if fallback was used
            if warning:
                result["warning"] = warning
                result["partial"] = True

            return await add_timezone_metadata(client, result)

        except Exception as e:
            error_response = exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "domain_filter": domain_filter,
                    "area_filter": area_filter,
                },
                raise_error=False,
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                    "Check area/domain filter spelling",
                ],
            )
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["search"],
            "title": "Get System Overview",
        }
    )
    @log_tool_usage
    async def ha_get_overview(
        detail_level: Annotated[
            Literal["minimal", "standard", "full"],
            Field(
                default="minimal",
                description=(
                    "'minimal': 10 entities per domain sample (default); "
                    "'standard': ALL entities per domain (friendly_name only); "
                    "'full': ALL entities with entity_id + friendly_name + state + system_info"
                ),
            ),
        ] = "minimal",
        max_entities_per_domain: Annotated[
            int | None,
            Field(
                default=None,
                description="Override max entities per domain (None = all). Minimal defaults to 10.",
            ),
        ] = None,
        include_state: Annotated[
            bool | str | None,
            Field(
                default=None,
                description="Include state field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
        include_entity_id: Annotated[
            bool | str | None,
            Field(
                default=None,
                description="Include entity_id field for entities (None = auto based on level). Full defaults to True.",
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Get AI-friendly system overview with intelligent categorization.

        Returns comprehensive system information at the requested detail level,
        including Home Assistant base_url, version, location, timezone, and entity overview.
        Use 'standard' (default) for most queries. Optionally customize entity fields and limits.
        """
        # Coerce boolean parameters that may come as strings from XML-style calls
        include_state_bool = coerce_bool_param(
            include_state, "include_state", default=None
        )
        include_entity_id_bool = coerce_bool_param(
            include_entity_id, "include_entity_id", default=None
        )

        result = await smart_tools.get_system_overview(
            detail_level,
            max_entities_per_domain,
            include_state_bool,
            include_entity_id_bool,
        )
        result = cast(dict[str, Any], result)

        # Include system info - essential fields always, full details at "full" level
        try:
            config = await client.get_config()
            system_info: dict[str, Any] = {
                "base_url": client.base_url,
                "version": config.get("version"),
                "location_name": config.get("location_name"),
                "time_zone": config.get("time_zone"),
                "language": config.get("language"),
                "state": config.get("state"),
            }
            # Full detail level adds extended system info
            if detail_level == "full":
                system_info.update({
                    "country": config.get("country"),
                    "currency": config.get("currency"),
                    "unit_system": config.get("unit_system", {}),
                    "latitude": config.get("latitude"),
                    "longitude": config.get("longitude"),
                    "elevation": config.get("elevation"),
                    "components_loaded": len(config.get("components", [])),
                    "safe_mode": config.get("safe_mode", False),
                    "internal_url": config.get("internal_url"),
                    "external_url": config.get("external_url"),
                })
            result["system_info"] = system_info
        except Exception as e:
            logger.warning(f"Failed to fetch system info for overview: {e}")

        return result

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["search"],
            "title": "Deep Search",
        }
    )
    @log_tool_usage
    async def ha_deep_search(
        query: str,
        search_types: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Types to search: 'automation', 'script', 'helper'. "
                    "Pass as list or JSON array string. Default: all types."
                ),
            ),
        ] = None,
        limit: int = 5,
        offset: int = 0,
        include_config: Annotated[
            bool | str,
            Field(
                default=False,
                description=(
                    "Include full config in results. Default: False (returns summary only). "
                    "Use ha_config_get_automation/ha_config_get_script for individual configs."
                ),
            ),
        ] = False,
    ) -> dict[str, Any]:
        """Deep search across automation, script, and helper definitions.

        Searches not only entity names but also within configuration definitions including
        triggers, actions, sequences, and other config fields. Perfect for finding automations
        that use specific services, helpers referenced in scripts, or tracking down where
        particular entities are being used.

        Args:
            query: Search query (can be partial, with typos)
            search_types: Types to search (list of strings, default: ["automation", "script", "helper"])
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
        # Parse search_types to handle JSON string input from MCP clients
        parsed_search_types = parse_string_list_param(search_types, "search_types")
        include_config_bool = coerce_bool_param(include_config, "include_config", default=False) or False
        try:
            result = await smart_tools.deep_search(
                query, parsed_search_types, limit, offset, include_config_bool
            )
            return cast(dict[str, Any], result)
        except Exception as e:
            logger.error(
                f"Error in deep search: query={query}, "
                f"search_types={parsed_search_types}, limit={limit}, "
                f"error={e}",
                exc_info=True,
            )
            return exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "search_types": parsed_search_types,
                    "limit": limit,
                },
                raise_error=False,
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                ],
            )

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["search"],
            "title": "Get Entity State",
        }
    )
    @log_tool_usage
    async def ha_get_state(entity_id: str) -> dict[str, Any]:
        """Get detailed state information for a Home Assistant entity with timezone metadata."""
        try:
            result = await client.get_entity_state(entity_id)
            return await add_timezone_metadata(client, result)
        except Exception as e:
            error_str = str(e).lower()
            # Check if entity not found
            if "404" in error_str or "not found" in error_str:
                error_response = create_entity_not_found_error(
                    entity_id,
                    details=str(e),
                )
            else:
                error_response = exception_to_structured_error(
                    e,
                    context={"entity_id": entity_id},
                    raise_error=False,
                )
            # Add entity-specific suggestions
            if "error" in error_response and isinstance(error_response["error"], dict):
                error_response["error"]["suggestions"] = [
                    f"Verify entity '{entity_id}' exists in Home Assistant",
                    "Check Home Assistant connection",
                    "Use ha_search_entities() to find correct entity IDs",
                ]
            error_with_tz = await add_timezone_metadata(client, error_response)
            raise_tool_error(error_with_tz)

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["search"],
            "title": "Get Multiple Entity States",
        }
    )
    @log_tool_usage
    async def ha_get_states(
        entity_ids: Annotated[
            list[str],
            Field(
                description="List of entity IDs to retrieve states for (e.g., ['light.kitchen', 'sensor.temperature'])"
            ),
        ],
    ) -> dict[str, Any]:
        """Get state information for multiple Home Assistant entities in a single call.

        Efficiently retrieves states for multiple entities using parallel requests
        instead of calling ha_get_state repeatedly. Maximum 100 entities per call.
        Duplicate entity IDs are automatically deduplicated.

        Returns success=True if at least one entity state was retrieved.
        Check 'error_count' for any failed lookups in partial-success scenarios.

        WHEN TO USE:
        - Checking states of multiple entities at once (e.g., verifying automation results)
        - Comparing states across related entities
        - Any scenario requiring 2+ entity state lookups

        EXAMPLES:
        - ha_get_states(["light.kitchen", "light.living_room", "sensor.temperature"])
        - ha_get_states(["automation.morning_routine", "binary_sensor.motion"])

        NOTE: For a single entity, use ha_get_state() instead.
        """
        MAX_ENTITIES = 100

        if not isinstance(entity_ids, list) or not entity_ids:
            return await add_timezone_metadata(
                client,
                create_validation_error(
                    "entity_ids must be a non-empty list of entity ID strings",
                    parameter="entity_ids",
                ),
            )

        if not all(isinstance(eid, str) for eid in entity_ids):
            return await add_timezone_metadata(
                client,
                create_validation_error(
                    "All entity_ids values must be strings",
                    parameter="entity_ids",
                ),
            )

        if len(entity_ids) > MAX_ENTITIES:
            return await add_timezone_metadata(
                client,
                create_validation_error(
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum of {MAX_ENTITIES}",
                    parameter="entity_ids",
                ),
            )

        # Deduplicate while preserving order
        unique_ids = list(dict.fromkeys(entity_ids))
        if len(unique_ids) < len(entity_ids):
            logger.debug(
                f"Deduplicated entity_ids: {len(entity_ids)} -> {len(unique_ids)}"
            )

        try:
            async def _fetch_state(entity_id: str) -> dict[str, Any]:
                try:
                    state = await client.get_entity_state(entity_id)
                    return {"success": True, "entity_id": entity_id, "state": state}
                except Exception as e:
                    logger.warning(f"Failed to fetch state for '{entity_id}': {e}")
                    return exception_to_structured_error(
                        e, context={"entity_id": entity_id},
                        raise_error=False,
                    )

            results = await asyncio.gather(
                *(_fetch_state(eid) for eid in unique_ids)
            )

            states: dict[str, Any] = {}
            errors: list[dict[str, Any]] = []

            for eid, result in zip(unique_ids, results, strict=True):
                if result.get("success") is True and "state" in result:
                    states[eid] = result["state"]
                else:
                    error_detail = result.get("error")
                    if error_detail is None:
                        error_detail = {"code": "INTERNAL_ERROR", "message": "Unknown error"}
                    errors.append({
                        "entity_id": result.get("entity_id", eid),
                        "error": error_detail,
                    })

            response: dict[str, Any] = {
                "success": len(states) > 0,
                "count": len(states),
                "states": states,
            }

            if errors:
                response["errors"] = errors
                response["error_count"] = len(errors)
                response["suggestions"] = [
                    "Use ha_search_entities() to find correct entity IDs for failed lookups",
                    "Verify entities exist in Home Assistant",
                ]
                if states:
                    response["partial"] = True

            return await add_timezone_metadata(client, response)

        except Exception as e:
            logger.error(f"Error getting bulk states: {e}", exc_info=True)
            error_response = exception_to_structured_error(
                e, context={"entity_ids": entity_ids},
                raise_error=False,
            )
            return await add_timezone_metadata(client, error_response)
