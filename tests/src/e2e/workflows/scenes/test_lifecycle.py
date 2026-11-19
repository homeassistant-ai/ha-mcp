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
from uuid import uuid4

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool
from ...utilities.wait_helpers import _POLLING_TRANSIENT_ERRORS

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


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
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"Scene registration check failed: {e}")

        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ Scene {scene_entity} was not registered within {timeout}s")
    return False


def _error_code(data: dict[str, Any]) -> str | None:
    error = data.get("error")
    return error.get("code") if isinstance(error, dict) else None


async def _wait_for_scene_removed(
    mcp_client,
    scene_id: str,
    entity_id: str,
    timeout: int = 15,
    poll_interval: float = 1.0,
) -> bool:
    """Poll until ``entity_id``'s state read answers ``ENTITY_NOT_FOUND``, then
    read the config once: it must answer ``ENTITY_NOT_FOUND`` too.

    Polling the config read alone is not enough: it answers ``CONFIG_NOT_FOUND``
    while the entity still lives. Any other failed read, an auth error say, does
    not count as removed. The caller reads the entity before the delete, so a
    wrong ``entity_id`` cannot pass as removed.
    """
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
        try:
            state_data = await safe_call_tool(
                mcp_client, "ha_get_state", {"entity_id": entity_id}
            )
            if _error_code(state_data) == "ENTITY_NOT_FOUND":
                get_data = await safe_call_tool(
                    mcp_client, "ha_config_get_scene", {"scene_id": scene_id}
                )
                return _error_code(get_data) == "ENTITY_NOT_FOUND"
        except _POLLING_TRANSIENT_ERRORS as e:
            logger.debug(f"Scene removal check failed: {e}")
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
        body_1 = get_data_1["config"]
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
        body_3 = get_data_3["config"]
        assert "light.kitchen_lights" in body_3.get("entities", {})

        # 4. Delete
        await MCPAssertions(mcp_client).call_tool_success(
            "ha_get_state", {"entity_id": "scene.e2e_basic"}
        )
        await MCPAssertions(mcp_client).call_tool_success(
            "ha_config_remove_scene", {"scene_id": scene_id, "wait": True}
        )
        removed = await _wait_for_scene_removed(mcp_client, scene_id, "scene.e2e_basic")
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
        body = verify_data["config"]
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

    async def test_remove_names_the_automation_using_the_scene(
        self, mcp_client, cleanup_tracker
    ):
        """Core's related search finds the automation that turns the scene on;
        the scene is removed anyway, with a warning naming it. The unit tests
        set ``search`` up themselves; this one proves a real install has it."""
        scene_id = f"related_e2e_{uuid4().hex[:8]}"
        automation = f"uses_{scene_id}"
        cleanup_tracker.track("scene", f"scene.{scene_id}")
        cleanup_tracker.track("automation", automation)
        mcp = MCPAssertions(mcp_client)
        try:
            await mcp.call_tool_success(
                "ha_config_set_scene",
                {
                    "scene_id": scene_id,
                    # The scene's entity_id comes from its name.
                    "config": {**_make_test_scene_config("Related"), "name": scene_id},
                },
            )
            created = await mcp.call_tool_success(
                "ha_config_set_automation",
                {
                    "entity_id": automation,
                    "config": {
                        "alias": automation,
                        "triggers": [{"trigger": "event", "event_type": automation}],
                        "actions": [
                            {
                                "action": "scene.turn_on",
                                "target": {"entity_id": f"scene.{scene_id}"},
                            }
                        ],
                    },
                },
            )
            await mcp.call_tool_success(
                "ha_get_state", {"entity_id": f"scene.{scene_id}"}
            )
            removed = await mcp.call_tool_success(
                "ha_config_remove_scene", {"scene_id": scene_id}
            )
            # Checked before the cleanup below could remove it a second time.
            automation_id = created["automation_id"]
            assert removed["referenced_by"] == {"automation": [automation_id]}
            assert any(automation_id in w for w in removed["warnings"])
            assert await _wait_for_scene_removed(
                mcp_client, scene_id, f"scene.{scene_id}"
            )
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_config_remove_automation",
                {"entity_id": f"automation.{automation}"},
            )
            await safe_call_tool(
                mcp_client, "ha_config_remove_scene", {"scene_id": scene_id}
            )

    async def test_scene_rename_decouples_entity_id_from_storage_key(
        self, mcp_client, cleanup_tracker
    ):
        """Scene writes wait on the name-derived ID, while reads use the key.

        Both full-config and transform writes exercise post-write resolution;
        config reads and removal exercise the legacy scene resolver.
        """
        scene_id = "night_light_led_desk_strip"
        # entity_id below is what HA actually derives from this name
        expected_entity_id = "scene.led_desk_strip_night_light"
        cleanup_tracker.track("scene", expected_entity_id)
        cleanup_tracker.track("scene", f"scene.{scene_id}")  # belt-and-suspenders

        # 1. Create with diverging name vs scene_id
        async with MCPAssertions(mcp_client) as mcp:
            create_data = await mcp.call_tool_success(
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
            "not yet queryable" in w.lower() for w in create_data.get("warnings", [])
        ), f"Resolver fell back to scene.{scene_id}; create_data={create_data}"

        # wait=True must finish with the name-derived entity queryable.
        # The tool's wait is the completion signal under test: an exception to
        # tests/AGENTS.md's polling rule. A test-side retry would hide early return.
        async with MCPAssertions(mcp_client) as mcp:
            state_data = await mcp.call_tool_success(
                "ha_get_state", {"entity_id": expected_entity_id}
            )
        assert state_data["data"]["entity_id"] == expected_entity_id

        # 2. Get via the storage scene_id — this drives the resolver to
        # find the actual entity_id under the hood for category fetch.
        async with MCPAssertions(mcp_client) as mcp:
            get_data = await mcp.call_tool_success(
                "ha_config_get_scene", {"scene_id": scene_id}
            )
        assert get_data.get("success") is True, f"Get failed: {get_data}"
        config_hash = get_data.get("config_hash")
        assert config_hash, "Get must return a config_hash"

        # 3. python_transform with the storage scene_id must succeed —
        # this exercises post-write resolution and waiting in the transform path.
        async with MCPAssertions(mcp_client) as mcp:
            transform_data = await mcp.call_tool_success(
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
            "not yet queryable" in w.lower() for w in transform_data.get("warnings", [])
        ), f"Resolver fell back on transform path; transform_data={transform_data}"

        # Cleanup via remove uses the same resolver under the hood.
        async with MCPAssertions(mcp_client) as mcp:
            remove_data = await mcp.call_tool_success(
                "ha_config_remove_scene",
                {"scene_id": scene_id, "wait": False},
            )
        assert remove_data.get("success") is True, f"Remove failed: {remove_data}"

    async def test_deep_search_to_get_scene_round_trip_on_renamed_scene(
        self, mcp_client, cleanup_tracker
    ):
        """R7 blocker 17/21 end-to-end: ``ha_search`` returns the
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

        # 2. ha_search by friendly_name fragment.
        search_data = await safe_call_tool(
            mcp_client,
            "ha_search",
            {
                "query": "R7 Distinct Friendly",
                "search_types": ["scene"],
                "limit": 5,
            },
        )
        assert search_data.get("success") is True, f"Search failed: {search_data}"
        scenes = search_data.get("scenes") or []
        match = next(
            (
                s
                for s in scenes
                if s.get("entity_id") == "scene.r7_distinct_friendly_round_trip"
            ),
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

    async def test_capture_of_a_differently_cased_id_replaces_its_entry(
        self, mcp_client, cleanup_tracker
    ):
        """Capturing ``LIGHT.BED_LIGHT`` replaces the stored ``light.bed_light``
        entry and keeps its metadata: readback holds one entry for the light.
        The unit tests pin the same property; this one sends capture_entities
        through the MCP schema."""
        scene_id = f"capture_case_{uuid4().hex[:8]}"
        cleanup_tracker.track("scene", f"scene.{scene_id}")
        mcp = MCPAssertions(mcp_client)
        try:
            await mcp.call_tool_success(
                "ha_config_set_scene",
                {
                    "scene_id": scene_id,
                    "config": {
                        "name": scene_id,
                        "entities": {
                            "light.bed_light": {"state": "off", "brightness": 5}
                        },
                        "metadata": {"light.bed_light": {"entity_only": False}},
                    },
                    "capture_entities": ["LIGHT.BED_LIGHT"],
                },
            )
            stored = await mcp.call_tool_success(
                "ha_config_get_scene", {"scene_id": scene_id}
            )
            assert list(stored["config"]["entities"]) == ["light.bed_light"]
            # The supplied entry had no attributes; the captured state has them.
            assert "friendly_name" in stored["config"]["entities"]["light.bed_light"]
            assert stored["config"]["metadata"] == {
                "light.bed_light": {"entity_only": False}
            }
        finally:
            await safe_call_tool(
                mcp_client, "ha_config_remove_scene", {"scene_id": scene_id}
            )

    @pytest.mark.requires_component_surface
    async def test_a_captured_device_is_saved_as_the_scene_editor_saves_it(
        self, mcp_client, cleanup_tracker
    ):
        """capture_devices saves the device's light without entity_only, as the
        editor saves a picked device. The membership rules are pinned in the
        unit tests; this one sends capture_devices through the MCP schema to
        the component's device read."""
        scene_id = f"capture_device_{uuid4().hex[:8]}"
        cleanup_tracker.track("scene", f"scene.{scene_id}")
        mcp = MCPAssertions(mcp_client)
        entity = await mcp.call_tool_success(
            "ha_get_entity", {"entity_id": "light.bed_light"}
        )
        device_id = entity["entity_entry"]["device_id"]
        try:
            await mcp.call_tool_success(
                "ha_config_set_scene",
                {
                    "scene_id": scene_id,
                    "config": {"name": scene_id},
                    "capture_devices": [device_id],
                },
            )
            stored = await mcp.call_tool_success(
                "ha_config_get_scene", {"scene_id": scene_id}
            )
            assert "light.bed_light" in stored["config"]["entities"]
            assert "light.bed_light" not in stored["config"].get("metadata", {})
        finally:
            await safe_call_tool(
                mcp_client, "ha_config_remove_scene", {"scene_id": scene_id}
            )
