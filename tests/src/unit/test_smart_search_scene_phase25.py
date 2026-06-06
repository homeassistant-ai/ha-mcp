"""Unit tests for the deep_search scene branch's Phase-2.5 entity-registry
augmentation and partial-failure surfacing.

Phase 2.5 covers the case where HA derives a scene's entity_id from its
``name`` slug rather than its storage key — bulk-fetched configs are
keyed by the storage key, so Phase 3 lookup via entity-id slug would
miss them without the registry-driven alias step.

The partial-failure surfacing covers Boy-Scout finding from PR #1168
review: when the Attempt-C per-id fetch hits the wall-clock budget or
records per-id failures, the response now carries ``partial: True``
and a ``partial_reason`` so callers can distinguish "no scene matched"
from "matches may be missing".
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _make_tools(client: Any) -> SmartSearchTools:
    """Construct SmartSearchTools without loading global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


def _make_scene_entity(entity_id: str, friendly_name: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": "scening",
        "attributes": {"friendly_name": friendly_name},
    }


@pytest.mark.asyncio
class TestSceneRegistryAugmentation:
    """Phase-2.5 entity-registry augmentation in the deep_search scene branch.

    The registry maps each scene's storage ``unique_id`` to its actual
    ``entity_id`` (which HA derives from the scene's ``name``). When the two
    diverge (the typical case once a scene is renamed in the UI), the bulk
    fetch keys the config by the storage id while Phase-3 scoring iterates
    by entity_id slug. Phase 2.5 backfills the slug-keyed alias so the
    lookup connects.
    """

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # One scene whose entity_id slug differs from its storage key.
        client.get_states = AsyncMock(
            return_value=[
                _make_scene_entity(
                    "scene.led_desk_strip_night_light",
                    "LED Desk Strip Night Light",
                ),
            ]
        )
        # REST bulk endpoint fails to force WS bulk path.
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))
        return client

    async def test_registry_augmentation_aliases_renamed_scene(
        self, mock_client
    ) -> None:
        """Scene with diverging storage_id and entity_id slug is still
        findable via the entity_id slug after Phase-2.5 augmentation.

        Storage key  : ``night_light_led_desk_strip``
        entity_id    : ``scene.led_desk_strip_night_light``

        Without Phase 2.5 the bulk-fetched config lives at
        ``all_scene_configs['night_light_led_desk_strip']`` while Phase-3
        looks it up at ``all_scene_configs['led_desk_strip_night_light']`` —
        the augmentation backfills the second key as an alias of the first.
        """

        # WS bulk returns the scene config keyed by the *storage* id.
        # Registry list returns the unique_id → entity_id mapping that
        # diverges from the storage id — Phase 2.5 backfills the alias.
        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type in ("config/scene/config/list", "scene/config/list"):
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "night_light_led_desk_strip",
                            "name": "LED Desk Strip Night Light",
                            "entities": {
                                "light.led_desk_strip": {
                                    "state": "on",
                                    "brightness": 30,
                                }
                            },
                        }
                    ],
                }
            if msg_type == "config/entity_registry/list":
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "scene.led_desk_strip_night_light",
                            "unique_id": "night_light_led_desk_strip",
                        }
                    ],
                }
            return {"success": False}

        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="led desk strip",
            search_types=["scene"],
            limit=10,
            include_config=True,
        )

        assert result["success"] is True
        scenes = result.get("scenes", [])
        assert len(scenes) == 1, f"Expected 1 scene match, got: {scenes}"
        match = scenes[0]
        assert match["entity_id"] == "scene.led_desk_strip_night_light"
        # Critical: config is non-None — the augmentation routed the bulk
        # config through the entity_id-slug key successfully.
        assert match.get("config") is not None
        assert "entities" in match["config"]
        assert "light.led_desk_strip" in match["config"]["entities"]

    async def test_registry_augmentation_failure_falls_through(
        self, mock_client
    ) -> None:
        """A failing entity-registry list does not break the scene branch —
        it should swallow the registry exception, leave configs keyed by
        storage_id, and still produce results when storage_id matches the
        entity_id slug (the unrenamed-scene case).
        """
        unrenamed_entity = _make_scene_entity("scene.movie_night", "Movie Night")
        mock_client.get_states = AsyncMock(return_value=[unrenamed_entity])

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type in ("config/scene/config/list", "scene/config/list"):
                return {
                    "success": True,
                    "result": [
                        {
                            "id": "movie_night",
                            "name": "Movie Night",
                            "entities": {
                                "light.living_room": {"state": "off"},
                            },
                        }
                    ],
                }
            if msg_type == "config/entity_registry/list":
                # Augmentation step fails — must not break the branch.
                raise RuntimeError("registry list failed")
            return {"success": False}

        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="movie",
            search_types=["scene"],
            limit=10,
            include_config=True,
        )

        assert result["success"] is True
        scenes = result.get("scenes", [])
        assert len(scenes) == 1, f"Augmentation failure must not lose match: {scenes}"
        # storage_id and entity_id slug coincide, so Phase 3 finds the
        # config via the storage-id key directly.
        assert scenes[0]["config"] is not None


