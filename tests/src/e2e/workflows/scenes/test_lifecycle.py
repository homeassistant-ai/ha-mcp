"""
Scene Lifecycle E2E Tests

Tests the complete scene workflow: List -> Create -> Get -> Activate -> Delete
This represents the most critical user journey for Home Assistant scene management.

Note: Home Assistant scenes are managed via service calls (scene.create, scene.delete)
rather than a WebSocket config API like automations/scripts.
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.scene
@pytest.mark.cleanup
class TestSceneLifecycle:
    """Test complete scene management workflows."""

    async def _find_test_light_entity(self, mcp_client) -> str:
        """
        Find a suitable light entity for testing.

        Prefers demo entities, falls back to any available light.
        Returns entity_id of a suitable light for testing.
        """
        # Search for light entities
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)

        # Handle nested data structure
        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if not results:
            pytest.skip("No light entities available for testing")

        # Prefer demo entities
        for entity in results:
            entity_id = entity.get("entity_id", "")
            if "demo" in entity_id.lower() or "test" in entity_id.lower():
                logger.info(f"Using demo/test light: {entity_id}")
                return entity_id

        # Fall back to first available light
        entity_id = results[0].get("entity_id", "")
        if not entity_id:
            pytest.skip("No valid light entity found for testing")

        logger.info(f"Using first available light: {entity_id}")
        return entity_id

    async def test_list_scenes(self, mcp_client):
        """
        Test: List all scenes

        This test validates that we can retrieve the list of all scenes
        from Home Assistant via the states API.
        """
        logger.info("Testing ha_list_scenes...")

        # List scenes
        result = await mcp_client.call_tool("ha_list_scenes", {})
        data = parse_mcp_result(result)

        # Should succeed even if no scenes exist
        assert data.get("success") is True, f"List scenes failed: {data}"
        assert "count" in data, "Response should include count"
        assert "scenes" in data, "Response should include scenes list"
        assert isinstance(data["scenes"], list), "Scenes should be a list"

        logger.info(f"Found {data['count']} scenes")

    async def test_basic_scene_lifecycle(self, mcp_client, cleanup_tracker):
        """
        Test: Create scene -> Get -> Activate -> Delete

        This test validates the fundamental scene workflow that most
        users will follow when managing Home Assistant scenes.
        """

        # 1. DISCOVER: Find available test entities
        test_light = await self._find_test_light_entity(mcp_client)
        logger.info(f"Using test light entity: {test_light}")

        # 2. CREATE: Basic scene with light state
        scene_id = "test_movie_night_e2e"
        entity_id = f"scene.{scene_id}"
        logger.info(f"Creating scene: {scene_id}")

        create_result = await mcp_client.call_tool(
            "ha_create_scene",
            {
                "scene_id": scene_id,
                "entities": {test_light: {"state": "on", "brightness": 50}},
            },
        )

        create_data = assert_mcp_success(create_result, "scene creation")

        # Extract scene entity_id
        created_entity_id = create_data.get("entity_id")
        assert (
            created_entity_id == entity_id
        ), f"Entity ID mismatch: {created_entity_id}"
        assert create_data.get("operation") == "created", "Should indicate created"

        cleanup_tracker.track("scene", entity_id)
        logger.info(f"Created scene: {entity_id}")

        # 3. GET: Verify scene exists via state query
        await asyncio.sleep(1)  # Allow time for scene to be registered

        logger.info("Verifying scene via ha_get_scene...")
        get_result = await mcp_client.call_tool(
            "ha_get_scene",
            {"entity_id": entity_id},
        )

        get_data = assert_mcp_success(get_result, "scene retrieval")

        assert get_data.get("entity_id") == entity_id, f"Entity ID mismatch: {get_data}"
        logger.info("Scene exists and is queryable")

        # 4. ACTIVATE: Turn on the scene
        logger.info("Activating scene...")
        activate_result = await mcp_client.call_tool(
            "ha_activate_scene",
            {"entity_id": entity_id},
        )

        activate_data = assert_mcp_success(activate_result, "scene activation")
        assert (
            activate_data.get("operation") == "activated"
        ), "Should indicate activated"

        logger.info("Scene activated successfully")

        # 5. DELETE: Remove the test scene
        logger.info("Deleting scene...")
        delete_result = await mcp_client.call_tool(
            "ha_delete_scene",
            {"entity_id": entity_id},
        )

        delete_data = assert_mcp_success(delete_result, "scene deletion")
        assert delete_data.get("operation") == "deleted", "Should indicate deleted"

        logger.info("Scene deleted successfully")

        # 6. VERIFY DELETION: Scene should no longer exist
        await asyncio.sleep(1)

        final_check = await mcp_client.call_tool(
            "ha_get_scene",
            {"entity_id": entity_id},
        )

        final_data = parse_mcp_result(final_check)
        assert not final_data.get(
            "success"
        ), f"Scene should be deleted but still exists: {final_data}"

        logger.info("Scene deletion verified")

    async def test_scene_activation_with_transition(self, mcp_client, cleanup_tracker):
        """
        Test: Create and activate scene with transition time

        This test validates that scenes can be activated with a
        transition time for smooth lighting changes.
        """
        # Find test entity
        test_light = await self._find_test_light_entity(mcp_client)

        # Create scene
        scene_id = "transition_test_e2e"
        entity_id = f"scene.{scene_id}"
        logger.info(f"Creating scene: {scene_id}")

        create_result = await mcp_client.call_tool(
            "ha_create_scene",
            {
                "scene_id": scene_id,
                "entities": {test_light: {"state": "on", "brightness": 100}},
            },
        )

        create_data = assert_mcp_success(create_result, "scene creation")
        cleanup_tracker.track("scene", entity_id)

        # Activate with transition
        logger.info("Activating scene with 2 second transition...")
        activate_result = await mcp_client.call_tool(
            "ha_activate_scene",
            {
                "entity_id": entity_id,
                "transition": 2.0,
            },
        )

        activate_data = assert_mcp_success(
            activate_result, "scene activation with transition"
        )
        assert activate_data.get("transition") == 2.0, "Transition should be 2.0"

        logger.info("Scene activated with transition")

        # Cleanup
        await mcp_client.call_tool("ha_delete_scene", {"entity_id": entity_id})
        logger.info("Test scene cleaned up")

    async def test_scene_with_snapshot_entities(self, mcp_client, cleanup_tracker):
        """
        Test: Create scene by snapshotting current entity states

        This test validates that scenes can be created by capturing
        the current state of entities.
        """
        # Find test entity
        test_light = await self._find_test_light_entity(mcp_client)

        # First, set the light to a known state
        await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": test_light,
            },
        )
        await asyncio.sleep(1)

        # Create scene by snapshotting
        scene_id = "snapshot_test_e2e"
        entity_id = f"scene.{scene_id}"
        logger.info(f"Creating scene by snapshot: {scene_id}")

        create_result = await mcp_client.call_tool(
            "ha_create_scene",
            {
                "scene_id": scene_id,
                "entities": {},
                "snapshot_entities": [test_light],
            },
        )

        create_data = assert_mcp_success(create_result, "snapshot scene creation")
        cleanup_tracker.track("scene", entity_id)

        assert (
            create_data.get("entity_count") >= 1
        ), f"Should have at least 1 entity, got {create_data.get('entity_count')}"

        logger.info("Snapshot scene created successfully")

        # Cleanup
        await mcp_client.call_tool("ha_delete_scene", {"entity_id": entity_id})

    async def test_scene_with_multiple_entities(self, mcp_client, cleanup_tracker):
        """
        Test: Create scene with multiple entities

        This test validates that scenes can control multiple entities
        with different states.
        """
        # Find multiple light entities
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 5},
        )

        search_data = parse_mcp_result(search_result)
        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if len(results) < 2:
            pytest.skip("Need at least 2 light entities for multi-entity test")

        light1 = results[0].get("entity_id")
        light2 = results[1].get("entity_id")

        # Create scene with multiple entities
        scene_id = "multi_entity_test_e2e"
        entity_id = f"scene.{scene_id}"
        logger.info(f"Creating multi-entity scene with: {light1}, {light2}")

        create_result = await mcp_client.call_tool(
            "ha_create_scene",
            {
                "scene_id": scene_id,
                "entities": {
                    light1: {"state": "on", "brightness": 255},
                    light2: {"state": "on", "brightness": 128},
                },
            },
        )

        create_data = assert_mcp_success(create_result, "multi-entity scene creation")
        cleanup_tracker.track("scene", entity_id)

        assert (
            create_data.get("entity_count") == 2
        ), f"Should have 2 entities, got {create_data.get('entity_count')}"

        logger.info("Multi-entity scene created")

        # Activate the scene
        activate_result = await mcp_client.call_tool(
            "ha_activate_scene",
            {"entity_id": entity_id},
        )
        assert_mcp_success(activate_result, "multi-entity scene activation")

        logger.info("Multi-entity scene activated")

        # Cleanup
        await mcp_client.call_tool("ha_delete_scene", {"entity_id": entity_id})


@pytest.mark.scene
async def test_scene_apply(mcp_client):
    """
    Test: Apply scene states without creating a persistent scene

    Validates that ha_apply_scene can set entity states directly
    without creating a scene entity.
    """
    # Find test entity
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "domain_filter": "light", "limit": 1},
    )

    search_data = parse_mcp_result(search_result)
    if "data" in search_data:
        results = search_data.get("data", {}).get("results", [])
    else:
        results = search_data.get("results", [])

    if not results:
        pytest.skip("No light entities available")

    test_light = results[0].get("entity_id")

    logger.info(f"Testing ha_apply_scene with {test_light}...")

    # Apply scene state
    apply_result = await mcp_client.call_tool(
        "ha_apply_scene",
        {
            "entities": {test_light: {"state": "on", "brightness": 128}},
            "transition": 1.0,
        },
    )

    apply_data = assert_mcp_success(apply_result, "scene apply")
    assert apply_data.get("operation") == "applied", "Should indicate applied"
    assert apply_data.get("entity_count") == 1, "Should have 1 entity"

    logger.info("Scene applied successfully")


@pytest.mark.scene
async def test_scene_error_handling(mcp_client):
    """
    Test: Scene error handling

    Validates that scene tools return appropriate errors for
    invalid operations.
    """
    logger.info("Testing scene error handling...")

    # Test getting non-existent scene
    get_result = await mcp_client.call_tool(
        "ha_get_scene",
        {"entity_id": "scene.nonexistent_scene_12345"},
    )

    get_data = parse_mcp_result(get_result)
    assert not get_data.get("success"), "Should fail for non-existent scene"
    assert (
        "not found" in get_data.get("error", "").lower()
        or get_data.get("reason") == "not_found"
    ), f"Should indicate not found: {get_data}"

    logger.info("Non-existent scene handled correctly")


@pytest.mark.scene
async def test_scene_create_validation(mcp_client):
    """
    Test: Scene creation validation

    Validates that scene creation properly validates inputs.
    """
    logger.info("Testing scene creation validation...")

    # Test creating scene with empty entities and no snapshot
    empty_result = await mcp_client.call_tool(
        "ha_create_scene",
        {
            "scene_id": "empty_scene_e2e",
            "entities": {},
        },
    )

    empty_data = parse_mcp_result(empty_result)
    assert not empty_data.get(
        "success"
    ), "Should fail with empty entities and no snapshot"

    logger.info("Empty entities validation works correctly")


@pytest.mark.scene
async def test_apply_scene_validation(mcp_client):
    """
    Test: Apply scene validation

    Validates that apply scene properly validates inputs.
    """
    logger.info("Testing apply scene validation...")

    # Test applying empty entities
    empty_result = await mcp_client.call_tool(
        "ha_apply_scene",
        {"entities": {}},
    )

    empty_data = parse_mcp_result(empty_result)
    assert not empty_data.get("success"), "Should fail with empty entities"
    assert (
        "empty" in empty_data.get("error", "").lower()
    ), f"Should mention empty: {empty_data}"

    logger.info("Apply scene validation works correctly")
