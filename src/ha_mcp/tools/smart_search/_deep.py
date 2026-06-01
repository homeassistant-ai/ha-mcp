"""Deep search across automation/script/scene/helper/dashboard configs."""

import asyncio
import logging
import re
from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ..helpers import exception_to_structured_error, safe_info, safe_progress
from ..tools_config_dashboards import fetch_dashboards_list
from ..tools_config_entry_flow import FLOW_HELPER_TYPES
from ..tools_integrations import fetch_entry_options
from ._config import (
    AUTOMATION_CONFIG_TIME_BUDGET,
    BULK_REST_TIMEOUT,
    DEFAULT_CONCURRENCY_LIMIT,
    INDIVIDUAL_CONFIG_TIMEOUT,
    SCRIPT_CONFIG_TIME_BUDGET,
)
from ._scenes import SceneSearchMixin

logger = logging.getLogger(__name__)


class DeepSearchMixin(SceneSearchMixin):
    """deep_search orchestration + per-type automation/script/helper/dashboard/flow search."""

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
