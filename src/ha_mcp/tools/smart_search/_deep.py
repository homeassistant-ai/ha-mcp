"""Deep search across automation/script/scene/helper/dashboard configs."""

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ...utils.fuzzy_search import BM25Scorer, calculate_ratio, tokenize
from ..helpers import exception_to_structured_error, safe_info, safe_progress
from ..tools_config_dashboards import fetch_dashboards_list
from ..tools_config_entry_flow import FLOW_HELPER_TYPES
from ..tools_integrations import fetch_entry_options
from ._base import _SearchBase
from ._config import (
    AUTOMATION_CONFIG_TIME_BUDGET,
    BULK_REST_TIMEOUT,
    BULK_WEBSOCKET_TIMEOUT,
    DEFAULT_CONCURRENCY_LIMIT,
    INDIVIDUAL_CONFIG_TIMEOUT,
    INDIVIDUAL_FETCH_BATCH_SIZE,
    SCENE_CONFIG_TIME_BUDGET,
    SCRIPT_CONFIG_TIME_BUDGET,
)

logger = logging.getLogger(__name__)


class DeepSearchMixin(_SearchBase):
    """``deep_search`` and its config-fetch/scoring helpers."""

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

    @staticmethod
    def _index_configs(
        items: list[dict[str, Any]],
        id_of: Callable[[dict[str, Any]], str | None],
    ) -> dict[str, dict[str, Any]]:
        """Build a ``{id: config}`` map, skipping items with no usable id."""
        configs: dict[str, dict[str, Any]] = {}
        for item in items:
            key = id_of(item)
            if key:
                configs[key] = item
        return configs

    async def _bulk_fetch_configs(
        self,
        rest_endpoint: str,
        ws_types: list[str],
        id_of: Callable[[dict[str, Any]], str | None],
        rest_timeout: float,
        label: str,
    ) -> dict[str, dict[str, Any]] | None:
        """Bulk-fetch all configs of one domain: REST endpoint, then WS list endpoints.

        Returns ``{id: config}`` (possibly empty) on the first successful
        attempt, or ``None`` when every attempt failed. An empty-but-successful
        REST list returns ``{}`` (not ``None``) so the caller skips the
        individual-fetch fallback exactly as it would for a populated response.
        """
        try:
            resp = await asyncio.wait_for(
                self.client._request("GET", rest_endpoint),
                timeout=rest_timeout,
            )
            if isinstance(resp, list):
                return self._index_configs(resp, id_of)
        except Exception as e:
            logger.debug(f"{label} REST bulk fetch failed: {e}")

        for ws_type in ws_types:
            try:
                ws_resp = await asyncio.wait_for(
                    self.client.send_websocket_message({"type": ws_type}),
                    timeout=BULK_WEBSOCKET_TIMEOUT,
                )
                if isinstance(ws_resp, dict) and ws_resp.get("success"):
                    return self._index_configs(ws_resp.get("result", []), id_of)
            except Exception as e:
                logger.debug(f"{label} WebSocket bulk fetch ({ws_type}) failed: {e}")
        return None

    async def _individual_fetch_budgeted(
        self,
        ids: list[str],
        fetch_one: Callable[[str], Awaitable[tuple[str, dict[str, Any] | None]]],
        budget: float,
        label: str,
        plural: str,
    ) -> tuple[dict[str, dict[str, Any]], int, int]:
        """Fetch configs individually in parallel batches under a wall-clock budget.

        ``fetch_one(id)`` returns ``(id, config | None)``. New batches stop
        launching once ``budget`` seconds elapse. Returns
        ``(configs, failed_count, skipped_count)``.

        Fetch order is NOT prioritized by name score: deep_search's purpose is
        to find matches INSIDE configs (conditions/actions), not just by name,
        so name-prioritizing would skip the configs most likely to contain
        non-obvious matches. See #879.
        """
        configs: dict[str, dict[str, Any]] = {}
        budget_start = time.perf_counter()
        total_to_fetch = len(ids)
        fetched_count = 0
        failed_count = 0
        skipped_count = 0
        for i in range(0, len(ids), INDIVIDUAL_FETCH_BATCH_SIZE):
            if time.perf_counter() - budget_start > budget:
                skipped_count = total_to_fetch - fetched_count - failed_count
                logger.warning(
                    f"{label} config fetch budget exhausted ({budget}s). "
                    f"Fetched {fetched_count}/{total_to_fetch} "
                    f"({failed_count} failed), skipped {skipped_count} {plural}."
                )
                break
            batch = ids[i : i + INDIVIDUAL_FETCH_BATCH_SIZE]
            batch_results = await asyncio.gather(*[fetch_one(x) for x in batch])
            for key, config in batch_results:
                if config is not None:
                    configs[key] = config
                    fetched_count += 1
                else:
                    failed_count += 1
        return configs, failed_count, skipped_count

    def _score_config_entries(
        self,
        scored: list[tuple[str, str, str | None, int]],
        configs: dict[str, dict[str, Any]],
        query_lower: str,
        exact_match: bool,
    ) -> list[dict[str, Any]]:
        """Score each ``(entity_id, friendly_name, key, name_score)`` against its config.

        Returns one raw match record per entry clearing its threshold. Each
        per-type caller maps these records into its own result shape.
        """
        matches: list[dict[str, Any]] = []
        for entity_id, friendly_name, key, name_score in scored:
            config = configs.get(key, {}) if key else {}
            config_match_score = (
                self._search_in_dict(config, query_lower, exact_match) if config else 0
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
                matches.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "key": key,
                        "config": config,
                        "score": total_score,
                        "match_in_name": match_in_name,
                        "match_in_config": config_match_score >= threshold,
                    }
                )
        return matches

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

    async def _walk_scene_registry(
        self, configs: dict[str, dict[str, Any]]
    ) -> tuple[set[str], dict[str, str], bool]:
        """Walk the entity registry once for scene metadata (Phase 2.5).

        Returns ``(homeassistant_scene_uids, slug_to_storage_id, registry_failed)``
        and mutates ``configs`` in place, aliasing each bulk-fetched config under
        its entity-id slug. Two outputs:

        1. ``homeassistant_scene_uids`` -- unique_ids backed by
           ``platform == "homeassistant"`` (HA's storage collection).
           Integration-managed scenes (Hue, IKEA, deCONZ, ...) are entity-only;
           the per-id REST endpoint ``/config/scene/config/<id>`` can't fetch
           them and treating their 404s as ``failed_count`` produces a
           misleading ``partial: true`` flag (issue #1168 R3 blocker 2).
        2. Slug-keyed aliases pointing at the bulk-fetched config. HA derives a
           scene's entity_id from the ``name`` field via its own slugify
           (collapsing runs of underscores, replacing all non-alnum with
           underscores, etc.); approximating that with ``.replace()`` chains
           produces near-misses.

        Run unconditionally so the platform filter is available even when the
        bulk fetch returned nothing (the common Hue-only case).
        """
        homeassistant_scene_uids: set[str] = set()
        # Issue #1168 R7 blocker 17/21: registry-derived slug->storage map for
        # the result-builder fallback, keeping the storage key correct for any
        # scene the registry knows about regardless of bulk-fetch coverage.
        slug_to_storage_id: dict[str, str] = {}
        try:
            reg_resp = await asyncio.wait_for(
                self.client.send_websocket_message(
                    {"type": "config/entity_registry/list"}
                ),
                timeout=BULK_WEBSOCKET_TIMEOUT,
            )
            if isinstance(reg_resp, dict) and reg_resp.get("success"):
                for entry in reg_resp.get("result") or []:
                    self._index_scene_registry_entry(
                        entry, configs, homeassistant_scene_uids, slug_to_storage_id
                    )
        except Exception as e:
            # Issue #1168 R5 blocker 11: promote DEBUG -> WARNING and signal the
            # fallback so partial_reason can explain why the count looks
            # elevated. A true registry outage previously looked identical to
            # the steady-state happy path on stderr.
            logger.warning(
                "Scene entity-registry augmentation failed: %s; "
                "integration-platform filter unavailable, attempting all scenes",
                e,
            )
            return homeassistant_scene_uids, slug_to_storage_id, True
        return homeassistant_scene_uids, slug_to_storage_id, False

    @staticmethod
    def _index_scene_registry_entry(
        entry: dict[str, Any],
        configs: dict[str, dict[str, Any]],
        homeassistant_scene_uids: set[str],
        slug_to_storage_id: dict[str, str],
    ) -> None:
        """Record one entity-registry scene entry into the registry-walk outputs."""
        ent_id = entry.get("entity_id") or ""
        uid = entry.get("unique_id")
        if not ent_id.startswith("scene.") or not uid:
            return
        if entry.get("platform") == "homeassistant":
            homeassistant_scene_uids.add(uid)
        slug = ent_id.removeprefix("scene.")
        if slug:
            slug_to_storage_id[slug] = uid
        if uid in configs and slug and slug != uid:
            configs[slug] = configs[uid]

    @staticmethod
    def _select_scene_ids_to_fetch(
        scored: list[tuple[str, str, str | None, int]],
        configs: dict[str, dict[str, Any]],
        homeassistant_scene_uids: set[str],
    ) -> tuple[list[str], int]:
        """Pick scene ids needing a per-id fetch, skipping integration-managed ones.

        Issue #1168 R3 blocker 2: integration-managed scenes 404 on the per-id
        REST endpoint by design, so surfacing those as fetch failures masks real
        errors. They are counted separately (returned as ``integration_skipped``).
        When the registry call failed (``homeassistant_scene_uids`` empty), fall
        back to attempting all scenes -- false partials beat dropping legitimate
        HA-managed scenes silently.

        Returns ``(sids_to_fetch, integration_skipped_count)``.
        """
        if not homeassistant_scene_uids:
            return [sid for _, _, sid, _ in scored if sid and sid not in configs], 0
        sids: list[str] = []
        integration_skipped = 0
        for _, _, sid, _ in scored:
            if not sid or sid in configs:
                continue
            if sid in homeassistant_scene_uids:
                sids.append(sid)
            else:
                integration_skipped += 1
        return sids, integration_skipped

    @staticmethod
    def _resolve_scene_storage_id(
        scene_config: dict[str, Any],
        scene_id: str | None,
        slug_to_storage_id: dict[str, str],
    ) -> str | None:
        """Resolve a scene's storage key (the contract used by ha_config_*_scene).

        Issue #1168 R6/R7 blockers 17/21: three-tier resolution:
          1. ``scene_config["id"]`` -- present whenever the bulk fetch carried it.
          2. ``slug_to_storage_id`` -- registry-derived; covers integration-
             managed scenes and any scene whose bulk record omitted ``id``.
          3. ``scene_id`` itself (the entity-id slug) -- final fallback when the
             registry walk also failed; surfaced via ``logger.warning`` so the
             silent-slug-mismatch path becomes observable.
        """
        config_id = scene_config.get("id") if isinstance(scene_config, dict) else None
        if isinstance(config_id, str):
            return config_id
        if scene_id in slug_to_storage_id:
            return slug_to_storage_id[scene_id]
        logger.warning(
            "ha_deep_search scene result fell back to entity-id slug for "
            "scene_id=%r -- neither bulk config nor registry walk produced a "
            "storage key. ``ha_config_get_scene`` will rely on its resolver "
            "remap to land on the right scene.",
            scene_id,
        )
        return scene_id

    async def _deep_search_scenes(
        self,
        all_entities: list[dict[str, Any]],
        query_lower: str,
        exact_match: bool,
    ) -> tuple[list[dict[str, Any]], int, int, int, bool]:
        """Deep-search scenes: 3-tier strategy plus registry-walk augmentation.

        Scenes have no listing primitive, so entities are enumerated from
        get_states() and configs fetched per id. Returns the scene results plus
        the four signals that drive the response ``partial`` flag:
        ``(results, failed_count, skipped_count, integration_skipped, registry_failed)``.
        """
        scene_entities = [
            e for e in all_entities if e.get("entity_id", "").startswith("scene.")
        ]

        # Phase 1: Score all scenes by name (instant)
        scored: list[tuple[str, str, str | None, int]] = []
        for entity in scene_entities:
            entity_id = entity.get("entity_id", "")
            friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)
            scene_id = entity_id.replace("scene.", "")
            name_score = self.fuzzy_searcher._calculate_entity_score(
                entity_id, friendly_name, "scene", query_lower
            )
            scored.append((entity_id, friendly_name, scene_id, name_score))

        # Phase 2: bulk fetch
        configs = await self._bulk_fetch_configs(
            "/config/scene/config",
            ["config/scene/config/list", "scene/config/list"],
            lambda item: (
                item.get("id") or item.get("name", "").lower().replace(" ", "_")
            ),
            INDIVIDUAL_CONFIG_TIMEOUT,
            "Scene",
        )
        bulk_fetched = configs is not None
        if configs is None:
            configs = {}

        # Phase 2.5: registry walk (runs unconditionally, mutates ``configs``,
        # and must precede Attempt C since the integration-skip filter depends
        # on its homeassistant_scene_uids output).
        (
            homeassistant_scene_uids,
            slug_to_storage_id,
            registry_failed,
        ) = await self._walk_scene_registry(configs)

        failed_count = 0
        skipped_count = 0
        integration_skipped = 0

        # Attempt C: parallel per-id fetch with a wall-clock budget so a few
        # slow scenes don't tank the whole search.
        if not bulk_fetched:
            sids_to_fetch, integration_skipped = self._select_scene_ids_to_fetch(
                scored, configs, homeassistant_scene_uids
            )

            async def _fetch_scene_config(
                sid: str,
            ) -> tuple[str, dict[str, Any] | None]:
                try:
                    config_resp = await asyncio.wait_for(
                        self.client.get_scene_config(sid),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    return (sid, config_resp.get("config", {}))
                except Exception as e:
                    logger.debug(f"Scene individual config fetch ({sid}) failed: {e}")
                    return (sid, None)

            (
                fetched_configs,
                failed_count,
                skipped_count,
            ) = await self._individual_fetch_budgeted(
                sids_to_fetch,
                _fetch_scene_config,
                SCENE_CONFIG_TIME_BUDGET,
                "Scene",
                "scenes",
            )
            configs.update(fetched_configs)

        # Phase 3: Score scenes, resolving each match's storage key
        scene_results: list[dict[str, Any]] = []
        for m in self._score_config_entries(scored, configs, query_lower, exact_match):
            scene_config = m["config"]
            scene_results.append(
                {
                    "entity_id": m["entity_id"],
                    "scene_id": self._resolve_scene_storage_id(
                        scene_config, m["key"], slug_to_storage_id
                    ),
                    "friendly_name": m["friendly_name"],
                    "score": m["score"],
                    "match_in_name": m["match_in_name"],
                    "match_in_config": m["match_in_config"],
                    "config": scene_config if scene_config else None,
                }
            )
        return (
            scene_results,
            failed_count,
            skipped_count,
            integration_skipped,
            registry_failed,
        )

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

    @staticmethod
    def _apply_scene_partial_flag(
        response: dict[str, Any], scene_stats: dict[str, Any]
    ) -> None:
        """Set ``partial``/``partial_reason`` from the scene Attempt-C signals.

        Only set ``partial: True`` when something actually went wrong;
        downstream consumers treat absence as success. Issue #1168 R3 blocker 2:
        integration-managed scenes intentionally skip the per-id fetch and never
        raise ``partial`` on their own (the count is informational).
        """
        failed = scene_stats["failed"]
        skipped = scene_stats["skipped"]
        if not (failed or skipped):
            return
        response["partial"] = True
        reason_parts = [
            f"Scene config fetch incomplete: {failed} failed, "
            f"{skipped} skipped (time budget)."
        ]
        if scene_stats["integration_skipped"]:
            reason_parts.append(
                f" {scene_stats['integration_skipped']} integration-managed "
                "scenes are scored by attribute only (no per-id fetch)."
            )
        if scene_stats["registry_failed"]:
            # Issue #1168 R5 blocker 11: when the registry fetch errors, the
            # integration-platform filter is unavailable and Attempt C falls
            # back to attempting all scenes -- surface that so an elevated
            # failed_count isn't mistaken for a real config outage.
            reason_parts.append(
                " Entity-registry fetch failed; integration-platform filter "
                "unavailable, attempted all scenes (false-positive failures "
                "expected for integration-managed scenes)."
            )
        reason_parts.append(
            " Some scene matches may be missing config data; tune "
            "HAMCP_SCENE_CONFIG_TIME_BUDGET to raise the budget."
        )
        response["partial_reason"] = "".join(reason_parts)

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
        """Search for query in nested dictionary/list structures.

        When exact_match is True, uses substring matching (returns 100 if found, 0 if not).
        When exact_match is False, collects all string leaves, tokenizes them into a
        single BM25 document, and scores against the query tokens.  Falls back to
        token-level SequenceMatcher if BM25 returns 0 (typo correction).
        """
        if exact_match:
            return self._search_in_dict_exact(data, query)

        # Fuzzy path: collect all string leaves, build a single tokenised document
        leaves: list[str] = []
        self._collect_string_leaves(data, leaves)
        if not leaves:
            return 0

        query_tokens = tokenize(query)
        if not query_tokens:
            return 0

        # Build a single flat token list from all leaves
        doc_tokens: list[str] = []
        for leaf in leaves:
            doc_tokens.extend(tokenize(leaf))

        if not doc_tokens:
            return 0

        # Use BM25 with a 1-document corpus (the config dict as a single doc)
        scorer = BM25Scorer()
        scorer.fit([doc_tokens])
        raw = scorer.score(query_tokens, 0)

        if raw > 0:
            # Normalise against the theoretical max (sum of IDF per query
            # token). With a 1-document corpus every token's IDF is identical
            # (~0.288 with smoothing), so the ratio effectively measures how
            # many query tokens the config contains. Cap at 100 for the edge
            # case where high TF pushes raw above the sum-of-IDFs baseline.
            max_possible = scorer.max_possible_score(query_tokens)
            if max_possible > 0:
                return min(100, round(raw / max_possible * 100))
            logger.warning(
                "BM25 scored > 0 but max_possible IDF is 0; "
                "query_tokens=%s, doc_tokens_len=%d",
                query_tokens,
                len(doc_tokens),
            )
            return 100

        # Tier-3 fallback: token-level SequenceMatcher for typos
        logger.debug(
            "BM25 returned 0 for query_tokens=%s; "
            "falling back to SequenceMatcher typo scoring over %d unique tokens",
            query_tokens,
            len(set(doc_tokens)),
        )
        best = 0
        for qt in query_tokens:
            for dt in set(doc_tokens):
                best = max(best, calculate_ratio(qt, dt))
        return best if best >= 70 else 0

    @staticmethod
    def _collect_string_leaves(
        data: dict[str, Any] | list[Any] | Any, out: list[str]
    ) -> None:
        """Recursively collect all string representations from nested data."""
        if isinstance(data, dict):
            for key, value in data.items():
                out.append(str(key))
                DeepSearchMixin._collect_string_leaves(value, out)
        elif isinstance(data, list):
            for item in data:
                DeepSearchMixin._collect_string_leaves(item, out)
        elif isinstance(data, str):
            out.append(data)
        elif data is not None:
            out.append(str(data))

    @classmethod
    def _search_in_dict_exact(
        cls,
        data: dict[str, Any] | list[Any] | Any,
        query: str,
    ) -> int:
        """Exact substring search in nested structures (returns 100 or 0)."""
        if isinstance(data, dict):
            return cls._exact_in_dict(data, query)
        if isinstance(data, list):
            return cls._exact_in_list(data, query)
        if isinstance(data, str):
            return 100 if query in data.lower() else 0
        if data is not None:
            return 100 if query in str(data).lower() else 0
        return 0

    @classmethod
    def _exact_in_dict(cls, data: dict[str, Any], query: str) -> int:
        """Exact-match scan over a dict's keys and recursively over its values."""
        for key, value in data.items():
            if query in str(key).lower():
                return 100
            if cls._search_in_dict_exact(value, query) >= 100:
                return 100
        return 0

    @classmethod
    def _exact_in_list(cls, data: list[Any], query: str) -> int:
        """Exact-match scan recursively over a list's items."""
        for item in data:
            if cls._search_in_dict_exact(item, query) >= 100:
                return 100
        return 0