@pytest.mark.asyncio
class TestSceneFetchPartialFailure:
    """Boy-Scout-d coverage: partial-failure surfacing for the scene branch.

    When Attempt C records per-id failures or hits the time budget, the
    response now exposes ``partial: True`` plus a ``partial_reason`` string
    so callers can branch on incomplete results rather than treat a
    missing match as 'no scene matched'.
    """

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                _make_scene_entity("scene.bedroom", "Bedroom"),
                _make_scene_entity("scene.kitchen", "Kitchen"),
            ]
        )
        # Force Phase 1 (REST bulk) and Phase 2 (WS bulk) failures so the
        # branch falls into Attempt C, where per-id failures show up.
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))
        client.send_websocket_message = AsyncMock(
            side_effect=Exception("WS unavailable")
        )
        return client

    async def test_per_id_failures_surface_partial(self, mock_client) -> None:
        """Per-id config fetch failure → response carries ``partial: True``."""

        async def _failing_get(scene_id: str) -> dict[str, Any]:
            raise RuntimeError(f"Mock fetch fail for {scene_id}")

        mock_client.get_scene_config = AsyncMock(side_effect=_failing_get)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="bedroom",
            search_types=["scene"],
            limit=10,
        )

        assert result["success"] is True
        # Both per-id fetches failed, so response must flag partial.
        assert result.get("partial") is True, (
            f"Expected partial=True after per-id failures, got: {result}"
        )
        reason = result.get("partial_reason", "")
        assert "failed" in reason.lower(), (
            f"partial_reason should name the failure mode: {reason!r}"
        )

    async def test_no_partial_flag_when_everything_clean(self, mock_client) -> None:
        """Happy path → no ``partial`` field at all (absence == success)."""
        mock_client.get_scene_config = AsyncMock(
            return_value={
                "config": {
                    "name": "Bedroom",
                    "entities": {"light.bed": {"state": "on"}},
                }
            }
        )
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="bedroom",
            search_types=["scene"],
            limit=10,
        )

        assert result["success"] is True
        assert "partial" not in result, f"Clean fetch must not set partial: {result}"
        assert "partial_reason" not in result


