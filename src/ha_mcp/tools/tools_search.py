"""
Search and discovery tools for Home Assistant MCP server.

This module provides entity search, system overview, deep search, and state retrieval tools.
"""

import asyncio
import logging
from typing import Annotated, Any, Literal, cast

from fastmcp import Context
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
    keeps the existing call-site signature (accepts a *results* list) and
    renames ``total_count`` → ``total_matches`` to match the search tools'
    response shape.
    """
    meta = build_pagination_metadata(total_matches, offset, limit, len(results))
    meta["total_matches"] = meta.pop("total_count")
    return meta


async def _exact_match_search(
    client: Any,
    query: str,
    domain_filter: str | None,
    limit: int,
    offset: int = 0,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Search entities by substring on entity_id + friendly_name.

    Used both as the ``exact_match=True`` primary path and as the
    fallback when fuzzy search raises. In addition to ``client.get_states()``,
    also queries the entity registry via WebSocket to honor
    ``include_hidden``: when False, entities with ``hidden_by != null``
    are skipped.
    """
    # Fetch states + entity registry in parallel. Registry-list failure
    # is tolerated (we just lose the hidden filter); states-fetch failure
    # is fatal — auth/connection errors must propagate so the agent sees
    # "your token is invalid" instead of "zero entities matched".
    entities_task = client.get_states()
    registry_task = client.send_websocket_message(
        {"type": "config/entity_registry/list"}
    )
    gather_results = await asyncio.gather(
        entities_task, registry_task, return_exceptions=True
    )
    state_result: Any = gather_results[0]
    registry_result: Any = gather_results[1]
    if isinstance(state_result, BaseException):
        raise state_result
    all_entities = state_result
    hidden_ids: set[str] = set()
    if (
        not include_hidden
        and isinstance(registry_result, dict)
        and registry_result.get("success")
    ):
        for entry in registry_result.get("result", []):
            if entry.get("hidden_by") is not None:
                eid = entry.get("entity_id")
                if eid:
                    hidden_ids.add(eid)

    query_lower = query.lower().strip()

    results = []
    for entity in all_entities:
        entity_id = entity.get("entity_id", "")
        if entity_id in hidden_ids:
            continue
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
                description=(
                    "Limit to a single domain (e.g. 'light', 'sensor', "
                    "'calendar'). Case-insensitive — values are normalized "
                    "to lowercase before matching."
                ),
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
        include_hidden: Annotated[
            bool | str,
            Field(
                default=False,
                description=(
                    "Include entities marked hidden_by in the entity registry "
                    "(default: False). Hidden entities are typically integration "
                    "diagnostics or user-suppressed entries that an agent acting "
                    "on a user phrase shouldn't surface. Set to True to include "
                    "them — useful for diagnostics or service workflows."
                ),
            ),
        ] = False,
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
        # HA domains are canonically lowercase; agents that capitalize from a
        # user phrase ("turn on the Lights") would otherwise hit a silent
        # zero-result.
        if domain_filter:
            domain_filter = domain_filter.lower()
        # Coerce boolean parameter that may come as string from XML-style calls
        group_by_domain_bool = (
            coerce_bool_param(group_by_domain, "group_by_domain", default=False)
            or False
        )
        exact_match_bool = coerce_bool_param(exact_match, "exact_match", default=True)
        include_hidden_bool = (
            coerce_bool_param(include_hidden, "include_hidden", default=False)
            or False
        )

        try:
            offset = coerce_int_param(offset, "offset", default=0, min_value=0) or 0
            limit = coerce_int_param(limit, "limit", default=10, min_value=1)

            # If area_filter is provided, use area-based search
            if area_filter:
                area_result = await smart_tools.get_entities_by_area(
                    area_filter,
                    group_by_domain=True,
                    include_hidden=include_hidden_bool,
                )

                # If we also have a query, filter the area results
                if query and query.strip():
                    # Collect entities from all matched areas, applying
                    # domain_filter if present. get_entities_by_area is called
                    # with group_by_domain=True above, so entities is always a
                    # dict keyed by domain. Iterate sorted area_id keys so
                    # the order matches the area_only branch.
                    all_area_entities = []
                    for area_id in sorted(area_result.get("areas", {})):
                        area_data = area_result["areas"][area_id]
                        entities = area_data.get("entities") or {}
                        if domain_filter:
                            all_area_entities.extend(entities.get(domain_filter, []))
                        else:
                            for domain_entities in entities.values():
                                all_area_entities.extend(domain_entities)

                    # Batch-fetch aliases for the surviving entity_ids so
                    # the fuzzy haystack includes them. Aliases live only
                    # in get_entries (not the slim list endpoint), so
                    # this is a single bounded round-trip on top of
                    # get_entities_by_area's calls.
                    area_entity_ids = sorted(
                        e.get("entity_id", "")
                        for e in all_area_entities
                        if e.get("entity_id")
                    )
                    aliases_map: dict[str, list[str]] = {}
                    if area_entity_ids:
                        try:
                            entries_resp = await client.send_websocket_message({
                                "type": "config/entity_registry/get_entries",
                                "entity_ids": area_entity_ids,
                            })
                            if (
                                isinstance(entries_resp, dict)
                                and entries_resp.get("success")
                            ):
                                for eid, entry in (
                                    entries_resp.get("result", {}) or {}
                                ).items():
                                    if isinstance(entry, dict):
                                        aliases_map[eid] = (
                                            entry.get("aliases", []) or []
                                        )
                            else:
                                logger.warning(
                                    "alias_enrichment_failed: get_entries "
                                    "returned non-success for %d area "
                                    "entities (resp=%r)",
                                    len(area_entity_ids),
                                    entries_resp,
                                )
                        except (KeyError, TypeError, AttributeError) as alias_err:
                            logger.warning(
                                "alias_enrichment_failed: malformed payload "
                                "for %d area entities (err=%r)",
                                len(area_entity_ids),
                                alias_err,
                            )

                    # Apply fuzzy search to area entities
                    from ..utils.fuzzy_search import create_fuzzy_searcher

                    fuzzy_searcher = create_fuzzy_searcher(threshold=80)

                    entities_for_search = [
                        {
                            "entity_id": entity.get("entity_id", ""),
                            "attributes": {
                                "friendly_name": entity.get("friendly_name", "")
                            },
                            "state": entity.get("state", "unknown"),
                            "_aliases": aliases_map.get(
                                entity.get("entity_id", ""), []
                            ),
                        }
                        for entity in all_area_entities
                    ]

                    matches, total_matches = fuzzy_searcher.search_entities(
                        entities_for_search, query, limit, offset
                    )

                    # Format matches similar to smart_entity_search.
                    # Top-level `area_filter` already carries this
                    # context for the caller; per-result echo would be
                    # redundant and asymmetric vs the other branches.
                    results = [
                        {
                            "entity_id": match["entity_id"],
                            "friendly_name": match["friendly_name"],
                            "domain": match["domain"],
                            "state": match["state"],
                            "score": match["score"],
                            "match_type": match["match_type"],
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
                    if domain_filter:
                        search_data["domain_filter"] = domain_filter

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
                        # Iterate ALL fuzzy-matched areas, not just the first.
                        # Pre-fix: `next(iter(...))` silently dropped every
                        # area but one — a query like area_filter="bedroom"
                        # against ["bedroom","bedroom_kids"] would return
                        # only one area's entities and miss the user's
                        # intended one entirely. Match the with-query
                        # branch by iterating all matched areas.
                        all_results: list[dict[str, Any]] = []
                        area_names_matched: list[str] = []
                        # Sort area_id keys to make iteration deterministic;
                        # the upstream `matched_area_ids` is a set, so
                        # without sorting we'd be at the mercy of CPython
                        # set-iteration order.
                        for area_id in sorted(area_result["areas"]):
                            area_data = area_result["areas"][area_id]
                            area_names_matched.append(
                                area_data.get("area_name", area_id)
                            )
                            entities_data = area_data.get("entities") or {}
                            for domain, entities in entities_data.items():
                                if domain_filter and domain != domain_filter:
                                    continue
                                # `{**entity, ...}` avoids mutating dicts
                                # owned by the smart_search helper.
                                # Add score+match_type so the response
                                # shape matches the other four
                                # search-type branches. Score=100 because
                                # area membership is exact, not fuzzy.
                                # Strip leading-underscore internal
                                # fields (e.g. `_aliases`) so the
                                # response only contains public-API
                                # fields.
                                all_results.extend(
                                    {
                                        **{
                                            k: v
                                            for k, v in entity.items()
                                            if not k.startswith("_")
                                        },
                                        "domain": domain,
                                        "score": 100,
                                        "match_type": "area_match",
                                    }
                                    for entity in entities
                                )

                        paginated = all_results[offset : offset + limit]

                        area_search_data: dict[str, Any] = {
                            "success": True,
                            "area_filter": area_filter,
                            **_build_pagination_metadata(
                                len(all_results), offset, limit, paginated
                            ),
                            "results": paginated,
                            "search_type": "area_only",
                            # `area_names` lists every matched area;
                            # `area_name` (singular) is kept for backward
                            # compatibility with existing callers — new
                            # callers should read `area_names`.
                            "area_names": area_names_matched,
                            "area_name": (
                                area_names_matched[0]
                                if area_names_matched
                                else area_filter
                            ),
                        }
                        if domain_filter:
                            area_search_data["domain_filter"] = domain_filter
                        if group_by_domain_bool:
                            # Group the paginated slice (not all_results) so
                            # by_domain and results stay in sync.
                            paginated_by_domain: dict[str, list[dict[str, Any]]] = {}
                            for entity in paginated:
                                paginated_by_domain.setdefault(
                                    entity["domain"], []
                                ).append(entity)
                            area_search_data["by_domain"] = paginated_by_domain
                        return await add_timezone_metadata(client, area_search_data)
                    else:
                        # Empty match: still emit `area_names: []` so
                        # callers don't KeyError when they read the
                        # field on a zero-match response. Symmetry with
                        # the populated branch.
                        empty_area_data: dict[str, Any] = {
                            "success": True,
                            "area_filter": area_filter,
                            **_build_pagination_metadata(0, offset, limit, []),
                            "results": [],
                            "search_type": "area_only",
                            "area_names": [],
                            "message": f"No entities found in area: {area_filter}",
                        }
                        if domain_filter:
                            empty_area_data["domain_filter"] = domain_filter
                        if group_by_domain_bool:
                            empty_area_data["by_domain"] = {}
                        return await add_timezone_metadata(client, empty_area_data)

            # Regular entity search (no area filter)
            # Handle empty query with domain_filter - list all entities of that domain
            if domain_filter and (not query or not query.strip()):
                # Fetch states + registry list in parallel. Registry-list
                # failure is tolerated (we just lose the hidden filter);
                # states-fetch failure is fatal — auth/connection errors
                # must propagate so the agent sees the real cause instead
                # of silently ranked-zero results.
                states_task = client.get_states()
                registry_task = client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                )
                gather_results = await asyncio.gather(
                    states_task, registry_task, return_exceptions=True
                )
                states_result: Any = gather_results[0]
                registry_result: Any = gather_results[1]
                if isinstance(states_result, BaseException):
                    raise states_result
                all_entities = states_result
                hidden_ids: set[str] = set()
                if (
                    not include_hidden_bool
                    and isinstance(registry_result, dict)
                    and registry_result.get("success")
                ):
                    for entry in registry_result.get("result", []):
                        if entry.get("hidden_by") is not None:
                            eid = entry.get("entity_id")
                            if eid:
                                hidden_ids.add(eid)

                # Filter by domain (and hidden_by, when not opted in)
                filtered_entities = [
                    e
                    for e in all_entities
                    if e.get("entity_id", "").startswith(f"{domain_filter}.")
                    and e.get("entity_id") not in hidden_ids
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
            # - exact_match=True: substring match
            # - exact_match=False: fuzzy first, fall back to substring on failure
            #
            # If both real strategies fail we propagate the exception so
            # callers see why; we deliberately do NOT fall back to a
            # zero-scored entity dump (a clean error is strictly more
            # useful to an agent than a noise pile flagged
            # `partial: True`).

            result: dict[str, Any]
            warning: str | None = None
            search_type = "exact_match" if exact_match_bool else "fuzzy_search"

            if exact_match_bool:
                # Exact match mode: substring matching only. No fallback —
                # _exact_match_search only fails when client.get_states()
                # itself fails, in which case any further retry is futile.
                result = await _exact_match_search(
                    client,
                    query,
                    domain_filter,
                    limit,
                    offset,
                    include_hidden=include_hidden_bool,
                )
                search_type = "exact_match"
            else:
                # Fuzzy mode: BM25 → substring fallback on exception only.
                try:
                    result = await smart_tools.smart_entity_search(
                        query,
                        limit,
                        offset=offset,
                        domain_filter=domain_filter,
                        include_hidden=include_hidden_bool,
                    )
                    search_type = "fuzzy_search"
                except asyncio.CancelledError:
                    raise
                except ToolError:
                    # Auth/connection/structured failures must propagate; the
                    # substring fallback below is for fuzzy-engine bugs only.
                    raise
                except Exception as fuzzy_error:
                    logger.warning(
                        f"Fuzzy search failed, falling back to substring "
                        f"match: {fuzzy_error}"
                    )
                    result = await _exact_match_search(
                        client,
                        query,
                        domain_filter,
                        limit,
                        offset,
                        include_hidden=include_hidden_bool,
                    )
                    warning = "Fuzzy search unavailable, using substring match"
                    search_type = "exact_match"

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
                    "Types to search: 'automation', 'script', 'scene', 'helper', 'dashboard'. "
                    "Pass as list or JSON array string. Default: automation, script, scene, helper."
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
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Search inside automation, script, scene, helper, and dashboard *configurations* — not for finding entity IDs.

        Use this when you need to find configurations by what they *do* (e.g., which automations
        call a specific service, which scenes set a particular entity, or any config that contains
        a certain action). For finding entity IDs by name, use ha_search_entities instead.

        Searches within configuration definitions including triggers, actions, sequences, scene
        entity sets, and other config fields. Also searches dashboard configurations (cards,
        badges, views) when search_types includes 'dashboard'.

        **NOTE:** Dashboards and badges are NOT searched by default. Add 'dashboard' to
        search_types to include them.

        Args:
            query: Search query (exact substring by default, or fuzzy with exact_match=False)
            search_types: Types to search (default: ["automation", "script", "scene", "helper"])
            limit: Maximum total results to return (default: 5)
            exact_match: Use exact substring matching (default: True)

        Examples:
            - Find automations referencing an entity: ha_deep_search("sensor.temperature")
            - Find with fuzzy matching: ha_deep_search("motion", exact_match=False)
            - Find scenes touching a light: ha_deep_search("light.kitchen")
            - Search dashboards for entity refs: ha_deep_search("sensor.temperature", search_types=["dashboard"])
            - Search everything: ha_deep_search("light.bedroom", search_types=["automation","script","scene","helper","dashboard"])
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
                ctx=ctx,
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
            raise_tool_error(
                create_validation_error(
                    "entity_id must be a non-empty string or list of entity ID strings",
                    parameter="entity_id",
                )
            )

        if not all(isinstance(eid, str) for eid in entity_ids):
            raise_tool_error(
                create_validation_error(
                    "All entity_id values must be strings",
                    parameter="entity_id",
                )
            )

        if len(entity_ids) > MAX_ENTITIES:
            raise_tool_error(
                create_validation_error(
                    f"Too many entity IDs: {len(entity_ids)} exceeds maximum of {MAX_ENTITIES}",
                    parameter="entity_id",
                )
            )

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
