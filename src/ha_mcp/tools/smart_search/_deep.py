"""Deep search across automation/script/scene/helper/dashboard configs."""

import asyncio
import logging
import re
from typing import Any

from fastmcp import Context
from fastmcp.exceptions import ToolError

from ...client.rest_client import HomeAssistantAPIError
from ..config_entry_flow import FLOW_HELPER_TYPES
from ..helpers import exception_to_structured_error, safe_info, safe_progress
from ..tools_config_dashboards import fetch_dashboards_list
from ..tools_integrations import fetch_entry_options_with_status
from ._config import (
    AUTOMATION_CONFIG_TIME_BUDGET,
    BULK_REST_TIMEOUT,
    DEFAULT_CONCURRENCY_LIMIT,
    INDIVIDUAL_CONFIG_TIMEOUT,
    INDIVIDUAL_FETCH_BATCH_SIZE,
    SCRIPT_CONFIG_TIME_BUDGET,
)
from ._fetch import (
    http_500_diagnosis_hint,
    is_timeout_error,
    record_first_failure,
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
        config_time_budget: float | None = None,
        ctx: Context | None = None,
        *,
        prefetched_states: list[dict[str, Any]] | None = None,
        prefetched_registry: Any = None,
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
            prefetched_states: Pre-fetched ``get_states()`` list shared by the
                ha_search orchestrator when both search branches run; ``None``
                means fetch here.
            prefetched_registry: Pre-fetched ``config/entity_registry/list``
                response threaded to the scene registry walk *and* the helper
                name-staleness map. ``None`` means the scene walk fetches it
                itself; the helper branch degrades to storage-name matching
                (it never fetches the registry on its own — see
                ``_deep_search_helpers``). The orchestrator only hands one down
                when both the entity and config-body branches run, so direct
                ``deep_search``/``ha_deep_search`` callers get ``None`` here.

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

            # Fetch all entities once at the beginning to avoid repeated calls.
            # The ha_search orchestrator may hand us a snapshot it already fetched
            # for the entity branch so the two branches share one /api/states.
            all_entities = (
                prefetched_states
                if prefetched_states is not None
                else await self.client.get_states()
            )
            phase_done = 1
            await safe_progress(
                ctx,
                progress=phase_done,
                total=total_phases,
                # Config-reference search scans the full state machine by design
                # (the config body is unfiltered), so this count is the scanned
                # universe, not the visible one - phrase it so an operator with
                # the visibility filter on doesn't read it as "the filter is off".
                message=f"scanned {len(all_entities)} entity states for config references",
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
                "timeout": 0,
                "integration_skipped": 0,
                "registry_failed": False,
                "failed_sample": None,
            }

            # Diagnostic counters for the Attempt-C path on automations/
            # scripts and the input_* helper-type gather. Non-zero values
            # surface as ``partial: True`` so callers can detect "incomplete
            # zero" / "incomplete partial" results vs a true complete answer.
            # ``_skipped`` = budget exhausted before fetch; ``_failed`` =
            # fetch attempted but raised (caught at ``debug``-level);
            # ``_timeout`` = fetch exceeded the per-request timeout (#1784).
            automation_skipped = 0
            automation_failed = 0
            automation_yaml_skipped = 0
            automation_timeout = 0
            automation_failed_sample: str | None = None
            script_skipped = 0
            script_failed = 0
            script_yaml_skipped = 0
            script_timeout = 0
            script_failed_sample: str | None = None
            helper_failed = 0
            dashboard_failed = 0

            if "automation" in search_types:
                (
                    results["automations"],
                    automation_skipped,
                    automation_failed,
                    automation_yaml_skipped,
                    automation_timeout,
                    automation_failed_sample,
                ) = await self._deep_search_automations(
                    all_entities,
                    automation_unique_id_map,
                    query_lower,
                    exact_match,
                    config_time_budget=config_time_budget,
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"automations searched ({len(results['automations'])} matches)",
                )

            if "script" in search_types:
                (
                    results["scripts"],
                    script_skipped,
                    script_failed,
                    script_yaml_skipped,
                    script_timeout,
                    script_failed_sample,
                ) = await self._deep_search_scripts(
                    all_entities,
                    query_lower,
                    exact_match,
                    config_time_budget=config_time_budget,
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
                    scene_stats["timeout"],
                    scene_stats["failed_sample"],
                ) = await self._deep_search_scenes(
                    all_entities,
                    query_lower,
                    exact_match,
                    config_time_budget=config_time_budget,
                    prefetched_registry=prefetched_registry,
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"scenes searched ({len(results['scenes'])} matches)",
                )

            if "helper" in search_types:
                (
                    results["helpers"],
                    helper_failed,
                ) = await self._deep_search_helpers(
                    query_lower,
                    exact_match,
                    semaphore,
                    include_config,
                    prefetched_registry=prefetched_registry,
                )
                phase_done += 1
                await safe_progress(
                    ctx,
                    progress=phase_done,
                    total=total_phases,
                    message=f"helpers searched ({len(results['helpers'])} matches)",
                )

            if "dashboard" in search_types:
                (
                    results["dashboards"],
                    dashboard_failed,
                ) = await self._deep_search_dashboards(
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
                automation_skipped=automation_skipped,
                automation_failed=automation_failed,
                automation_yaml_skipped=automation_yaml_skipped,
                automation_timeout=automation_timeout,
                automation_failed_sample=automation_failed_sample,
                script_skipped=script_skipped,
                script_failed=script_failed,
                script_yaml_skipped=script_yaml_skipped,
                script_timeout=script_timeout,
                script_failed_sample=script_failed_sample,
                helper_failed=helper_failed,
                dashboard_failed=dashboard_failed,
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
            return None  # unreachable: exception_to_structured_error raises

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
        *,
        config_time_budget: float | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, int, int, str | None]:
        """Deep-search automations: two-tier config fetch (REST bulk -> budgeted individual).

        Returns ``(matches, skipped_count, failed_count, yaml_skipped_count,
        timeout_count, failed_sample)``. ``skipped_count`` is non-zero only when bulk fetch
        fell back to the per-id Attempt-C path AND its wall-clock budget
        exhausted before all configs were fetched; ``failed_count`` is
        non-zero when individual config fetches raised a non-404, non-timeout
        exception (caught at ``debug``-level); ``yaml_skipped_count`` is
        non-zero when individual config fetches returned 404, which is the
        documented HA behaviour for YAML-defined automations (the
        ``/config/automation/config/<id>`` REST endpoint only exposes
        UI-storage automations); ``timeout_count`` is non-zero when fetches
        exceeded the per-request ``INDIVIDUAL_CONFIG_TIMEOUT`` — usually a
        sign the HA server serves config reads serially and the concurrent
        batch queued past the timeout, not that anything is broken (#1784).
        All four surface as ``partial: True`` in the response so callers can
        distinguish a complete zero-result from an incomplete one —
        skipped/failed ids carry their score-without-config through the
        merge and fall below the match threshold silently otherwise.
        ``failed_sample`` is one representative
        ``summarize_fetch_error`` summary of a ``failed``-class exception
        (``None`` when none occurred), surfaced in the failed fragment so
        the response names WHAT raised instead of pointing at debug logs
        (#1784 follow-up).
        """
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

        # Phase 2: bulk fetch (REST)
        configs = await self._bulk_fetch_configs(
            "/config/automation/config",
            lambda item: item.get("id"),
            BULK_REST_TIMEOUT,
            "Automation",
        )
        bulk_fetched = configs is not None
        if configs is None:
            configs = {}

        # Attempt C: parallel individual REST calls with time budget (LAST RESORT)
        skipped_count = 0
        failed_count = 0
        yaml_skipped_count = 0
        timeout_count = 0
        # One representative summary — only the FIRST ``failed``-class
        # exception is kept (the fetch closure guards the append); it rides
        # partial_reason as an ``e.g.`` (#1784 follow-up). The remaining
        # failures are counted (``failed_count``) but not summarized, so the
        # motivating "every per-id fetch 500s" case does N-1 fewer
        # ``summarize_fetch_error`` calls.
        failed_errors: list[str] = []
        if not bulk_fetched:
            uids_to_fetch = [
                uid for _, _, uid, _ in scored if uid and uid not in configs
            ]

            async def _fetch_automation_config(
                uid: str,
            ) -> tuple[str, dict[str, Any] | None, str | None]:
                try:
                    config = await asyncio.wait_for(
                        self.client._request("GET", f"/config/automation/config/{uid}"),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    return (uid, config, None)
                except HomeAssistantAPIError as e:
                    if e.status_code == 404:
                        # YAML-defined automations 404 on the per-id REST
                        # endpoint by design (it only serves UI-storage
                        # automations). Classify distinctly so the warning
                        # explains the gap is structural, not transient.
                        logger.debug(
                            f"Automation individual config fetch ({uid}) "
                            "returned 404 — likely YAML-defined; not exposed "
                            "via /config/automation/config."
                        )
                        return (uid, None, "yaml_skipped")
                    logger.debug(
                        f"Automation individual config fetch ({uid}) failed: {e}"
                    )
                    record_first_failure(failed_errors, e)
                    return (uid, None, "failed")
                except TimeoutError:
                    # asyncio.wait_for hit INDIVIDUAL_CONFIG_TIMEOUT. Classify
                    # distinctly from "failed": on servers that serialize
                    # config reads, a batch's tail requests queue past the
                    # timeout while still perfectly healthy (#1784).
                    logger.debug(
                        f"Automation individual config fetch ({uid}) timed "
                        f"out after {INDIVIDUAL_CONFIG_TIMEOUT}s."
                    )
                    return (uid, None, "timeout")
                except Exception as e:
                    if is_timeout_error(e):
                        # The REST client's own httpx timeout (HA_TIMEOUT)
                        # fired first and arrived wrapped in a
                        # HomeAssistantConnectionError — still a timeout,
                        # not a failure. See is_timeout_error.
                        logger.debug(
                            f"Automation individual config fetch ({uid}) "
                            f"timed out (client-side HTTP timeout): {e}"
                        )
                        return (uid, None, "timeout")
                    logger.debug(
                        f"Automation individual config fetch ({uid}) failed: {e}"
                    )
                    record_first_failure(failed_errors, e)
                    return (uid, None, "failed")

            (
                fetched_configs,
                failed_count,
                skipped_count,
                yaml_skipped_count,
                timeout_count,
            ) = await self._individual_fetch_budgeted(
                uids_to_fetch,
                _fetch_automation_config,
                config_time_budget
                if config_time_budget is not None
                else AUTOMATION_CONFIG_TIME_BUDGET,
                "Automation",
                "automations",
            )
            configs.update(fetched_configs)

        # Phase 3: Score with whatever configs we have
        matches = [
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
        return (
            matches,
            skipped_count,
            failed_count,
            yaml_skipped_count,
            timeout_count,
            failed_errors[0] if failed_errors else None,
        )

    async def _deep_search_scripts(
        self,
        all_entities: list[dict[str, Any]],
        query_lower: str,
        exact_match: bool,
        *,
        config_time_budget: float | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, int, int, str | None]:
        """Deep-search scripts: same two-tier strategy as automations.

        Returns ``(matches, skipped_count, failed_count, yaml_skipped_count,
        timeout_count, failed_sample)``; semantics identical to ``_deep_search_automations``
        — the 404 path catches YAML-defined scripts (which
        ``client.get_script_config`` re-raises as
        ``HomeAssistantAPIError(status_code=404)``).
        """
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
        skipped_count = 0
        failed_count = 0
        yaml_skipped_count = 0
        timeout_count = 0
        # One representative summary — only the FIRST ``failed``-class
        # exception is kept (the fetch closure guards the append); it rides
        # partial_reason as an ``e.g.`` (#1784 follow-up). The remaining
        # failures are counted (``failed_count``) but not summarized, so the
        # motivating "every per-id fetch 500s" case does N-1 fewer
        # ``summarize_fetch_error`` calls.
        failed_errors: list[str] = []
        if not bulk_fetched:
            sids_to_fetch = [
                sid for _, _, sid, _ in scored if sid and sid not in configs
            ]

            async def _fetch_script_config(
                sid: str,
            ) -> tuple[str, dict[str, Any] | None, str | None]:
                try:
                    config_resp = await asyncio.wait_for(
                        self.client.get_script_config(sid),
                        timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                    )
                    return (sid, config_resp.get("config", {}), None)
                except HomeAssistantAPIError as e:
                    if e.status_code == 404:
                        # YAML-defined scripts 404 on the per-id REST endpoint.
                        # See _fetch_automation_config for the rationale; same
                        # structural-gap class.
                        logger.debug(
                            f"Script individual config fetch ({sid}) returned "
                            "404 — likely YAML-defined; not exposed via "
                            "/config/script/config."
                        )
                        return (sid, None, "yaml_skipped")
                    logger.debug(f"Script individual config fetch ({sid}) failed: {e}")
                    record_first_failure(failed_errors, e)
                    return (sid, None, "failed")
                except TimeoutError:
                    # See _fetch_automation_config: per-request timeout under
                    # batch concurrency, distinct from a real failure (#1784).
                    logger.debug(
                        f"Script individual config fetch ({sid}) timed out "
                        f"after {INDIVIDUAL_CONFIG_TIMEOUT}s."
                    )
                    return (sid, None, "timeout")
                except Exception as e:
                    if is_timeout_error(e):
                        # Client-side HTTP timeout arrived wrapped; still a
                        # timeout. See _fetch_automation_config.
                        logger.debug(
                            f"Script individual config fetch ({sid}) timed "
                            f"out (client-side HTTP timeout): {e}"
                        )
                        return (sid, None, "timeout")
                    logger.debug(f"Script individual config fetch ({sid}) failed: {e}")
                    record_first_failure(failed_errors, e)
                    return (sid, None, "failed")

            (
                fetched_configs,
                failed_count,
                skipped_count,
                yaml_skipped_count,
                timeout_count,
            ) = await self._individual_fetch_budgeted(
                sids_to_fetch,
                _fetch_script_config,
                config_time_budget
                if config_time_budget is not None
                else SCRIPT_CONFIG_TIME_BUDGET,
                "Script",
                "scripts",
            )
            configs.update(fetched_configs)

        # Phase 3: Score scripts
        matches = [
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
        return (
            matches,
            skipped_count,
            failed_count,
            yaml_skipped_count,
            timeout_count,
            failed_errors[0] if failed_errors else None,
        )

    @staticmethod
    def _build_helper_registry_map(
        prefetched_registry: Any,
        helper_types: set[str],
    ) -> dict[str, tuple[str, str | None]]:
        """Map each input_* helper's storage ``unique_id`` to current ``(entity_id, name)``.

        Derived from the entity-registry snapshot the ha_search orchestrator
        already fetched — never fetched here, so the helper branch adds no
        request (see ``_deep_search_helpers``). A UI rename updates only the
        entity registry, so this lets helper scoring match a helper's CURRENT
        entity_id and name while the ``<type>/list`` storage record still
        carries its creation-time slug/name (issue #1794). For a storage helper
        the registry ``unique_id`` equals the ``<type>/list`` record ``id`` and
        ``platform`` equals the helper domain (e.g. ``input_boolean``); the
        current name is the registry ``name`` (user override) falling back to
        ``original_name`` — left ``None`` when both are absent so the caller
        keeps the storage name. Entries with no usable ``unique_id`` /
        ``entity_id``, a non-input_* platform, or a non-dict shape are skipped.

        Returns ``{}`` when no snapshot was handed down (direct ``deep_search``
        callers) or the snapshot is a soft failure — the caller then degrades
        to storage-name-only matching.
        """
        if not (
            isinstance(prefetched_registry, dict) and prefetched_registry.get("success")
        ):
            return {}
        out: dict[str, tuple[str, str | None]] = {}
        for entry in prefetched_registry.get("result") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("platform") not in helper_types:
                continue
            uid = entry.get("unique_id")
            entity_id = entry.get("entity_id")
            if not uid or not entity_id:
                continue
            current_name = entry.get("name") or entry.get("original_name")
            out[uid] = (entity_id, current_name)
        return out

    async def _search_helper_type(
        self,
        helper_type: str,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        *,
        registry_by_uid: dict[str, tuple[str, str | None]] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Fetch one input_* helper type via WS list and return query matches.

        Returns ``(matches, failed)``. ``failed`` is True when the
        ``<type>/list`` backend was unreachable (raised) or returned a
        non-success response — distinct from a successful empty match set.
        A soft ``{"success": False}`` is a backend failure, not a "no
        helpers" signal, so it must surface as ``partial`` rather than be
        swallowed to an empty list: otherwise the caller cannot tell a real
        zero-match from a partial backend outage (helpers run on every
        default ``ha_search`` call).

        ``registry_by_uid`` maps a helper's storage ``unique_id`` to its
        CURRENT ``(entity_id, name)`` from the entity registry (built by
        ``_deep_search_helpers`` from the orchestrator's registry snapshot). A
        UI rename updates only the registry, so the ``<type>/list`` record
        still carries the creation-time entity_id slug and name (issue #1794);
        when the map has an entry we score and emit the current values so a
        search for the renamed name/entity_id matches. The storage name is
        still scored (and the storage body still searched below), so config
        references and un-renamed helpers never regress. ``None`` (direct
        ``deep_search`` callers with no snapshot) degrades to the storage
        values — today's behaviour.
        """
        registry_by_uid = registry_by_uid or {}
        async with semaphore:
            try:
                resp = await self.client.send_websocket_message(
                    {"type": f"{helper_type}/list"}
                )
                # A soft failure (``{"success": False, "error": ...}``) does not
                # raise; match that documented shape explicitly so it surfaces
                # as a backend failure instead of a clean zero-match.
                if isinstance(resp, dict) and resp.get("success") is False:
                    logger.debug(f"{helper_type}/list returned non-success: {resp!r}")
                    return [], True

                matches: list[dict[str, Any]] = []
                for helper in resp.get("result", []):
                    helper_id = helper.get("id", "")
                    storage_name = helper.get("name", helper_id)
                    # Prefer the registry's CURRENT entity_id + name after a UI
                    # rename; fall back to the storage-derived values when this
                    # helper isn't in the snapshot (or no snapshot was handed
                    # down at all).
                    reg_entity_id, reg_name = registry_by_uid.get(
                        helper_id, (None, None)
                    )
                    entity_id = reg_entity_id or f"{helper_type}.{helper_id}"
                    display_name = reg_name or storage_name

                    # Score the query against every name the helper answers to —
                    # its current (registry) name and its storage name — so
                    # neither the rename nor the pre-rename value can hide it.
                    name_match_score = max(
                        self.fuzzy_searcher._calculate_entity_score(
                            entity_id, candidate, helper_type, query_lower
                        )
                        for candidate in {display_name, storage_name}
                    )
                    config_match_score = self._search_in_dict(
                        helper, query_lower, exact_match
                    )
                    total_score, threshold, match_in_name = self._score_deep_match(
                        entity_id,
                        display_name,
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
                                "name": display_name,
                                "score": total_score,
                                "match_in_name": match_in_name,
                                "match_in_config": config_match_score >= threshold,
                                "config": helper,
                            }
                        )
                return matches, False
            except Exception as e:
                logger.debug(f"Could not list {helper_type}: {e}")
                return [], True

    async def _deep_search_helpers(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        include_config: bool,
        *,
        prefetched_registry: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Deep-search helpers: parallel input_* WS lists plus flow-based helpers.

        Returns ``(results, failed_type_count)`` — ``failed_type_count`` counts
        each helper backend that failed: an ``input_*`` ``<type>/list`` fetch
        that raised or returned a non-success response, plus the flow-helper
        config-entries list fetch when it is unreachable or returns an
        unexpected shape, plus each per-entry flow-helper options-flow probe
        that failed (the flow raised or returned a non-form first step — the
        config body was then never searched). Per-entry flow-helper *scoring*
        failures (a code bug processing a response the backend did return) stay
        tolerated inside ``_search_flow_helpers`` (one bad entry must not sink
        the gather) and don't surface here. Helpers run on every default
        ``ha_search`` call, so silent failures here mean the caller cannot
        tell "no helpers match" from "helper backend partially down" —
        surfaced via ``partial: True``.

        ``prefetched_registry`` is the orchestrator's already-fetched
        ``config/entity_registry/list`` response, reused (never re-fetched
        here — this path adds no request) to map each input_* helper's storage
        ``unique_id`` to its CURRENT entity_id + name so a UI rename doesn't
        hide the helper from a search for its new name (#1794). ``None`` on the
        direct ``deep_search``/``ha_deep_search`` path yields an empty map and
        storage-name-only matching.
        """
        helper_types = [
            "input_boolean",
            "input_number",
            "input_select",
            "input_text",
            "input_datetime",
            "input_button",
        ]

        registry_by_uid = self._build_helper_registry_map(
            prefetched_registry, set(helper_types)
        )

        results: list[dict[str, Any]] = []
        failed_type_count = 0
        type_results = await asyncio.gather(
            *[
                self._search_helper_type(
                    ht,
                    query_lower,
                    exact_match,
                    semaphore,
                    registry_by_uid=registry_by_uid,
                )
                for ht in helper_types
            ],
            return_exceptions=True,
        )
        for result in type_results:
            if isinstance(result, tuple):
                type_matches, type_failed = result
                results.extend(type_matches)
                if type_failed:
                    failed_type_count += 1
            elif isinstance(result, Exception):
                failed_type_count += 1
                logger.debug(f"Helper list fetch failed: {result}")

        # Flow-based helpers (template, group, utility_meter, derivative, ...)
        # are config entries, not storage records, and have no `<type>/list`
        # WebSocket endpoint. Pull them via the standard
        # /config/config_entries/entry REST surface and probe each entry's
        # options flow so the helper's current config is searchable alongside
        # the input_* helpers above.
        flow_results, flow_failed_count = await self._search_flow_helpers(
            query_lower,
            exact_match,
            semaphore,
            include_config=include_config,
        )
        results.extend(flow_results)
        failed_type_count += flow_failed_count
        return results, failed_type_count

    async def _search_one_dashboard(
        self,
        url_path: str,
        title: str,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Search a single dashboard's config for the query.

        Returns ``(matches, failed)``. ``failed`` is True when the
        ``lovelace/config`` fetch raised, returned a non-success response, or
        returned a non-dict shape (a backend failure for this dashboard) —
        distinct from a successful no-match. Surfacing it lets
        ``ha_search(search_types=["dashboard"])`` report ``partial`` instead
        of a complete-looking empty result.
        """
        async with semaphore:
            try:
                get_data: dict[str, Any] = {"type": "lovelace/config"}
                if url_path != "default":
                    get_data["url_path"] = url_path
                resp = await asyncio.wait_for(
                    self.client.send_websocket_message(get_data),
                    timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                )
                # A soft failure does NOT raise: send_websocket_message returns
                # {"success": False, "error": ...} on a 403-after-retries or a
                # command error. Without this guard that error envelope is a
                # dict, so it passes the isinstance check below and gets
                # searched as if it were a config — reporting a clean no-match
                # for what is really a backend failure (the same class the
                # scene registry walk handles). Match the documented failure
                # shape explicitly (``success is False``) so a missing-success
                # raw response still falls through to the ``result`` fallback.
                if isinstance(resp, dict) and resp.get("success") is False:
                    logger.debug(
                        f"Dashboard config returned non-success ({url_path}): {resp!r}"
                    )
                    return [], True
                config = resp.get("result", resp) if isinstance(resp, dict) else resp
                if not isinstance(config, dict):
                    logger.debug(
                        f"Dashboard config non-dict shape ({url_path}): "
                        f"{type(config).__name__}"
                    )
                    return [], True

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
                    ], False
                return [], False
            except Exception as e:
                logger.debug(f"Dashboard search failed ({url_path}): {e}")
                return [], True

    async def _deep_search_dashboards(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[dict[str, Any]], int]:
        """Deep-search storage-mode dashboards plus the default dashboard.

        Returns ``(results, failed_count)``. ``failed_count`` counts each
        dashboard surface that failed *without* raising: the registry-list
        fetch returning ``None`` (unexpected/failed shape — previously
        swallowed by ``or []``) plus each per-dashboard ``lovelace/config``
        fetch that raised or returned a non-dict. Routed through
        ``_apply_per_type_partial_flag`` so a failed dashboard backend
        surfaces as ``partial`` instead of a complete-looking empty result.
        The outer ``except`` still re-raises a catastrophic gather failure to
        deep_search's handler.
        """
        try:
            dashboard_entries = await fetch_dashboards_list(self.client)
            # ``None`` = the list fetch hit an unexpected/failed shape
            # (``fetch_dashboards_list`` logs a warning and returns None). The
            # old ``or []`` collapsed that to a clean empty result; count it
            # so the caller sees ``partial`` rather than a silent zero.
            list_failed = dashboard_entries is None
            dashboard_entries = dashboard_entries or []

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
            failed_count = 1 if list_failed else 0
            for dash_result in dash_results:
                if isinstance(dash_result, tuple):
                    dash_matches, dash_failed = dash_result
                    results.extend(dash_matches)
                    if dash_failed:
                        failed_count += 1
                elif isinstance(dash_result, Exception):
                    failed_count += 1
                    logger.debug(f"Dashboard search failed: {dash_result}")
            return results, failed_count

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
        *,
        automation_skipped: int = 0,
        script_skipped: int = 0,
        automation_failed: int = 0,
        script_failed: int = 0,
        automation_yaml_skipped: int = 0,
        script_yaml_skipped: int = 0,
        automation_timeout: int = 0,
        script_timeout: int = 0,
        automation_failed_sample: str | None = None,
        script_failed_sample: str | None = None,
        helper_failed: int = 0,
        dashboard_failed: int = 0,
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
        self._apply_per_type_partial_flag(
            response,
            automation_skipped=automation_skipped,
            script_skipped=script_skipped,
            automation_failed=automation_failed,
            script_failed=script_failed,
            automation_yaml_skipped=automation_yaml_skipped,
            script_yaml_skipped=script_yaml_skipped,
            automation_timeout=automation_timeout,
            script_timeout=script_timeout,
            automation_failed_sample=automation_failed_sample,
            script_failed_sample=script_failed_sample,
            helper_failed=helper_failed,
            dashboard_failed=dashboard_failed,
        )
        return response

    @staticmethod
    def _apply_per_type_partial_flag(
        response: dict[str, Any],
        *,
        automation_skipped: int = 0,
        script_skipped: int = 0,
        automation_failed: int = 0,
        script_failed: int = 0,
        automation_yaml_skipped: int = 0,
        script_yaml_skipped: int = 0,
        automation_timeout: int = 0,
        script_timeout: int = 0,
        automation_failed_sample: str | None = None,
        script_failed_sample: str | None = None,
        helper_failed: int = 0,
        dashboard_failed: int = 0,
    ) -> None:
        """Set ``partial: True`` when the deep-search per-type fetch path lost
        data — either the Attempt-C wall-clock budget exhausted
        (``*_skipped``), individual fetches raised non-404 non-timeout
        exceptions (``*_failed``, caught at ``debug``-level so they would
        otherwise be silent), individual fetches returned 404 because the
        entity is YAML-defined (``*_yaml_skipped``), or individual fetches
        exceeded the per-request timeout (``*_timeout`` — on servers that
        serialize config reads a concurrent batch's tail queues past the
        timeout while perfectly healthy, so the wording points at the
        batch-size/timeout knobs rather than at the entities, #1784).
        ``helper_failed`` / ``dashboard_failed`` cover the helper- and
        dashboard-list/config backends, whose per-unit ``except`` blocks
        would otherwise swallow a backend outage to an empty list with no
        signal.

        Mirrors ``_apply_scene_partial_flag`` 's "looks complete when it
        isn't" coverage onto the automation/script/helper/dashboard paths —
        helpers in particular run on every default ``ha_search`` call, so
        silent per-type-list failures would leave callers unable to tell a
        real zero-match from a partial backend outage.

        The wording is intentionally forceful — every fragment states
        plainly that the entities were *not scanned*, that their match
        status is *unknown*, and that the result *cannot be treated as
        exhaustive*. Earlier, softer wording (``"N failed (per-id fetch
        raised; see server logs at debug level)"``) was empirically
        rationalised away by blind agents who reported the result as
        complete; the harder phrasing closes that gap.

        ``automation_failed_sample`` / ``script_failed_sample`` carry one
        representative ``summarize_fetch_error`` summary for the generic
        ``failed`` class, appended to its fragment as an ``e.g.`` so the
        response names WHAT raised instead of pointing at debug logs
        (#1784 follow-up). When that sample is an HTTP 500 — whose body is
        aiohttp's generic placeholder, so the sample can't name the cause —
        a static HA-log diagnosis is appended too (``http_500_diagnosis_hint``).
        ``None`` keeps the fragment wording unchanged.

        Append-safe: the existing ``partial_reason`` (if any) is preserved
        and the new reasons are concatenated with ``" ; "``.
        """
        reasons: list[str] = []
        # The automation and script fragments are symmetric (noun, per-id
        # endpoint, budget env var); loop rather than duplicating the four
        # per-class fragments per type.
        for (
            noun,
            endpoint,
            budget_env,
            skipped,
            failed,
            yaml_skipped,
            timeout,
            failed_sample,
        ) in (
            (
                "automation",
                "/config/automation/config",
                "HAMCP_AUTOMATION_CONFIG_TIME_BUDGET",
                automation_skipped,
                automation_failed,
                automation_yaml_skipped,
                automation_timeout,
                automation_failed_sample,
            ),
            (
                "script",
                "/config/script/config",
                "HAMCP_SCRIPT_CONFIG_TIME_BUDGET",
                script_skipped,
                script_failed,
                script_yaml_skipped,
                script_timeout,
                script_failed_sample,
            ),
        ):
            if skipped:
                reasons.append(
                    f"{skipped} {noun}(s) not scanned (time budget "
                    "exhausted) — their match status is unknown; this result "
                    "is not exhaustive. Pass `config_time_budget=` on "
                    "`ha_search` to raise the per-call limit (or, for the "
                    f"default, set {budget_env} or the matching field in "
                    "the web Settings UI's Advanced section)."
                )
            if failed:
                # Name ONE representative error inline when the fetch path
                # captured one — the opaque bucket alone sent users on a
                # debug-log dive for trivially-diagnosable server errors
                # (#1784 follow-up: every per-id script fetch 500ing on a
                # ``!secret`` reference in scripts.yaml).
                sample_suffix = f"; e.g. {failed_sample}" if failed_sample else ""
                # An HTTP 500 body is aiohttp's generic "500 Internal Server
                # Error" — the real cause (e.g. a ``!secret`` the per-id
                # config endpoint rejects) is HA-log-only, never in the
                # response — so the sample can't name it. Append the static
                # diagnosis the sample can't carry (#1784 follow-up).
                hint = http_500_diagnosis_hint(failed_sample)
                reasons.append(
                    f"{failed} {noun}(s) not scanned (per-id fetch raised "
                    f"a non-404 error{sample_suffix}) — their match status "
                    f"is unknown; this result is not exhaustive.{hint}"
                )
            if yaml_skipped:
                reasons.append(
                    f"{yaml_skipped} {noun}(s) not scanned (per-id config "
                    "endpoint returned 404 — these are likely YAML-defined "
                    f"{noun}s that the {endpoint} REST endpoint does not "
                    "expose) — their match status is unknown; this result "
                    "is not exhaustive."
                )
            if timeout:
                reasons.append(
                    f"{timeout} {noun}(s) not scanned (per-id fetch timed "
                    f"out after {INDIVIDUAL_CONFIG_TIMEOUT}s while "
                    f"{INDIVIDUAL_FETCH_BATCH_SIZE} fetches ran concurrently "
                    "— this usually means the HA server serves config reads "
                    f"serially, not that the {noun}s are broken) — their "
                    "match status is unknown; this result is not exhaustive. "
                    "Lower HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE and/or raise "
                    "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT (or the matching fields "
                    "in the web Settings UI's Advanced section)."
                )
        if helper_failed:
            reasons.append(
                f"{helper_failed} helper backend(s) not scanned (per-type list, "
                "flow-entries list, or a flow-entry options-probe failed) — "
                "their match status is unknown; this result is not exhaustive."
            )
        if dashboard_failed:
            reasons.append(
                f"{dashboard_failed} dashboard(s) not scanned (config or list "
                "fetch failed) — their match status is unknown; this result is "
                "not exhaustive."
            )
        if not reasons:
            return
        response["partial"] = True
        existing = response.get("partial_reason", "")
        separator = " ; " if existing else ""
        response["partial_reason"] = existing + separator + " ; ".join(reasons)

    async def _search_flow_helpers(
        self,
        query_lower: str,
        exact_match: bool,
        semaphore: asyncio.Semaphore,
        *,
        include_config: bool,
    ) -> tuple[list[dict[str, Any]], int]:
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

        Returns ``(results, failed_count)``. ``failed_count`` counts flow-
        helper backend failures so the caller can route them to ``partial``:
        the whole surface unreachable (config-entries list fetch raised or
        returned an unexpected shape) counts as 1; otherwise it is the number
        of per-entry options-flow probes that failed (the flow raised or
        returned a non-form first step), so a helper whose config body could
        not be read is reported as incomplete rather than a silent clean
        non-match. Per-entry *scoring* failures (a bug processing a response
        the backend did return) are logged at warning and dropped without
        counting — one bad entry must not sink the gather, and a code bug is
        not a backend outage.
        """
        try:
            response = await self.client._request("GET", "/config/config_entries/entry")
        except Exception as exc:
            logger.debug(f"flow-helper search: list_entries failed: {exc}")
            return [], 1

        if not isinstance(response, list):
            logger.debug(
                "flow-helper search: list_entries returned unexpected shape "
                f"({type(response).__name__}); treating as backend failure"
            )
            return [], 1

        flow_entries = [e for e in response if self._is_flow_helper_entry(e)]
        if not flow_entries:
            return [], 0

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
        probe_failures = 0
        for item in scored:
            if isinstance(item, tuple):
                result, probe_failed = item
                if result is not None:
                    out.append(result)
                if probe_failed:
                    probe_failures += 1
            elif isinstance(item, Exception):
                # A scoring/extraction bug (e.g. a shape assumption breaking on
                # a future HA version) — the backend DID respond, our code
                # failed to score it. Log at warning so it's discoverable; do
                # not count it toward partial (it is a code bug, not a backend
                # outage, and the partial_reason wording is backend-specific).
                # One bad entry must not sink the whole multi-source deep_search.
                logger.warning(f"flow-helper scoring failed: {item!r}")
        return out, probe_failures

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
    ) -> tuple[dict[str, Any] | None, bool]:
        """Score one flow-helper config entry, probing its options flow as needed.

        Returns ``(match | None, probe_failed)``. ``probe_failed`` is True when
        the options-flow probe could not read this entry's config body (the
        flow raised or returned a non-form first step) — distinct from a
        genuinely-empty options form. It is reported even when the entry does
        not match (``None``): a probe failure means the config body was never
        searched, so a non-match is a *false* "no match" the caller must
        surface as ``partial`` rather than a clean zero. A malformed entry
        (no string ``entry_id``) is skipped before the probe and is not a
        probe failure.
        """
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str):
            return None, False
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
        probe_failed = False
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
                options, probe_ok = await fetch_entry_options_with_status(
                    self.client, entry_id, quiet=True
                )
                probe_failed = not probe_ok

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
            return None, probe_failed

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
        return result, probe_failed
