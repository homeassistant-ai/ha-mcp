"""Scene-specific deep search: registry walk + per-id config fetch."""

import asyncio
import logging
from typing import Any

from ...client.rest_client import (
    HomeAssistantAPIError,
    SceneResolution,
    SceneStorageConfigNotFoundError,
)
from ...ha_request_queue import limit_ha_transport_request
from ._config import (
    ENTITY_REGISTRY_TIMEOUT,
    INDIVIDUAL_CONFIG_TIMEOUT,
    INDIVIDUAL_FETCH_BATCH_SIZE,
    SCENE_CONFIG_TIME_BUDGET,
)
from ._fetch import (
    ConfigFetchMixin,
    http_500_diagnosis_hint,
    is_timeout_error,
    record_first_failure,
)

logger = logging.getLogger(__name__)


class SceneSearchMixin(ConfigFetchMixin):
    """Scene config search (scenes lack a list primitive; per-id fetch + registry walk)."""

    async def _walk_scene_registry(
        self,
        *,
        prefetched_registry: Any = None,
    ) -> tuple[set[str], dict[str, str], bool]:
        """Walk the entity registry once for scene metadata (Phase 2.5).

        Returns ``(homeassistant_scene_uids, slug_to_storage_id, registry_failed)``.
        Two outputs:

        1. ``homeassistant_scene_uids`` -- unique_ids backed by
           ``platform == "homeassistant"`` (HA's storage collection).
           Integration-managed scenes (Hue, IKEA, deCONZ, ...) are entity-only;
           the per-id REST endpoint ``/config/scene/config/<id>`` can't fetch
           them and treating their 404s as ``failed_count`` produces a
           misleading ``partial: true`` flag (issue #1168 R3 blocker 2).
        2. ``slug_to_storage_id`` -- each scene's entity-id slug mapped to its
           storage key, used by the result builder. HA derives a scene's
           entity_id from the ``name`` field via its own slugify (collapsing
           runs of underscores, replacing all non-alnum with underscores,
           etc.); approximating that with ``.replace()`` chains produces
           near-misses.

        Assumption — caveat for downstream callers: when ``registry_failed``
        is ``False``, the returned ``homeassistant_scene_uids`` set is
        assumed to be COMPLETE — every HA-managed scene the registry knows
        about appears in the set. ``_select_scene_ids_to_fetch`` relies on
        this to classify out-of-set UIDs as integration-managed. If HA ever
        returns a successful-but-truncated ``entity_registry/list`` response
        (no current known case), genuinely-HA-managed scenes whose UIDs are
        missing from the response would be misclassified as
        integration-managed and never fetched. Detecting a truncated
        registry response is not generally possible from its shape — the
        function trusts ``success: True`` as a completeness signal.
        """
        homeassistant_scene_uids: set[str] = set()
        # Issue #1168 R7 blocker 17/21: registry-derived slug->storage map.
        # It decides which scenes are fetched, which storage key each fetch
        # asks for, and the storage key the result reports.
        slug_to_storage_id: dict[str, str] = {}
        try:
            # The ha_search orchestrator may hand us the registry list it already
            # fetched for the entity branch (one list instead of two). A
            # pre-fetched non-success payload flows through the same else-branch
            # below → registry_failed=True, matching a self-fetched soft failure.
            if prefetched_registry is not None:
                reg_resp = prefetched_registry
            else:
                reg_resp = await asyncio.wait_for(
                    self.client.send_websocket_message(
                        {"type": "config/entity_registry/list"}
                    ),
                    timeout=ENTITY_REGISTRY_TIMEOUT,
                )
            if isinstance(reg_resp, dict) and reg_resp.get("success"):
                for entry in reg_resp.get("result") or []:
                    self._index_scene_registry_entry(
                        entry, homeassistant_scene_uids, slug_to_storage_id
                    )
            else:
                # Soft-failure path: `send_websocket_message` returns
                # `{"success": False, "error": ...}` for a command HA rejected,
                # including a post-retry 403 that is not transport death (a
                # dead transport raises instead since #1947). Treat it the same as
                # the raise branch — without the platform filter we cannot
                # tell HA-managed from integration-managed scenes, so route
                # to attempt-all + registry_failed=True. Falling through to
                # `return ..., False` here would produce a fully-complete-
                # looking response with no scene configs.
                logger.warning(
                    "Scene entity-registry list returned non-success: %r; "
                    "integration-platform filter unavailable, attempting all scenes",
                    reg_resp,
                )
                return homeassistant_scene_uids, slug_to_storage_id, True
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

    @staticmethod
    def _select_scene_ids_to_fetch(
        scored: list[tuple[str, str, str | None, int]],
        homeassistant_scene_uids: set[str],
        registry_failed: bool,
        slug_to_storage_id: dict[str, str],
    ) -> tuple[list[str], int]:
        """Pick scene ids needing a per-id fetch, skipping integration-managed ones.

        Issue #1168 R3 blocker 2: integration-managed scenes 404 on the per-id
        REST endpoint by design, so surfacing those as fetch failures masks real
        errors. They are counted separately (returned as ``integration_skipped``).

        Three cases on the registry walk's outcome:

        - ``registry_failed=True`` — the entity-registry call raised; we can't
          tell which scenes are HA-managed, so attempt all (false partials
          beat dropping HA-managed scenes silently).
        - ``registry_failed=False`` with non-empty ``homeassistant_scene_uids``
          — fetch only the HA-managed ones, count integration scenes as
          ``integration_skipped``.
        - ``registry_failed=False`` with empty ``homeassistant_scene_uids``
          — registry succeeded but found zero HA-managed scenes (every scene
          is integration-managed). Attempting them would 404 every single
          one. Skip all per-id fetches and count them as
          ``integration_skipped``.

        The ids in ``scored`` are entity-id SLUGS while
        ``homeassistant_scene_uids`` holds STORAGE keys, and the two diverge
        for any scene renamed in the UI (HA derives the entity_id from the
        scene's ``name``, never re-keying storage). Comparing them directly
        classified every renamed HA scene as integration-managed and skipped
        its fetch, so its config never loaded. ``slug_to_storage_id`` -- built
        by the registry walk for exactly this mapping -- translates first.

        Returns ``(sids_to_fetch, integration_skipped_count)``, in SLUG terms:
        the caller fetches by storage key but keys the result by slug, which
        is what the scoring pass looks up.
        """
        if registry_failed:
            # Registry walk failed — we can't distinguish HA-managed from
            # integration-managed. Attempt all and accept false partials.
            return [sid for _, _, sid, _ in scored if sid], 0
        sids: list[str] = []
        integration_skipped = 0
        for _, _, sid, _ in scored:
            if not sid:
                continue
            if slug_to_storage_id.get(sid, sid) in homeassistant_scene_uids:
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

        Issue #1168 R6/R7 blockers 17/21: three-step resolution:
          1. ``scene_config["id"]`` -- present whenever the per-id config carried it.
          2. ``slug_to_storage_id`` -- registry-derived; covers integration-
             managed scenes and any scene whose config omitted ``id``.
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
            "ha_search scene result fell back to entity-id slug for "
            "scene_id=%r -- neither the per-id config nor the registry walk produced a "
            "storage key. ``ha_config_get_scene`` will rely on its resolver "
            "remap to land on the right scene.",
            scene_id,
        )
        return scene_id

    async def _fetch_one_scene_config(
        self,
        sid: str,
        slug_to_storage_id: dict[str, str],
        failed_errors: list[str],
        *,
        registry_failed: bool,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        """Fetch one scene's storage config and classify the outcome.

        Extracted from ``_deep_search_scenes``'s Attempt-C fetch closure
        (C901). Returns ``(sid, config-or-None, marker)`` where the marker is
        the ``_individual_fetch_budgeted`` classification: ``None`` (success),
        ``"yaml_skipped"``, ``"timeout"`` or ``"failed"``.
        """
        # Hand the client the resolution this scan already made instead of the
        # bare storage key. The client's own resolver treats its input as an
        # entity-id slug ("scene.<input>" registry/state lookups), so a bare
        # storage key that differs from the name-derived slug misses BOTH
        # lookups and every not-a-storage-scene 404 degrades to the bare
        # "vanished" case — round-6 of #2302's no-component lane caught exactly
        # that on the seeded YAML-with-id scene. Passing the resolution keeps
        # the 404 classification working AND drops the per-scene registry
        # round-trip the internal re-resolve used to pay.
        #
        # ONLY when the registry walk succeeded, though: on the failed walk the
        # map is empty, and a synthesized (sid, registry_hit=False) resolution
        # would suppress the client's own per-scene resolver — which may work
        # again by fetch time on a transient list failure, and is the only way
        # a UI-renamed storage scene (storage key != slug) can still resolve.
        # Leaving resolution unset there restores the pre-#2302 per-scene
        # resolve for exactly that path (Codex review on #2302).
        resolution = (
            None
            if registry_failed
            else SceneResolution(
                storage_key=slug_to_storage_id.get(sid, sid),
                registry_hit=sid in slug_to_storage_id,
                platform=None,
            )
        )
        try:
            async with limit_ha_transport_request():
                config_resp = await asyncio.wait_for(
                    # Fetch by STORAGE key (what the endpoint indexes on),
                    # return under the SLUG (what the scoring pass looks up).
                    self.client.get_scene_config(sid, resolution=resolution),
                    timeout=INDIVIDUAL_CONFIG_TIMEOUT,
                )
            return (sid, config_resp.get("config", {}), None)
        except SceneStorageConfigNotFoundError as e:
            # The entity EXISTS, it is just not an editable storage scene:
            # a YAML-defined scene carrying an ``id:`` (which registers
            # under platform ``homeassistant`` exactly like a storage
            # scene, so the registry walk cannot pre-filter it), an
            # ``id``-less YAML scene the client confirmed through the state
            # machine, or an integration-managed (Hue/deCONZ/...) scene.
            # The client decides that classification itself
            # (``_raise_scene_config_404``) from the registry/state lookups
            # it makes for the fetch, independently of the search-side
            # registry filter — which is why this branch stays correct on
            # the registry-failed attempt-all path, where integration-
            # managed scenes DO reach the fetch. Every one of them is a
            # structural gap rather than a fetch outage, so classify it the
            # way the automation/script fetchers do (#2292).
            logger.debug(
                f"Scene individual config fetch ({sid}) returned 404 "
                f"— not a storage scene: {e}"
            )
            return (sid, None, "yaml_skipped")
        except HomeAssistantAPIError as e:
            if e.status_code == 404:
                # A bare 404 (not the subclass above) means the entity is in
                # neither the registry nor the state machine: it vanished
                # between get_states() and this fetch. That IS a real gap in
                # this result, so it belongs in the failed bucket with a
                # representative sample.
                logger.debug(
                    f"Scene individual config fetch ({sid}) returned 404 "
                    "— entity no longer exists since enumeration."
                )
            else:
                logger.debug(f"Scene individual config fetch ({sid}) failed: {e}")
            record_first_failure(failed_errors, e)
            return (sid, None, "failed")
        except TimeoutError:
            # Per-request timeout under batch concurrency — distinct
            # from a real failure; see _fetch_automation_config
            # (#1784).
            logger.debug(
                f"Scene individual config fetch ({sid}) timed out "
                f"after {INDIVIDUAL_CONFIG_TIMEOUT}s."
            )
            return (sid, None, "timeout")
        except Exception as e:
            if is_timeout_error(e):
                # Client-side HTTP timeout arrived wrapped; still a
                # timeout. See is_timeout_error in _fetch.
                logger.debug(
                    f"Scene individual config fetch ({sid}) timed "
                    f"out (client-side HTTP timeout): {e}"
                )
                return (sid, None, "timeout")
            logger.debug(f"Scene individual config fetch ({sid}) failed: {e}")
            record_first_failure(failed_errors, e)
            return (sid, None, "failed")

    async def _deep_search_scenes(
        self,
        all_entities: list[dict[str, Any]],
        query_lower: str,
        exact_match: bool,
        *,
        config_time_budget: float | None = None,
        prefetched_registry: Any = None,
        storage_ids_out: dict[str, str] | None = None,
        graph_entity_ids: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, int, int, bool, int, str | None]:
        """Deep-search scenes: per-id fetch plus registry-walk augmentation.

        Scenes have no listing primitive, so entities are enumerated from
        get_states() and configs fetched per id. Returns the scene results plus
        the seven diagnostic signals feeding the response ``partial`` /
        ``partial_reason``:
        ``(results, failed_count, yaml_skipped_count, skipped_count,
        integration_skipped, registry_failed, timeout_count, failed_sample)``.
        ``failed_sample`` is one representative ``summarize_fetch_error``
        summary of a ``failed``-class exception (``None`` when none occurred)
        — see the automation/script mirror in ``_deep_search_automations``
        (#1784 follow-up).
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

        configs: dict[str, dict[str, Any]] = {}

        # Phase 2.5: registry walk. Must precede the per-id fetch since the
        # integration-skip filter depends on its homeassistant_scene_uids
        # output.
        (
            homeassistant_scene_uids,
            slug_to_storage_id,
            registry_failed,
        ) = await self._walk_scene_registry(prefetched_registry=prefetched_registry)
        # Hand the slug-to-storage-key map back to deep_search, which needs it
        # to give reference-graph scene hits their real storage key. An out
        # parameter rather than a ninth return value: the one production
        # caller and the one test that drive this function unpack its tuple
        # positionally.
        if storage_ids_out is not None:
            storage_ids_out.update(slug_to_storage_id)

        failed_count = 0
        yaml_skipped_count = 0
        skipped_count = 0
        integration_skipped = 0
        timeout_count = 0
        # One representative summary — ``record_first_failure`` (which holds
        # the guard) keeps the first ``failed``-class exception, upgrading
        # once to the first HTTP 500 so a fast-failing outlier can't suppress
        # the 500 diagnosis hint; it rides partial_reason as an ``e.g.``
        # (#1784 follow-up). The remaining failures are counted
        # (``failed_count``) but not summarized.
        failed_errors: list[str] = []

        # Attempt C: parallel per-id fetch with a wall-clock budget so a few
        # slow scenes don't tank the whole search.
        sids_to_fetch, integration_skipped = self._select_scene_ids_to_fetch(
            scored, homeassistant_scene_uids, registry_failed, slug_to_storage_id
        )

        async def _fetch_scene_config(
            sid: str,
        ) -> tuple[str, dict[str, Any] | None, str | None]:
            return await self._fetch_one_scene_config(
                sid, slug_to_storage_id, failed_errors, registry_failed=registry_failed
            )

        (
            fetched_configs,
            failed_count,
            skipped_count,
            yaml_skipped_count,
            timeout_count,
        ) = await self._individual_fetch_budgeted(
            sids_to_fetch,
            _fetch_scene_config,
            config_time_budget
            if config_time_budget is not None
            else SCENE_CONFIG_TIME_BUDGET,
            "Scene",
            "scenes",
            deprioritize={
                entity_id.removeprefix("scene.") for entity_id in graph_entity_ids or ()
            },
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
                    "match_in_references": False,
                    "config": scene_config if scene_config else None,
                }
            )
        return (
            scene_results,
            failed_count,
            yaml_skipped_count,
            skipped_count,
            integration_skipped,
            registry_failed,
            timeout_count,
            failed_errors[0] if failed_errors else None,
        )

    @staticmethod
    def _apply_scene_partial_flag(
        response: dict[str, Any], scene_stats: dict[str, Any]
    ) -> None:
        """Set ``partial``/``partial_reason`` from the scene Attempt-C signals.

        Only set ``partial: True`` when something actually went wrong;
        downstream consumers treat absence as success. Issue #1168 R3 blocker 2:
        integration-managed scenes intentionally skip the per-id fetch and never
        raise ``partial`` on their own (the count is informational).

        Wording uses the same forceful triad as ``_apply_per_type_partial_flag``
        (``not scanned`` / ``match status is unknown`` / ``not exhaustive``)
        so blind agents can't rationalise scene incompleteness any more easily
        than automation/script incompleteness — the softer prior phrasing was
        empirically rationalised away on parallel paths.
        """
        failed = scene_stats["failed"]
        skipped = scene_stats["skipped"]
        # .get(): tolerate older callers/tests that build the stats dict
        # without the timeout key (added for #1784) or without the
        # yaml_skipped key (added for #2292).
        timeout = scene_stats.get("timeout", 0)
        yaml_skipped = scene_stats.get("yaml_skipped", 0)
        if not (failed or yaml_skipped or skipped or timeout):
            return
        response["partial"] = True
        reason_parts: list[str] = []
        if failed:
            # Name ONE representative error inline when Attempt C captured
            # one (.get(): tolerate stats dicts built without the key) —
            # mirrors the automation/script ``e.g.`` sample, #1784 follow-up.
            failed_sample = scene_stats.get("failed_sample")
            sample_suffix = f"; e.g. {failed_sample}" if failed_sample else ""
            # An HTTP 500 sample names the status but not the cause (the body
            # is aiohttp's generic placeholder); append the static HA-log
            # diagnosis, mirroring the automation/script fragment (#1784).
            hint = http_500_diagnosis_hint(failed_sample)
            reason_parts.append(
                f"{failed} scene(s) not scanned (per-id fetch raised"
                f"{sample_suffix}) — "
                f"their match status is unknown; this result is not "
                f"exhaustive.{hint}"
            )
        if yaml_skipped:
            # Structural, not transient, and two kinds land here: a YAML-defined
            # scene with an ``id:`` is indistinguishable from a storage scene in
            # the entity registry, so it survives the integration-platform
            # filter and then 404s on the per-id endpoint; and on the
            # registry-failed attempt-all path integration-managed scenes reach
            # the fetch and 404 the same way. Name both so the reader knows
            # where the definition actually lives. Mirrors the
            # automation/script fragment in ``_apply_per_type_partial_flag``
            # (#2292).
            reason_parts.append(
                f"{yaml_skipped} scene(s) not scanned (per-id config endpoint "
                "returned 404 — these are YAML-defined scenes, or "
                "integration-managed scenes, which that endpoint does not "
                "expose) — their match status is unknown; this result is not "
                "exhaustive. Their definitions live outside HA storage "
                "(typically scenes.yaml or the owning integration); check "
                "there if the match matters."
            )
        if timeout:
            reason_parts.append(
                f"{timeout} scene(s) not scanned (per-id fetch timed out "
                f"after {INDIVIDUAL_CONFIG_TIMEOUT}s while "
                f"{INDIVIDUAL_FETCH_BATCH_SIZE} fetches ran concurrently — "
                "this usually means the HA server serves config reads "
                "serially, not that the scenes are broken) — their match "
                "status is unknown; this result is not exhaustive. Lower "
                "HAMCP_INDIVIDUAL_FETCH_BATCH_SIZE and/or raise "
                "HAMCP_INDIVIDUAL_CONFIG_TIMEOUT (or the matching fields in "
                "the web Settings UI's Advanced section)."
            )
        if skipped:
            reason_parts.append(
                f"{skipped} scene(s) not scanned (time budget exhausted) — "
                "their match status is unknown; this result is not exhaustive. "
                "Pass `config_time_budget=` on `ha_search` to raise the "
                "per-call limit (or, for the default, set "
                "HAMCP_SCENE_CONFIG_TIME_BUDGET or the matching field in the "
                "web Settings UI's Advanced section)."
            )
        if scene_stats["integration_skipped"]:
            # Informational, not an unknown-match-status condition: these
            # scenes are deliberately scored by attribute-only, so their
            # match status is *known* (by name+state), just incomplete.
            reason_parts.append(
                f"{scene_stats['integration_skipped']} integration-managed "
                "scenes are scored by attribute only (no per-id fetch)."
            )
        if scene_stats["registry_failed"]:
            # Issue #1168 R5 blocker 11: when the registry fetch errors, the
            # integration-platform filter is unavailable and Attempt C falls
            # back to attempting all scenes -- surface that so the elevated
            # counts aren't mistaken for a real config outage. Since the 404
            # taxonomy split (#2292) the bucket that swells here is
            # ``yaml_skipped``, not ``failed``: the REST client classifies an
            # integration-managed scene's 404 as
            # ``SceneStorageConfigNotFoundError`` from its own registry/state
            # lookups, which this path's failed registry walk does not affect.
            reason_parts.append(
                "Entity-registry fetch failed; integration-platform filter "
                "unavailable, attempted all scenes (false-positive failures "
                "expected for integration-managed scenes). The registry is "
                "also where a scene's storage key comes from, so the returned "
                "`scene_id` values fall back to the entity-id slug and will "
                "not resolve for a scene that was renamed in the UI."
            )
        # Use the standardised " ; " separator (matches
        # ``_merge_payload_metadata`` and ``_apply_per_type_partial_flag``).
        response["partial_reason"] = " ; ".join(reason_parts)