@pytest.mark.asyncio
class TestSceneIntegrationFilter:
    """Issue #1168 R3 blocker 2: integration-managed scenes (Hue, IKEA,
    deCONZ, …) are entity-only — the per-id REST endpoint
    ``/config/scene/config/<id>`` 404s by design, so treating those 404s
    as Attempt-C failures produces a misleading ``partial: True`` flag
    on every install with integration scenes (KP13's Hue test rig hit
    106 of 107 scenes failing). Phase 2.5 reads the registry's
    ``platform`` field to filter Attempt C to HA-managed scenes only;
    integration scenes are scored on attributes alone.
    """

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # One HA-managed scene + two Hue-managed scenes.
        client.get_states = AsyncMock(
            return_value=[
                _make_scene_entity("scene.movie_night", "Movie Night"),
                _make_scene_entity("scene.hue_relax", "Hue Relax"),
                _make_scene_entity("scene.hue_concentrate", "Hue Concentrate"),
            ]
        )
        # Force REST + WS bulk fetches into the no-bulk path so Attempt C
        # runs and the platform filter actually gates per-id fetches.
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))

        async def _ws_message(payload: dict[str, Any]) -> dict[str, Any]:
            msg_type = payload.get("type", "")
            if msg_type == "config/entity_registry/list":
                # Registry surfaces platform info — HA-managed first, then Hue.
                return {
                    "success": True,
                    "result": [
                        {
                            "entity_id": "scene.movie_night",
                            "unique_id": "movie_night",
                            "platform": "homeassistant",
                        },
                        {
                            "entity_id": "scene.hue_relax",
                            "unique_id": "hue_relax",
                            "platform": "hue",
                        },
                        {
                            "entity_id": "scene.hue_concentrate",
                            "unique_id": "hue_concentrate",
                            "platform": "hue",
                        },
                    ],
                }
            # All other WS calls (the bulk-listing attempts) fail to force Attempt C.
            raise Exception(f"WS not configured for {msg_type}")

        client.send_websocket_message = AsyncMock(side_effect=_ws_message)
        return client

    async def test_hue_scenes_not_counted_as_failures(self, mock_client) -> None:
        """Per-id fetch is gated by ``platform == "homeassistant"``: Hue
        scenes are skipped entirely (no 404), and the response is NOT
        flagged partial when the only "missing" scenes were Hue."""

        # Only the HA-managed scene has a per-id config to return.
        async def _get_scene(sid: str) -> dict[str, Any]:
            if sid == "movie_night":
                return {
                    "config": {
                        "name": "Movie Night",
                        "entities": {"light.tv": {"state": "on"}},
                    }
                }
            # Hue scenes would 404 here — but the platform filter must
            # prevent the call from happening in the first place.
            raise AssertionError(
                f"Hue scene {sid!r} must NOT be per-id-fetched (R3 blocker 2)"
            )

        mock_client.get_scene_config = AsyncMock(side_effect=_get_scene)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="hue",  # match the Hue-named scenes
            search_types=["scene"],
            limit=10,
        )

        assert result["success"] is True
        # No ``partial`` flag — the only "missing config" cases are
        # integration-managed (informational, not a fault).
        assert result.get("partial") is not True, (
            f"Hue-only skip must not raise partial: {result}"
        )

    async def test_partial_reason_distinguishes_integration_from_failure(
        self, mock_client
    ) -> None:
        """When BOTH a real failure (HA-managed scene 404) AND
        integration-managed skips happen, ``partial: True`` fires for
        the real failure and the reason names both buckets so an
        operator can tell them apart."""

        async def _get_scene(sid: str) -> dict[str, Any]:
            if sid == "movie_night":
                # The HA-managed scene fails the per-id fetch — real
                # failure, must contribute to partial.
                raise RuntimeError("REST 500 on movie_night")
            raise AssertionError(
                f"Hue scene {sid!r} must NOT be per-id-fetched (R3 blocker 2)"
            )

        mock_client.get_scene_config = AsyncMock(side_effect=_get_scene)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="movie",
            search_types=["scene"],
            limit=10,
        )

        assert result["success"] is True
        assert result.get("partial") is True
        reason = result.get("partial_reason", "")
        # The real failure is named (so the operator knows something broke).
        # Wording strengthened in PR #1529 R5 — every per-type/scene
        # incompleteness fragment carries the "not scanned" triad.
        assert "scene(s) not scanned (per-id fetch raised)" in reason
        assert "match status is unknown" in reason.lower()
        # Integration-managed scenes are surfaced separately so their
        # 100+ count on Hue installs doesn't read as "everything broken".
        assert "integration-managed" in reason.lower(), (
            f"partial_reason should distinguish integration scenes: {reason!r}"
        )


