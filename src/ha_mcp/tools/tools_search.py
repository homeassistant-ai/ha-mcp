"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, cast

from fastmcp.exceptions import ToolError
from pydantic import Field

from ..config import get_global_settings
from ..errors import create_validation_error
from ..transforms.categorized_search import DEFAULT_PINNED_TOOLS
from .helpers import exception_to_structured_error, log_tool_usage, raise_tool_error
from .util_helpers import (
    add_timezone_metadata,
    build_pagination_metadata,
    coerce_bool_param,
    coerce_int_param,
    parse_string_list_param,
)

logger = logging.getLogger(__name__)


def _build_pagination_metadata(
    total_matches: int, offset: int, limit: int, results: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build standardized pagination metadata for search responses.

    Thin wrapper around the shared ``build_pagination_metadata`` helper that
    keeps the existing call-site signature (accepts a *results* list and uses
    ``total_matches`` as the key name expected by search tools).
    """
    meta = build_pagination_metadata(total_matches, offset, limit, len(results))
    # Search tools use "total_matches" instead of "total_count" —
    # construct explicitly to avoid fragile dependency on shared helper's key names
    return {
        "total_matches": meta["total_count"],
        "offset": meta["offset"],
        "limit": meta["limit"],
        "count": meta["count"],
        "has_more": meta["has_more"],
        "next_offset": meta["next_offset"],
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
            is_exact = (
                query_lower == entity_id.lower() or query_lower == friendly_name.lower()
            )
            results.append(
                {
                    "entity_id": entity_id,
                    "friendly_name": friendly_name,
                    "domain": domain,
                    "state": entity.get("state", "unknown"),
                    "score": 100 if is_exact else 80,
                    "match_type": "exact_match",
                }
            )

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)
    paginated = results[offset : offset + limit]
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

    paginated = results[offset : offset + limit]
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
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Search Entities",
        },
    )
    @log_tool_usage
    async def ha_search_entities(
        query: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Entity name to search for (fuzzy or exact match). "
                    "Omit to list entities; `domain_filter` or `area_filter` "
                    "must be set in that mode."
                ),
            ),
        ] = None,
        domain_filter: Annotated[
            str | None,
            Field(
                default=None,
                description="Limit to a single domain (e.g. 'light', 'sensor', 'calendar').",
            ),
        ] = None,
        area_filter: Annotated[
            str | None,
            Field(
                default=None,
                description="Limit to entities in a specific area (area ID or name).",
            ),
        ] = None,
        limit: int = 10,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
        group_by_domain: bool | str = False,
        exact_match: Annotated[
            bool | str,
            Field(
                default=True,
                description=(
                    "Use exact substring matching (default: True). "
                    "Set to False for fuzzy matching when the query may contain "
                    "typos or approximate terms."
                ),
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Find or list entities (lights, sensors, switches, etc.) by name, domain, or area.

        When NOT to use: for searching inside automation, script, helper, or dashboard
        *configurations* (e.g. which automations call a service or reference an entity),
        use `ha_deep_search`.

        To enumerate all entities of a domain, omit `query` and pass `domain_filter`. For
        example, `ha_search_entities(domain_filter="calendar")` lists all calendars. At
        least one of `query`, `domain_filter`, or `area_filter` must be set.
        """
        # Normalize omitted/None query to empty string so downstream logic is unchanged
        query = query or ""
        if not query.strip() and not domain_filter and not area_filter:
            raise_tool_error(
                create_validation_error(
                    "At least one of 'query', 'domain_filter', or 'area_filter' must be set.",
                    parameter="query",
                )
            )
        # Coerce boolean parameter that may come as string from XML-style calls
        group_by_domain_bool = (
            coerce_bool_param(group_by_domain, "group_by_domain", default=False)
            or False
        )
        exact_match_bool = coerce_bool_param(exact_match, "exact_match", default=True)

        try:
            offset = coerce_int_param(offset, "offset", default=0, min_value=0) or 0
            limit = coerce_int_param(limit, "limit", default=10, min_value=1)

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

                    pagination = _build_pagination_metadata(
                        total_matches, offset, limit, results
                    )

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
                paginated_entities = filtered_entities[offset : offset + limit]
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
                    **_build_pagination_metadata(
                        len(filtered_entities), offset, limit, results
                    ),
                    "results": results,
                    "search_type": "domain_listing",
                    "note": f"Listing all {domain_filter} entities (empty query with domain_filter)",
                }
                if group_by_domain_bool:
                    domain_list_data["by_domain"] = {domain_filter: results}
                return await add_timezone_metadata(client, domain_list_data)

            # Search strategy depends on exact_match setting:
            # - exact_match=True: use exact substring matching directly
            # - exact_match=False: try fuzzy first, fall back to exact, then partial

            result: dict[str, Any] | None = None
            warning: str | None = None
            search_type = "exact_match" if exact_match_bool else "fuzzy_search"

            if exact_match_bool:
                # Exact match mode: skip fuzzy, go straight to substring matching
                try:
                    result = await _exact_match_search(
                        client, query, domain_filter, limit, offset
                    )
                    search_type = "exact_match"
                except asyncio.CancelledError:
                    raise
                except Exception as exact_error:
                    logger.warning(
                        f"Exact match failed, trying partial results: {exact_error}"
                    )
                    try:
                        result = await _partial_results_search(
                            client, query, domain_filter, limit, offset
                        )
                        warning = "Search degraded, returning partial results"
                        search_type = "partial_listing"
                    except asyncio.CancelledError:
                        raise
                    except Exception as partial_error:
                        logger.error(f"All search methods failed: {partial_error}")
                        raise Exception("All search methods failed") from partial_error
            else:
                # Fuzzy mode: graceful degradation chain
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
                        try:
                            result = await _partial_results_search(
                                client, query, domain_filter, limit, offset
                            )
                            warning = "Search degraded, returning partial results"
                            search_type = "partial_listing"
                        except asyncio.CancelledError:
                            raise
                        except Exception as partial_error:
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
                result["next_offset"] = (
                    result["offset"] + limit if result["has_more"] else None
                )

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

        except ToolError:
            raise
        except Exception as e:
            exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "domain_filter": domain_filter,
                    "area_filter": area_filter,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                    "Check area/domain filter spelling",
                ],
            )

    @mcp.tool(
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get System Overview",
        },
    )
    @log_tool_usage
    async def ha_get_overview(
        detail_level: Annotated[
            Literal["minimal", "standard", "full"],
            Field(
                default="minimal",
                description=(
                    "'minimal': 10 entities/domain, top-5 states (default); "
                    "'standard': 200 entities/page, top-10 states (use offset for more); "
                    "'full': 200 entities/page + entity_id + state + full states. "
                    "Use 'domains', 'limit', or max_entities_per_domain to control size"
                ),
            ),
        ] = "minimal",
        domains: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Filter to specific domains (e.g. 'light,sensor' or ['light','sensor']). "
                    "None = all domains. Useful to avoid context window overload."
                ),
            ),
        ] = None,
        limit: Annotated[
            int | str | None,
            Field(
                default=None,
                description=(
                    "Max total entities across all domains (default: unlimited for minimal, "
                    "200 for standard/full). Counts and states always complete. "
                    "Use with offset for pagination."
                ),
            ),
        ] = None,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of entities to skip for pagination (default: 0)",
            ),
        ] = 0,
        max_entities_per_domain: Annotated[
            int | None,
            Field(
                default=None,
                description="Override default entity cap per domain (minimal=10, standard/full=unlimited). 0 = no limit on entities or states.",
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
        include_notifications: Annotated[
            bool | str | None,
            Field(
                default=True,
                description="Include active persistent notifications (default: True). Set False to skip.",
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Get AI-friendly system overview with intelligent categorization.

        Returns comprehensive system information at the requested detail level,
        including Home Assistant base_url, version, location, timezone, entity overview,
        and active persistent notifications (if any).
        Use 'minimal' (default) for most queries. Domain counts and states_summary
        are always complete regardless of entity pagination.
        Standard/full modes paginate entities (default 200 per page) — use offset
        to fetch more. Use 'domains' filter to narrow scope.
        """
        # Coerce boolean parameters that may come as strings from XML-style calls
        include_state_bool = coerce_bool_param(
            include_state, "include_state", default=None
        )
        include_entity_id_bool = coerce_bool_param(
            include_entity_id, "include_entity_id", default=None
        )
        include_notifications_bool = coerce_bool_param(
            include_notifications, "include_notifications", default=True
        )

        # Parse domains filter
        parsed_domains = parse_string_list_param(domains, "domains", allow_csv=True)

        # Parse pagination parameters
        limit_int = coerce_int_param(limit, "limit", default=None, min_value=1)
        offset_int = coerce_int_param(offset, "offset", default=0, min_value=0) or 0

        result = await smart_tools.get_system_overview(
            detail_level,
            max_entities_per_domain,
            include_state_bool,
            include_entity_id_bool,
            domains_filter=parsed_domains,
            limit=limit_int,
            offset=offset_int,
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
                system_info.update(
                    {
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
                        # No default: distinguish HA-not-exposing-the-key (None)
                        # from empty-allowlist ([]) — security-relevant for agents.
                        "allowlist_external_dirs": config.get(
                            "allowlist_external_dirs"
                        ),
                    }
                )
            result["system_info"] = system_info
        except Exception as e:
            logger.warning(f"Failed to fetch system info for overview: {e}")

        # Include active persistent notifications
        if include_notifications_bool:
            result["notification_count"] = 0
            try:
                ws_result = await client.send_websocket_message(
                    {"type": "persistent_notification/get"}
                )
                if ws_result.get("success"):
                    notifications = ws_result.get("result", [])
                    result["notification_count"] = len(notifications)
                    if notifications:
                        result["notifications"] = [
                            {
                                "notification_id": n.get("notification_id"),
                                "title": n.get("title"),
                                "message": n.get("message"),
                                "created_at": n.get("created_at"),
                            }
                            for n in notifications
                        ]
            except Exception as e:
                logger.warning(f"Failed to fetch notifications for overview: {e}")

        # Include active repair issues
        result["repair_count"] = 0
        try:
            repairs_result = await client.send_websocket_message(
                {"type": "repairs/list_issues"}
            )
            if repairs_result.get("success"):
                issues = repairs_result.get("result", {}).get("issues", [])
                result["repair_count"] = len(issues)
                if issues:
                    result["repairs"] = [
                        {
                            "issue_id": r.get("issue_id"),
                            "domain": r.get("domain"),
                            "severity": r.get("severity"),
                            "translation_key": r.get("translation_key"),
                        }
                        for r in issues
                    ]
        except Exception as e:
            logger.warning("Failed to fetch repairs for overview: %s", e)
            result["repairs_error"] = f"Could not fetch repairs: {e}"

        # Include tool discovery hint when search transform is active
        settings = get_global_settings()
        if settings.enable_tool_search:
            result["tool_discovery"] = {
                "hint": (
                    "This server uses search-based tool discovery. "
                    "Use ha_search_tools(query='...') to find tools, then "
                    "execute the discovered tool directly by name (preferred), "
                    "or via a proxy for permission gating: "
                    "ha_call_read_tool, ha_call_write_tool, or "
                    "ha_call_delete_tool. Each proxy takes name and arguments "
                    "as separate top-level params. Call proxy tools SEQUENTIALLY "
                    "(not in parallel) to avoid cascading cancellations. "
                    "Do NOT assume a capability is unavailable without searching first."
                ),
                "pinned_tools": sorted(
                    [
                        *DEFAULT_PINNED_TOOLS,
                        "ha_search_tools",
                        "ha_call_read_tool",
                        "ha_call_write_tool",
                        "ha_call_delete_tool",
                    ]
                ),
            }

        return result

    @mcp.tool(
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Deep Search",
        },
    )
    @log_tool_usage
    async def ha_deep_search(
        query: str,
        search_types: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Types to search: 'automation', 'script', 'helper', 'dashboard'. "
                    "Pass as list or JSON array string. Default: automation, script, helper."
                ),
            ),
        ] = None,
        limit: Annotated[
            int | str,
            Field(
                default=5,
                description="Maximum total results to return (default: 5)",
            ),
        ] = 5,
        offset: Annotated[
            int | str,
            Field(
                default=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
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
        exact_match: Annotated[
            bool | str,
            Field(
                default=True,
                description=(
                    "Use exact substring matching (default: True). "
                    "Set to False for fuzzy matching when the query may contain typos "
                    "or when searching with approximate terms."
                ),
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Search inside automation, script, helper, and dashboard *configurations* — not for finding entity IDs.

        Use this when you need to find automations/scripts by what they *do* (e.g., which automations
        call a specific service, reference a particular entity, or contain a certain action).
        For finding entity IDs by name, use ha_search_entities instead.

        Searches within configuration definitions including triggers, actions, sequences, and other
        config fields. Also searches dashboard configurations (cards, badges, views) when
        search_types includes 'dashboard'.

        **NOTE:** Dashboards and badges are NOT searched by default. Add 'dashboard' to
        search_types to include them.

        Args:
            query: Search query (exact substring by default, or fuzzy with exact_match=False)
            search_types: Types to search (default: ["automation", "script", "helper"])
            limit: Maximum total results to return (default: 5)
            exact_match: Use exact substring matching (default: True)

        Examples:
            - Find automations referencing an entity: ha_deep_search("sensor.temperature")
            - Find with fuzzy matching: ha_deep_search("motion", exact_match=False)
            - Search dashboards for entity refs: ha_deep_search("sensor.temperature", search_types=["dashboard"])
            - Search everything: ha_deep_search("light.bedroom", search_types=["automation","script","helper","dashboard"])
        """
        # Parse search_types to handle JSON string input from MCP clients
        parsed_search_types = parse_string_list_param(search_types, "search_types")
        include_config_bool = (
            coerce_bool_param(include_config, "include_config", default=False) or False
        )
        exact_match_bool = coerce_bool_param(exact_match, "exact_match", default=True)
        try:
            limit = coerce_int_param(limit, "limit", default=5, min_value=1)
            offset = coerce_int_param(offset, "offset", default=0, min_value=0)
            result = await smart_tools.deep_search(
                query,
                parsed_search_types,
                limit,
                offset,
                include_config_bool,
                exact_match=exact_match_bool,
            )
            return cast(dict[str, Any], result)
        except ToolError:
            raise
        except Exception as e:
            logger.error(
                f"Error in deep search: query={query}, "
                f"search_types={parsed_search_types}, limit={limit}, "
                f"error={e}",
                exc_info=True,
            )
            exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "search_types": parsed_search_types,
                    "limit": limit,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                ],
            )

    @mcp.tool(
        tags={"Search & Discovery"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Entity State",
        },
    )
    @log_tool_usage
    async def ha_get_state(
        entity_id: Annotated[
            str | list[str],
            Field(
                description="Entity ID or list of entity IDs to retrieve state for "
                "(e.g., 'light.kitchen' or ['light.kitchen', 'sensor.temperature'])"
            ),
        ],
    ) -> dict[str, Any]:
        """Get current status, state, and attributes of one or more entities (lights, switches, sensors, climate, covers, locks, fans, etc.).

        SINGLE ENTITY:
        Pass a string entity_id. Returns the entity's full state and attributes.

        MULTIPLE ENTITIES:
        Pass a list of entity IDs (max 100). Efficiently retrieves states using
        parallel requests. Duplicates are automatically deduplicated.
        Returns success=True if at least one entity state was retrieved.
        Check 'error_count' for any failed lookups in partial-success scenarios.

        EXAMPLES:
        - Single: ha_get_state("light.kitchen")
        - Multiple: ha_get_state(["light.kitchen", "light.living_room", "sensor.temperature"])
        """
        # Single entity path
        if isinstance(entity_id, str):
            try:
                result = await client.get_entity_state(entity_id)
                return await add_timezone_metadata(client, result)
            except ToolError:
                raise
            except Exception as e:
                exception_to_structured_error(
                    e,
                    context={"entity_id": entity_id},
                    suggestions=[
                        f"Verify entity '{entity_id}' exists in Home Assistant",
                        "Check Home Assistant connection",
                        "Use ha_search_entities() to find correct entity IDs",
                    ],
                )

        # Multiple entities path
        entity_ids: list[str] = entity_id
        MAX_ENTITIES = 100

        if not isinstance(entity_ids, list) or not entity_ids:
            raise_tool_error(create_validation_error(
                "entity_id must be a non-empty string or list of entity ID strings",
                parameter="entity_id",
            ))

        if not all(isinstance(eid, str) for eid in entity_ids):
            raise_tool_error(create_validation_error(
                "All entity_id values must be strings",
                parameter="entity_id",
            ))

        if len(entity_ids) > MAX_ENTITIES:
            raise_tool_error(create_validation_error(
                f"Too many entity IDs: {len(entity_ids)} exceeds maximum of {MAX_ENTITIES}",
                parameter="entity_id",
            ))

        # Deduplicate while preserving order
        unique_ids = list(dict.fromkeys(entity_ids))
        if len(unique_ids) < len(entity_ids):
            logger.debug(
                f"Deduplicated entity_ids: {len(entity_ids)} -> {len(unique_ids)}"
            )

        try:

            async def _fetch_state(eid: str) -> dict[str, Any]:
                try:
                    state = await client.get_entity_state(eid)
                    return {"success": True, "entity_id": eid, "state": state}
                except Exception as e:
                    logger.warning(f"Failed to fetch state for '{eid}': {e}")
                    # ast-grep-ignore — batch item failure, aggregated via asyncio.gather
                    return exception_to_structured_error(
                        e,
                        context={"entity_id": eid},
                        raise_error=False,
                    )

            results = await asyncio.gather(*(_fetch_state(eid) for eid in unique_ids))

            states: dict[str, Any] = {}
            errors: list[dict[str, Any]] = []

            for eid, result in zip(unique_ids, results, strict=True):
                if result.get("success") is True and "state" in result:
                    states[eid] = result["state"]
                else:
                    error_detail = result.get("error")
                    if error_detail is None:
                        error_detail = {
                            "code": "INTERNAL_ERROR",
                            "message": "Unknown error",
                        }
                    errors.append(
                        {
                            "entity_id": result.get("entity_id", eid),
                            "error": error_detail,
                        }
                    )

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

        except ToolError:
            raise
        except Exception as e:
            logger.error(f"Error getting bulk states: {e}", exc_info=True)
            exception_to_structured_error(
                e,
                context={"entity_ids": entity_ids},
            )
