"""End-to-end tests for Home Assistant Scene Configuration tools (issue #995).

Validates the lifecycle of ha_config_get_scene / ha_config_set_scene /
ha_config_remove_scene against a real Home Assistant test container:

- Create a scene with a dict-shaped ``entities`` field
- Retrieve and verify ``config_hash`` is stable across reads
- Full-config replacement
- python_transform-based surgical edits
- Optimistic locking on stale config_hash
- Delete and verify removal

Mirrors the shape of ``tests/src/e2e/workflows/scripts/test_lifecycle.py``;
the key shape difference is that scene ``entities`` is a dict keyed by
entity_id, not a list of actions.
"""

import asyncio
import logging
import time
from typing import Any

import pytest

from ...utilities.assertions import safe_call_tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _extract_scene_config(get_data: dict[str, Any]) -> dict[str, Any]:
    """Pull the inner scene body out of the get response.

    The get response wraps the scene config as ``{"config": {"config": <body>}}``
    (REST-wrapper inside the tool wrapper). Mirror the script extractor.
    """
    config_wrapper = get_data.get("config", {})
    if isinstance(config_wrapper, dict) and "config" in config_wrapper:
        return config_wrapper.get("config", {})
    return config_wrapper


async def _wait_for_scene_registered(
    mcp_client, scene_id: str, timeout: int = 15, poll_interval: float = 1.0
) -> bool:
    """Poll until the scene is queryable via the management API or state API."""
    start_time = time.monotonic()
    scene_entity = f"scene.{scene_id}"
    while time.monotonic() - start_time < timeout:
        try:
            get_data = await safe_call_tool(
                mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
            )
            if get_data.get("success") and get_data.get("config"):
                return True

            state_data = await safe_call_tool(
                mcp_client, "ha_get_state", {"entity_id": scene_entity}
            )
            if state_data.get("success"):
                return True
        except Exception as e:
            logger.debug(f"Scene registration check failed: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ Scene {scene_entity} was not registered within {timeout}s")
    return False


async def _wait_for_scene_removed(
    mcp_client, scene_id: str, timeout: int = 15, poll_interval: float = 1.0
) -> bool:
    """Poll until the scene is no longer queryable."""
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            get_data = await safe_call_tool(
                mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
            )
            # Either success=False OR an empty/missing config means it's gone.
            if not get_data.get("success") or not get_data.get("config"):
                return True
        except Exception:
            return True
        await asyncio.sleep(poll_interval)
    return False


def _make_test_scene_config(name: str, **overrides: Any) -> dict[str, Any]:
    """Standard test scene shape: dict-keyed entities + a couple of attributes."""
    config = {
        "name": f"E2E {name}",
        "icon": "mdi:flask",
        "entities": {
            "light.bed_light": {"state": "on", "brightness": 200},
        },
    }
    config.update(overrides)
    return config