@pytest.mark.asyncio
class TestSceneRegistryFetchFailureSurfacing:
    """R5 blocker 11 — surface registry-fetch failures clearly.

    Before the fix, the catch around ``config/entity_registry/list``
    logged at DEBUG and silently switched to "attempt all scenes"
    mode. A real registry outage looked identical on stderr to the
    steady-state happy path, and the elevated ``failed_count`` from
    integration-managed scenes 404-ing on the per-id endpoint had no
    explanation — partial_reason just said "X failed" with no signal
    the registry was the upstream cause.

    Fix: WARNING log + a flag that threads "integration-platform filter
    unavailable" into ``partial_reason``.
    """

    @pytest.fixture
    def mock_client(self) -> MagicMock:
        client = MagicMock()
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(
            return_value=[
                _make_scene_entity("scene.bedroom", "Bedroom"),
            ]
        )
        client._request = AsyncMock(side_effect=Exception("REST bulk unavailable"))
        return client

    async def test_registry_fetch_failure_logs_warning(
        self, mock_client, caplog
    ) -> None:
        """``config/entity_registry/list`` raising must log at WARNING,
        not DEBUG. Operators rely on stderr WARNING+ for triage; a
        DEBUG-only log meant a true outage was invisible.
        """
        import logging

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type == "config/entity_registry/list":
                raise RuntimeError("simulated registry outage")
            # WS bulk also fails so the branch reaches Attempt C.
            return {"success": False}

        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler)

        async def _failing_get(scene_id: str) -> dict[str, Any]:
            raise RuntimeError(f"Mock fetch fail for {scene_id}")

        mock_client.get_scene_config = AsyncMock(side_effect=_failing_get)
        tools = _make_tools(mock_client)

        caplog.set_level(logging.WARNING, logger="ha_mcp.tools.smart_search")

        await tools.deep_search(query="bedroom", search_types=["scene"], limit=10)

        warn_records = [
            r
            for r in caplog.records
            if "entity-registry augmentation failed" in r.message
            and r.levelno == logging.WARNING
        ]
        assert warn_records, (
            "registry fetch failure must log at WARNING; "
            f"got records={[(r.levelname, r.message) for r in caplog.records]}"
        )

    async def test_registry_fetch_failure_threads_into_partial_reason(
        self, mock_client
    ) -> None:
        """When the registry fetch fails AND Attempt C records per-id
        failures, ``partial_reason`` must explain the registry-fetch
        fallback so the elevated count isn't read as a config outage.
        """

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type == "config/entity_registry/list":
                raise RuntimeError("simulated registry outage")
            return {"success": False}

        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler)

        async def _failing_get(scene_id: str) -> dict[str, Any]:
            raise RuntimeError(f"Mock fetch fail for {scene_id}")

        mock_client.get_scene_config = AsyncMock(side_effect=_failing_get)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="bedroom", search_types=["scene"], limit=10
        )

        assert result.get("partial") is True
        reason = result.get("partial_reason", "")
        assert "registry" in reason.lower() and "fetch failed" in reason.lower(), (
            f"partial_reason must surface the registry-fetch fallback; got {reason!r}"
        )
        # The user-facing wording explains the elevated failed_count.
        assert (
            "filter unavailable" in reason.lower() or "false-positive" in reason.lower()
        ), f"partial_reason must explain the elevated failed_count; got {reason!r}"

    async def test_registry_soft_failure_routes_to_registry_failed(
        self, mock_client
    ) -> None:
        """A non-raising non-success registry response must set ``registry_failed=True``.

        ``RestClient.send_websocket_message`` returns ``{"success": False, ...}``
        on connection drops or post-retry 403s rather than raising. Before the
        fix this took the falsy ``.get("success")`` branch and fell through to
        ``registry_failed=False`` with an empty UID set — every scene was then
        counted as ``integration_skipped`` and the response looked fully
        complete with zero scene configs and no ``partial`` flag. The fix
        routes the soft-failure response to the same attempt-all +
        ``registry_failed=True`` path as the raise branch, so
        ``_apply_scene_partial_flag`` surfaces the registry outage.
        """

        async def _ws_handler(message: dict[str, Any]) -> dict[str, Any]:
            msg_type = message.get("type")
            if msg_type == "config/entity_registry/list":
                # Soft-failure: returned, not raised.
                return {"success": False, "error": "connection lost"}
            # WS bulk also returns soft-failure so we reach Attempt C.
            return {"success": False}

        mock_client.send_websocket_message = AsyncMock(side_effect=_ws_handler)

        async def _failing_get(scene_id: str) -> dict[str, Any]:
            raise RuntimeError(f"Mock fetch fail for {scene_id}")

        mock_client.get_scene_config = AsyncMock(side_effect=_failing_get)
        tools = _make_tools(mock_client)

        result = await tools.deep_search(
            query="bedroom", search_types=["scene"], limit=10
        )

        # Same observable outcome as the raise path: partial=True with the
        # registry-fetch-failed reason surfaced.
        assert result.get("partial") is True, (
            f"soft-failure registry response must set partial=True; got {result}"
        )
        reason = result.get("partial_reason", "")
        assert "registry" in reason.lower() and "fetch failed" in reason.lower(), (
            f"partial_reason must surface the registry-fetch fallback on soft failure; got {reason!r}"
        )


