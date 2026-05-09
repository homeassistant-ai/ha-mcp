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
        assert "failed" in reason.lower()
        # Integration-managed scenes are surfaced separately so their
        # 100+ count on Hue installs doesn't read as "everything broken".
        assert "integration-managed" in reason.lower(), (
            f"partial_reason should distinguish integration scenes: {reason!r}"
        )