@pytest.mark.cleanup
class TestSceneLifecycle:
    """End-to-end coverage for the scene CRUD tools."""

    async def test_scene_basic_lifecycle(self, mcp_client, cleanup_tracker):
        """Create → get → update → delete with stable hash semantics."""
        scene_id = "test_basic_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        # 1. Create
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": _make_test_scene_config("Basic"),
                "wait": True,
            },
        )
        assert create_data.get("success") is True, f"Create failed: {create_data}"

        registered = await _wait_for_scene_registered(mcp_client, scene_id)
        assert registered, f"Scene {scene_id} not registered after create"

        # 2. Get + verify config_hash stability
        get_data_1 = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        assert get_data_1.get("success") is True
        body_1 = _extract_scene_config(get_data_1)
        assert "entities" in body_1
        assert "light.bed_light" in body_1["entities"]
        hash_1 = get_data_1.get("config_hash")
        assert hash_1, "config_hash missing on first get"

        get_data_2 = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        assert get_data_2.get("config_hash") == hash_1, (
            "config_hash should be stable across reads of an unchanged scene"
        )

        # 3. Full-config replacement
        replacement = _make_test_scene_config(
            "Basic Updated",
            entities={
                "light.bed_light": {"state": "on", "brightness": 80},
                "light.kitchen_lights": {"state": "off"},
            },
        )
        update_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": replacement,
                "config_hash": hash_1,
                "wait": True,
            },
        )
        assert update_data.get("success") is True

        get_data_3 = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        body_3 = _extract_scene_config(get_data_3)
        assert "light.kitchen_lights" in body_3.get("entities", {})

        # 4. Delete
        delete_data = await safe_call_tool(
            mcp_client, "ha_config_remove_scene", {"scene_id": scene_id, "wait": True}
        )
        # 405 on add-on / YAML-mode is an acceptable outcome — tested separately.
        if delete_data.get("success"):
            removed = await _wait_for_scene_removed(mcp_client, scene_id)
            assert removed, f"Scene {scene_id} still queryable after delete"

    async def test_scene_python_transform_surgical_edit(
        self, mcp_client, cleanup_tracker
    ):
        """python_transform updates a single entity's state without full replacement."""
        scene_id = "test_transform_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        # Seed
        await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": _make_test_scene_config(
                    "Transform",
                    entities={
                        "light.bed_light": {"state": "on", "brightness": 100},
                    },
                ),
                "wait": True,
            },
        )
        await _wait_for_scene_registered(mcp_client, scene_id)

        get_data = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        scene_hash = get_data.get("config_hash")
        assert scene_hash

        # Surgical edit: bump brightness
        transform_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "python_transform": (
                    "config['entities']['light.bed_light']['brightness'] = 220"
                ),
                "config_hash": scene_hash,
            },
        )
        assert transform_data.get("success") is True
        assert transform_data.get("action") == "python_transform"

        # Verify the change landed
        verify_data = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        body = _extract_scene_config(verify_data)
        assert (
            body.get("entities", {}).get("light.bed_light", {}).get("brightness") == 220
        )

        # Cleanup
        await safe_call_tool(
            mcp_client, "ha_config_remove_scene", {"scene_id": scene_id, "wait": False}
        )

    async def test_scene_python_transform_rejects_stale_hash(
        self, mcp_client, cleanup_tracker
    ):
        """A python_transform call with a stale config_hash returns a conflict error."""
        scene_id = "test_stale_hash_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": _make_test_scene_config("Stale"),
                "wait": True,
            },
        )
        await _wait_for_scene_registered(mcp_client, scene_id)

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "python_transform": (
                    "config['entities']['light.bed_light']['brightness'] = 50"
                ),
                "config_hash": "stale-hash-that-doesnt-match",
            },
        )
        # The tool surfaces the conflict as a structured error.
        assert data.get("success") is False
        err = data.get("error") or {}
        assert "modified" in (err.get("message") or "").lower() or (
            "conflict" in str(data).lower()
        )

        # Cleanup
        await safe_call_tool(
            mcp_client, "ha_config_remove_scene", {"scene_id": scene_id, "wait": False}
        )

    async def test_set_scene_rejects_list_shaped_entities(
        self, mcp_client, cleanup_tracker
    ):
        """Common LLM-misroute: list-shaped entities (the automation/script confusion) is rejected upfront."""
        scene_id = "test_wrong_shape_e2e_scene"
        cleanup_tracker.track("scene", f"scene.{scene_id}")

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": "Wrong shape",
                    # Looks like an automation action list — must be rejected.
                    "entities": [{"entity_id": "light.bed_light", "state": "on"}],
                },
            },
        )
        assert data.get("success") is False
        err = data.get("error") or {}
        msg = (err.get("message") or "").lower()
        assert "dict" in msg and "entities" in msg

    async def test_scene_rename_decouples_entity_id_from_storage_key(
        self, mcp_client, cleanup_tracker
    ):
        """End-to-end coverage for the ``_resolve_scene_entity_id`` flow.

        HA derives a scene's ``entity_id`` from the ``name`` slug rather than
        the storage key. A scene upserted with
        ``scene_id='night_light_led_desk_strip'`` and
        ``name='LED Desk Strip Night Light'`` lands at
        ``scene.led_desk_strip_night_light`` while the registry's
        ``unique_id`` stays ``night_light_led_desk_strip``. This test:

        1. Creates the scene with the diverging shape.
        2. Re-fetches via the storage scene_id and confirms the get works
           (the resolver resolves the entity_id under the hood for category
           lookup; a wrong resolver would surface as a missing/blank
           ``category`` field rather than a 404, but the smoke is the
           same).
        3. Verifies python_transform with config_hash works against the
           storage scene_id even though the entity_id differs — locks the
           wait-and-category path in the python_transform branch that
           BAT validation surfaced as broken before ``_resolve_scene_entity_id``
           landed.
        """
        scene_id = "night_light_led_desk_strip"
        # entity_id below is what HA actually derives from this name
        expected_entity_id = "scene.led_desk_strip_night_light"
        cleanup_tracker.track("scene", expected_entity_id)
        cleanup_tracker.track("scene", f"scene.{scene_id}")  # belt-and-suspenders

        # 1. Create with diverging name vs scene_id
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": "LED Desk Strip Night Light",
                    "icon": "mdi:weather-night",
                    "entities": {
                        "light.bed_light": {"state": "on", "brightness": 30},
                    },
                },
                "wait": True,
            },
        )
        assert create_data.get("success") is True, f"Scene create failed: {create_data}"
        # No 'not yet queryable' warning means the resolver picked up the
        # real entity_id correctly — the regression KP13 surfaced via BAT.
        assert not any(
            "not yet queryable" in w.lower()
            for w in create_data.get("warnings", [])
        ), f"Resolver fell back to scene.{scene_id}; create_data={create_data}"

        # 2. Get via the storage scene_id — this drives the resolver to
        # find the actual entity_id under the hood for category fetch.
        get_data = await safe_call_tool(
            mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
        )
        assert get_data.get("success") is True, f"Get failed: {get_data}"
        config_hash = get_data.get("config_hash")
        assert config_hash, "Get must return a config_hash"

        # 3. python_transform with the storage scene_id must succeed —
        # this exercises the wait-and-category branch in the transform
        # path, which uses _resolve_scene_entity_id internally.
        transform_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "python_transform": (
                    "config['entities']['light.bed_light']['brightness'] = 60"
                ),
                "config_hash": config_hash,
                "wait": True,
            },
        )
        assert transform_data.get("success") is True, (
            f"Transform failed: {transform_data}"
        )
        assert not any(
            "not yet queryable" in w.lower()
            for w in transform_data.get("warnings", [])
        ), f"Resolver fell back on transform path; transform_data={transform_data}"

        # Cleanup via remove uses the same resolver under the hood.
        remove_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_scene",
            {"scene_id": scene_id, "wait": False},
        )
        assert remove_data.get("success") is True, f"Remove failed: {remove_data}"

    async def test_deep_search_to_get_scene_round_trip_on_renamed_scene(
        self, mcp_client, cleanup_tracker
    ):
        """R7 blocker 17/21 end-to-end: ``ha_deep_search`` returns the
        storage key as ``scene_id`` (not the entity-id slug) for a
        renamed scene, and ``ha_config_get_scene`` lands on the same
        scene without relying on the resolver remap.

        Setup mirrors the rename pattern from
        ``test_scene_rename_decouples_entity_id_from_storage_key``:
        storage key ``r7_storkey_alpha`` + name ``R7 Distinct Friendly Round-Trip``
        produces entity_id ``scene.r7_distinct_friendly_round_trip``
        (HA's slugify of the friendly name).

        The contract being verified: ``deep_search`` → ``get_scene`` works
        directly with the returned ``scene_id``. Without R7's slug→storage
        map the deep_search result would carry the entity-id slug, and
        the get-scene call would either 404 or rely on its internal
        resolver to remap — both are slower / less robust paths.
        """
        scene_id = "r7_storkey_alpha"
        cleanup_tracker.track("scene", f"scene.{scene_id}")
        cleanup_tracker.track("scene", "scene.r7_distinct_friendly_round_trip")

        # 1. Create a scene whose entity_id slug diverges from the
        # storage key (the rename pattern that exposes B17).
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_scene",
            {
                "scene_id": scene_id,
                "config": {
                    "name": "R7 Distinct Friendly Round-Trip",
                    "icon": "mdi:magnify",
                    "entities": {
                        "light.bed_light": {"state": "on", "brightness": 50},
                    },
                },
                "wait": True,
            },
        )
        assert create_data.get("success") is True, f"Create failed: {create_data}"

        registered = await _wait_for_scene_registered(mcp_client, scene_id)
        assert registered, f"Scene {scene_id} not registered after create"

        # 2. ha_deep_search by friendly_name fragment.
        search_data = await safe_call_tool(
            mcp_client,
            "ha_deep_search",
            {
                "query": "R7 Distinct Friendly",
                "search_types": ["scene"],
                "limit": 5,
            },
        )
        assert search_data.get("success") is True, f"Search failed: {search_data}"
        scenes = search_data.get("scenes") or []
        match = next(
            (s for s in scenes if s.get("entity_id") == "scene.r7_distinct_friendly_round_trip"),
            None,
        )
        assert match is not None, (
            f"deep_search did not return the test scene; scenes={scenes}"
        )

        # The contract: scene_id is the storage key, not the entity slug.
        assert match["scene_id"] == scene_id, (
            f"deep_search returned scene_id={match['scene_id']!r}, expected "
            f"the storage key {scene_id!r}. The R7 slug→storage map should "
            "have supplied this even when bulk config omits ``id``."
        )

        # 3. Round-trip: feed the returned scene_id into get_scene.
        get_data = await safe_call_tool(
            mcp_client,
            "ha_config_get_scene",
            {"scene_id": match["scene_id"]},
        )
        assert get_data.get("success") is True, (
            f"get_scene round-trip failed on deep_search-returned scene_id "
            f"{match['scene_id']!r}: {get_data}"
        )

        # Cleanup.
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_scene",
            {"scene_id": scene_id, "wait": False},
        )