class TestApplyScenePartialFlag:
    """Direct unit coverage of ``_apply_scene_partial_flag``.

    The companion ``_apply_per_type_partial_flag`` has nine focused unit
    tests in ``test_ha_search_merge.py``; the scene equivalent had zero,
    leaving the ``registry_failed`` reason-string branch and the
    integration-skipped no-flag-on-its-own contract unanchored. These
    tests close that gap.
    """

    def test_noop_when_no_failures_or_skips(self) -> None:
        """No failures, no skips → no ``partial`` field at all."""
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        response: dict[str, Any] = {"success": True}
        SceneSearchMixin._apply_scene_partial_flag(
            response,
            {
                "failed": 0,
                "skipped": 0,
                "integration_skipped": 0,
                "registry_failed": False,
            },
        )
        assert "partial" not in response
        assert "partial_reason" not in response

    def test_integration_skipped_alone_does_not_set_partial(self) -> None:
        """Issue #1168 R3 blocker 2: integration-managed scenes intentionally
        skip the per-id fetch and never raise ``partial`` on their own."""
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        response: dict[str, Any] = {"success": True}
        SceneSearchMixin._apply_scene_partial_flag(
            response,
            {
                "failed": 0,
                "skipped": 0,
                "integration_skipped": 7,
                "registry_failed": False,
            },
        )
        assert "partial" not in response
        assert "partial_reason" not in response

    def test_failed_sets_partial_with_failure_reason(self) -> None:
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        response: dict[str, Any] = {"success": True}
        SceneSearchMixin._apply_scene_partial_flag(
            response,
            {
                "failed": 3,
                "skipped": 0,
                "integration_skipped": 0,
                "registry_failed": False,
            },
        )
        assert response["partial"] is True
        reason = response["partial_reason"]
        assert "3 scene(s) not scanned (per-id fetch raised)" in reason
        # Triad — matches the un-rationalisable wording introduced for
        # automation/script paths in PR #1529 R5.
        assert "match status is unknown" in reason
        assert "not exhaustive" in reason

    def test_skipped_sets_partial_with_budget_reason(self) -> None:
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        response: dict[str, Any] = {"success": True}
        SceneSearchMixin._apply_scene_partial_flag(
            response,
            {
                "failed": 0,
                "skipped": 5,
                "integration_skipped": 0,
                "registry_failed": False,
            },
        )
        assert response["partial"] is True
        reason = response["partial_reason"]
        assert "5 scene(s) not scanned (time budget exhausted)" in reason
        assert "match status is unknown" in reason
        assert "not exhaustive" in reason
        assert "HAMCP_SCENE_CONFIG_TIME_BUDGET" in reason

    def test_registry_failed_adds_filter_unavailable_clause(self) -> None:
        """When the registry walk failed (raise OR soft-failure), the
        partial_reason must explain that the integration-platform filter
        is unavailable so an elevated ``failed_count`` isn't read as a
        config outage. Both #1168 R5 blocker 11 and this PR's
        ``_walk_scene_registry`` soft-failure fix depend on this clause.
        """
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        response: dict[str, Any] = {"success": True}
        SceneSearchMixin._apply_scene_partial_flag(
            response,
            {
                "failed": 4,
                "skipped": 0,
                "integration_skipped": 0,
                "registry_failed": True,
            },
        )
        assert response["partial"] is True
        reason = response["partial_reason"].lower()
        assert "entity-registry fetch failed" in reason
        assert "filter unavailable" in reason or "false-positive" in reason

    def test_uses_space_semicolon_space_separator(self) -> None:
        """`" ; "` is the standardised separator across all three partial-
        flag setters (``_merge_payload_metadata``, ``_apply_per_type_partial_flag``,
        ``_apply_scene_partial_flag``). A regression to ``", "`` or
        ``"\\n"`` would pass the substring assertions above but break
        callers that split on the boundary."""
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        response: dict[str, Any] = {"success": True}
        SceneSearchMixin._apply_scene_partial_flag(
            response,
            {
                "failed": 2,
                "skipped": 3,
                "integration_skipped": 4,
                "registry_failed": True,
            },
        )
        reason = response["partial_reason"]
        assert " ; " in reason, (
            f"scene partial_reason fragments must be joined with ' ; '; got {reason!r}"
        )


