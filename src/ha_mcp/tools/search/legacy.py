"""Legacy ``ha_search`` route.

The REST and WebSocket orchestration used when the component search command
is unavailable, plus the config-body deep search it calls.
"""

import asyncio
import logging
from typing import Annotated, Any, cast

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ...errors import create_validation_error
from ..coercion import parse_string_list_param
from ..helpers import (
    exception_to_structured_error,
    raise_tool_error,
)
from ..util_helpers import merge_visibility_warnings
from .entities import (
    _normalize_state_filter,
    _requested_membership,
    _validate_entity_search_params,
)
from .modes import EntityModesMixin
from .response import (
    _CONFIG_BUCKETS,
    _apply_search_outcome,
    _emit_intent_skip_warning,
    _finalize_partial_state,
    _mirror_partial_to_warnings,
    _new_search_response,
    _prefetch_shared_search_snapshots,
    _project_response_fields,
    _ResolvedSearch,
    _synthesize_combined_pagination,
    _validate_search_types,
)

logger = logging.getLogger(__name__)


class LegacySearchMixin(EntityModesMixin):
    """Server-side ``ha_search`` orchestration without the component command."""

    async def _legacy_ha_search(
        self, req: _ResolvedSearch, ctx: Context | None
    ) -> dict[str, Any]:
        """Run the multi-fetch REST/WS ha_search orchestration (fallback path).

        Behaviourally unchanged from the pre-component implementation: the two
        surfaces fan out over shared ``/api/states`` + entity-registry
        snapshots, gather with per-surface partial handling, and assemble the
        flat dual-surface envelope.
        """
        # When both branches run they each independently fetch the full state
        # machine (/api/states) and the entity-registry list; fetch each once and
        # thread the snapshots down so the two branches share one of each instead
        # of fetching two.
        shared_states, shared_registry = await _prefetch_shared_search_snapshots(
            self._client,
            registry_eligible=req.registry_eligible,
            body_eligible=req.body_eligible,
        )

        registry_callable_kwargs: dict[str, Any] = {
            "query": req.query_text or None,
            "domain_filter": req.domain_filter,
            "area_filter": req.area_filter,
            "limit": req.limit,
            "offset": req.offset,
            "exact_match": req.exact_match,
            "include_hidden": req.include_hidden,
            "group_by_domain": req.group_by_domain,
            "per_domain_limit": req.per_domain_limit,
            "state_filter": req.state_filter,
            "result_fields": req.result_fields,
            "prefetched_states": shared_states,
            "prefetched_registry": shared_registry,
        }

        tasks: list[Any] = []
        labels: list[str] = []
        if req.registry_eligible:
            tasks.append(self._ha_search_entities(**registry_callable_kwargs))
            labels.append("entities")
        if req.body_eligible:
            tasks.append(
                self._ha_deep_search(
                    query=req.query_text,
                    search_types=req.parsed_search_types,
                    limit=req.limit,
                    offset=req.offset,
                    include_config=req.include_config,
                    exact_match=req.exact_match,
                    config_time_budget=req.config_time_budget,
                    ctx=ctx,
                    prefetched_states=shared_states,
                    prefetched_registry=shared_registry,
                )
            )
            labels.append("configs")

        # ``return_exceptions=True`` captures sub-task exceptions; the gather
        # call itself only raises if the orchestrator's own coroutine is
        # cancelled before the tasks complete.
        outcomes = await asyncio.gather(*tasks, return_exceptions=True)

        response = _new_search_response(req.query, req.parsed_search_types)
        # Surface the body-skip so a caller who actually wanted config
        # matches alongside the entity scope can see why their request
        # returned no automations / scripts / etc.
        _emit_intent_skip_warning(response, req.body_skipped_by_intent_gate)
        partial = False
        errors: list[dict[str, str]] = []
        for label, outcome in zip(labels, outcomes, strict=True):
            # Propagate non-Exception BaseException (CancelledError, SystemExit,
            # KeyboardInterrupt, GeneratorExit) so callers — timeouts, structured
            # concurrency, signal handlers — can react cleanly.
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, Exception
            ):
                raise outcome
            if isinstance(outcome, Exception):
                partial = True
                # ``str(asyncio.TimeoutError())`` is "" — fall back to the type
                # name so partial_reason never reads "entities: ".
                errors.append(
                    {"surface": label, "error": str(outcome) or type(outcome).__name__}
                )
                logger.warning("ha_search %s branch failed: %r", label, outcome)
                continue
            _apply_search_outcome(response, label, outcome)

        # ``count`` mirrors the previous ha_search_entities semantics: items
        # returned in this response (post-pagination), not total matches across
        # the corpus. Total matches live in entity_total_matches +
        # config_total_matches.
        response["count"] = len(response["entities"]) + sum(
            len(response.get(bucket, [])) for bucket in _CONFIG_BUCKETS
        )

        _synthesize_combined_pagination(response)
        _finalize_partial_state(response, partial_local=partial, errors_local=errors)
        _mirror_partial_to_warnings(response)

        return _project_response_fields(response, req.parsed_fields)

    async def _ha_search_entities(
        self,
        query: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Entity name to search for (fuzzy or exact match). "
                    "Omit to list entities; `domain_filter`, `area_filter`, "
                    "or `state_filter` must be set in that mode."
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
                description=(
                    "Limit to an area, or an exact/close floor name to expand "
                    "to every area on that floor."
                ),
            ),
        ] = None,
        limit: Annotated[
            int,
            Field(
                default=10,
                ge=1,
                description="Maximum number of results to return (default: 10, minimum: 1)",
            ),
        ] = 10,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
        group_by_domain: bool = False,
        exact_match: Annotated[
            bool,
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
            bool,
            Field(
                default=True,
                description=(
                    "Include entities marked hidden_by in the entity registry "
                    "(default: True). Hidden entities still appear in results "
                    "but receive a score penalty so they sort below comparable "
                    "visible matches — typically pulling integration "
                    "diagnostics and user-suppressed entries to the bottom of "
                    "the list rather than excluding them. Set to False to "
                    "filter them out entirely."
                ),
            ),
        ] = True,
        per_domain_limit: Annotated[
            int | None,
            Field(
                default=None,
                description=(
                    "When group_by_domain=True, cap results per domain to this number. "
                    "Applied after the global limit — use a high limit (e.g. limit=200) "
                    "with per_domain_limit=5 to get up to 5 entities from each domain. "
                    "Ignored when group_by_domain=False. "
                    "None = no per-domain cap (default)."
                ),
            ),
        ] = None,
        state_filter: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Filter results to entities in a specific state "
                    '(e.g. "on", "off", "unavailable"). Case-insensitive — '
                    "input is lowercased before matching. Applied server-side after "
                    "search results are collected. Can be used standalone (no "
                    "query/domain/area) to enumerate every entity in that state. "
                    "For exact-match, domain-listing, and state-listing searches, "
                    "total_matches reflects the filtered count. For fuzzy "
                    "searches, state_filter is page-only and total_matches remains "
                    "unfiltered (see state_filter_note in the response). "
                    "None = no state filter (default)."
                ),
            ),
        ] = None,
        result_fields: Annotated[
            str | list[str] | None,
            Field(
                default=None,
                description=(
                    "Project each entity record in results[] to only the specified keys. "
                    'E.g. ["entity_id", "state"] returns slim entity records. '
                    "None = full records (default). "
                    "Base keys: entity_id, friendly_name, domain, state, score, match_type. "
                    "Opt-in enrichment/membership keys (computed on request): area, floor, labels, aliases, is_group, member_entity_ids. "
                    "An unknown key is rejected."
                ),
            ),
        ] = None,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
    ) -> dict[str, Any]:
        """Search for entities (lights, sensors, switches, etc.) by name, domain, or area.

        Internal helper reached through the `ha_search` tool; the examples below
        use that tool as the entry point.

        When NOT to use: for searching inside automation, script, helper, or dashboard
        *configurations* (e.g. which automations call a service or reference an entity),
        search config bodies via `ha_search(query=...)`.

        To enumerate all entities of a domain, omit `query` and pass `domain_filter` —
        e.g. `ha_search(domain_filter="calendar")` lists all calendars. To enumerate
        every entity in a state, omit `query` and pass `state_filter` — e.g.
        `ha_search(state_filter="unavailable")`. At least one of `query`,
        `domain_filter`, `area_filter`, or `state_filter` must be set.

        ``prefetched_states`` / ``prefetched_registry`` are the orchestrator's
        shared snapshots; they only reach the regular (non-area, non-domain-only)
        path, which is the only one that can run alongside the config branch.
        """
        query, domain_filter, area_filter, parsed_result_fields = (
            _validate_entity_search_params(
                query, domain_filter, area_filter, result_fields, state_filter
            )
        )
        group_by_domain_bool = group_by_domain
        exact_match_bool = exact_match
        include_hidden_bool = include_hidden
        per_domain_limit_int = per_domain_limit

        try:
            state_filter = _normalize_state_filter(state_filter)

            if area_filter:
                area_result = await self._smart_tools.get_entities_by_area(
                    area_filter,
                    group_by_domain=True,
                    include_hidden=include_hidden_bool,
                    **(
                        {"include_membership": True}
                        if _requested_membership(parsed_result_fields)
                        else {}
                    ),
                )
                if query and query.strip():
                    area_search = await self._search_area_with_query(
                        query,
                        area_filter,
                        area_result,
                        domain_filter,
                        state_filter,
                        limit,
                        offset,
                        group_by_domain_bool,
                        per_domain_limit_int,
                        parsed_result_fields,
                    )
                else:
                    area_search = await self._search_area_only(
                        area_result,
                        area_filter,
                        domain_filter,
                        state_filter,
                        limit,
                        offset,
                        group_by_domain_bool,
                        per_domain_limit_int,
                        parsed_result_fields,
                    )
                # The three area builders rebuild a fresh response dict and do not
                # carry area_result's warnings; forward them here in one place so a
                # visibility/registry degradation on the area path is not silently
                # dropped (mirrors the non-area path's merge_visibility_warnings).
                # The builders now return the search dict directly (no
                # add_timezone_metadata wrapper), so warnings merge into that dict;
                # the ``["data"]`` unwrap is a defensive holdover for a hypothetical
                # future wrapped payload.
                warn_target = (
                    area_search["data"]
                    if isinstance(area_search, dict) and "data" in area_search
                    else area_search
                )
                merge_visibility_warnings(warn_target, area_result.get("warnings", []))
                return area_search

            if domain_filter and (not query or not query.strip()):
                return await self._search_domain_only(
                    query,
                    domain_filter,
                    state_filter,
                    limit,
                    offset,
                    include_hidden_bool,
                    group_by_domain_bool,
                    per_domain_limit_int,
                    parsed_result_fields,
                )

            if state_filter and not domain_filter and (not query or not query.strip()):
                return await self._search_state_only(
                    state_filter,
                    limit,
                    offset,
                    include_hidden_bool,
                    group_by_domain_bool,
                    per_domain_limit_int,
                    parsed_result_fields,
                )

            return await self._search_regular(
                query,
                domain_filter,
                state_filter,
                limit,
                offset,
                exact_match_bool,
                include_hidden_bool,
                group_by_domain_bool,
                per_domain_limit_int,
                parsed_result_fields,
                prefetched_states=prefetched_states,
                prefetched_registry=prefetched_registry,
            )

        except ToolError:
            raise
        except ValueError as e:
            # ValueError from param validation — surface as VALIDATION_FAILED
            # with the original message and NO generic operational
            # suggestions (those would just be misleading boilerplate
            # next to an unrelated message like "limit must be at least
            # 1, got 0").
            raise_tool_error(
                create_validation_error(
                    str(e),
                    context={
                        "query": query,
                        "domain_filter": domain_filter,
                        "area_filter": area_filter,
                        "state_filter": state_filter,
                    },
                )
            )
            return None  # unreachable: raise_tool_error always raises
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(
                e,
                context={
                    "query": query,
                    "domain_filter": domain_filter,
                    "area_filter": area_filter,
                    "state_filter": state_filter,
                },
                suggestions=[
                    "Check Home Assistant connection",
                    "Try simpler search terms",
                    "Check area/domain/state filter spelling",
                ],
            )
            return None  # unreachable: error helpers above always raise

    async def _ha_deep_search(
        self,
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
            int,
            Field(
                default=5,
                ge=1,
                description="Maximum total results to return (default: 5)",
            ),
        ] = 5,
        offset: Annotated[
            int,
            Field(
                default=0,
                ge=0,
                description="Number of results to skip for pagination (default: 0)",
            ),
        ] = 0,
        include_config: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Include full config in results. Default: False (returns summary only). "
                    "Use ha_config_get_automation/ha_config_get_script for individual configs."
                ),
            ),
        ] = False,
        exact_match: Annotated[
            bool,
            Field(
                default=True,
                description=(
                    "Use exact substring matching (default: True). "
                    "Set to False for fuzzy matching when the query may contain typos "
                    "or when searching with approximate terms."
                ),
            ),
        ] = True,
        config_time_budget: Annotated[
            float | None,
            Field(
                default=None,
                # Keep this identical to the ha_search parameter feeding it.
                # Undecorated helper, so this Field never reaches an advertised
                # schema and no guard can see it.
                ge=0.001,
                le=300,
                description=(
                    "Per-call override for the per-id config-fetch wall-clock "
                    "budget (seconds). Replaces the per-type "
                    "HAMCP_*_CONFIG_TIME_BUDGET defaults for automation, "
                    "script, AND scene branches. Use when a `partial: True` "
                    "response names time-budget skipping. Stateless per-call: "
                    "one caller's override doesn't affect others. None = use "
                    "the per-type env defaults."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
    ) -> dict[str, Any]:
        """Search inside automation, script, scene, helper, and dashboard *configurations* — not for finding entity IDs.

        Use this when you need to find configurations by what they *do* (e.g., which automations
        call a specific service, which scenes set a particular entity, or any config that contains
        a certain action). For finding entity IDs by name, use ha_search instead.

        Searches within configuration definitions including triggers, actions, sequences, scene
        entity sets, and other config fields. Also searches dashboard configurations (cards,
        badges, views) when search_types includes 'dashboard'.

        **NOTE:** Dashboards and badges are NOT searched by default. Add 'dashboard' to
        search_types to include them.

        The 'helper' search covers both input_* helpers (input_boolean, input_number, ...)
        and UI-created flow-based helpers (template, group, utility_meter, derivative, ...).
        For flow-helpers, results carry the parent config entry id under ``entry_id``.
        When ``include_config=False`` (the default), pair with
        ``ha_get_integration(entry_id=..., include_options=True)`` to retrieve the full
        config; set ``include_config=True`` to get it inline in one call.

        Args:
            query: Search query (exact substring by default, or fuzzy with exact_match=False)
            search_types: Types to search (default: ["automation", "script", "scene", "helper"])
            limit: Maximum total results to return (default: 5)
            exact_match: Use exact substring matching (default: True)

        Public entry-point examples (via ``ha_search``):
            - Find automations referencing an entity: ha_search(query="sensor.temperature")
            - Find with fuzzy matching: ha_search(query="motion", exact_match=False)
            - Find scenes touching a light: ha_search(query="light.kitchen")
            - Search dashboards for entity refs: ha_search(query="sensor.temperature", search_types=["dashboard"])
            - Search everything: ha_search(query="light.bedroom", search_types=["automation","script","scene","helper","dashboard"])
        """
        try:
            parsed_search_types = parse_string_list_param(search_types, "search_types")
        except ValueError as exc:
            raise_tool_error(
                create_validation_error(str(exc), parameter="search_types")
            )
        _validate_search_types(parsed_search_types)
        include_config_bool = include_config
        exact_match_bool = exact_match
        try:
            result = await self._smart_tools.deep_search(
                query,
                parsed_search_types,
                limit,
                offset,
                include_config_bool,
                exact_match=exact_match_bool,
                config_time_budget=config_time_budget,
                ctx=ctx,
                prefetched_states=prefetched_states,
                prefetched_registry=prefetched_registry,
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
            return None  # unreachable: exception_to_structured_error always raises
