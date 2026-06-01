"""Scene-specific deep search: registry walk + per-id config fetch."""

import asyncio
import logging
from typing import Any

from ._config import (
    BULK_WEBSOCKET_TIMEOUT,
    INDIVIDUAL_CONFIG_TIMEOUT,
    SCENE_CONFIG_TIME_BUDGET,
)
from ._fetch import ConfigFetchMixin

logger = logging.getLogger(__name__)


class SceneSearchMixin(ConfigFetchMixin):
    """Scene config search (scenes lack a list primitive; per-id fetch + registry walk)."""

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