class TestIndexSceneRegistryEntry:
    """Direct unit coverage of ``_index_scene_registry_entry``.

    The HA-managed vs integration-managed classification (``platform ==
    "homeassistant"``) was previously only tested transitively through
    ``deep_search`` integration scenarios. A regression that flipped the
    membership predicate (e.g. ``not "homeassistant"`` or matching
    substring instead of exact equality) would change downstream
    counting silently.
    """

    @staticmethod
    def _empty_outputs() -> tuple[dict[str, Any], set[str], dict[str, str]]:
        return {}, set(), {}

    def test_homeassistant_platform_entry_recorded_as_managed(self) -> None:
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        configs, uids, slug_map = self._empty_outputs()
        SceneSearchMixin._index_scene_registry_entry(
            {
                "entity_id": "scene.movie_night",
                "unique_id": "movie_night_storage_uid",
                "platform": "homeassistant",
            },
            configs,
            uids,
            slug_map,
        )
        assert "movie_night_storage_uid" in uids
        assert slug_map.get("movie_night") == "movie_night_storage_uid"

    def test_integration_platform_entry_skipped_from_uids_but_aliased(self) -> None:
        """Integration-managed scenes still need their slug alias so the
        result-builder fallback lands on the right storage key — but they
        MUST NOT enter ``homeassistant_scene_uids`` (would round-trip
        through per-id fetch and 404)."""
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        configs, uids, slug_map = self._empty_outputs()
        SceneSearchMixin._index_scene_registry_entry(
            {
                "entity_id": "scene.hue_movie",
                "unique_id": "hue_movie_uid",
                "platform": "hue",
            },
            configs,
            uids,
            slug_map,
        )
        assert "hue_movie_uid" not in uids
        assert slug_map.get("hue_movie") == "hue_movie_uid"

    def test_non_scene_entry_ignored(self) -> None:
        """Non-scene entries (lights, sensors, etc.) in the registry
        response must not pollute the outputs even when ``platform ==
        "homeassistant"``."""
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        configs, uids, slug_map = self._empty_outputs()
        SceneSearchMixin._index_scene_registry_entry(
            {
                "entity_id": "light.kitchen",
                "unique_id": "kitchen_light_uid",
                "platform": "homeassistant",
            },
            configs,
            uids,
            slug_map,
        )
        assert uids == set()
        assert slug_map == {}

    def test_missing_unique_id_skipped(self) -> None:
        """Entries without a ``unique_id`` cannot be aliased and must be
        silently ignored — happens on partially-restored registries."""
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        configs, uids, slug_map = self._empty_outputs()
        SceneSearchMixin._index_scene_registry_entry(
            {
                "entity_id": "scene.partial",
                "unique_id": None,
                "platform": "homeassistant",
            },
            configs,
            uids,
            slug_map,
        )
        assert uids == set()
        assert slug_map == {}

    def test_slug_aliased_to_existing_config_under_storage_key(self) -> None:
        """When a config is already bulk-fetched under its storage key and
        the entity_id slug differs, the registry walk must add a slug-keyed
        alias pointing at the same config dict — that's the whole reason
        Phase 2.5 exists (HA's slugify diverges from `.replace()` chains).
        """
        from ha_mcp.tools.smart_search._scenes import SceneSearchMixin

        # Config bulk-fetched under storage key "ee04b...".
        existing_config = {"id": "ee04b1a2", "name": "Movie Night"}
        configs: dict[str, dict[str, Any]] = {"ee04b1a2": existing_config}
        uids: set[str] = set()
        slug_map: dict[str, str] = {}

        # Registry says: this scene's entity_id is "scene.movie_night" but
        # its unique_id (== storage key) is "ee04b1a2".
        SceneSearchMixin._index_scene_registry_entry(
            {
                "entity_id": "scene.movie_night",
                "unique_id": "ee04b1a2",
                "platform": "homeassistant",
            },
            configs,
            uids,
            slug_map,
        )

        # Phase-3 lookup via slug now lands on the bulk-fetched config.
        assert configs.get("movie_night") is existing_config
