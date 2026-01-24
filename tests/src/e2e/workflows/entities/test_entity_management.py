"""
E2E tests for entity management tools.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, parse_mcp_result
from tests.src.e2e.utilities.cleanup import (
    TestEntityCleaner as EntityCleaner,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.registry
class TestEntityManagement:
    """Test entity management operations."""

    async def test_set_entity_assign_area(self, mcp_client, cleanup_tracker):
        """Test assigning an entity to an area using ha_set_entity."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper for entity
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Set Entity Area Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        logger.info(f"Created test entity: {entity_id}")

        # Create a test area
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": "E2E Test Room", "icon": "mdi:room"},
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        cleanup_tracker.track("area", area_id)

        logger.info(f"Created test area: {area_id}")

        # Assign entity to area
        update_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "area_id": area_id},
        )
        update_data = assert_mcp_success(update_result, "Assign entity to area")
        assert update_data.get("entity_entry", {}).get("area_id") == area_id, (
            f"Area not assigned: {update_data}"
        )

        logger.info(f"Entity assigned to area: {area_id}")

        # Cleanup
        await cleaner.cleanup_all()
        await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})

    async def test_set_entity_clear_area(self, mcp_client, cleanup_tracker):
        """Test clearing area assignment using empty string."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Clear Area Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Create and assign to area
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": "E2E Clear Area Room", "icon": "mdi:room"},
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        cleanup_tracker.track("area", area_id)

        # Assign entity to area first
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "area_id": area_id},
        )

        # Clear area using empty string
        clear_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "area_id": ""},
        )
        clear_data = assert_mcp_success(clear_result, "Clear entity area")
        assert clear_data.get("entity_entry", {}).get("area_id") is None, (
            f"Area not cleared: {clear_data}"
        )

        logger.info("Area assignment cleared successfully")

        # Cleanup
        await cleaner.cleanup_all()
        await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})

    async def test_set_entity_name_and_icon(self, mcp_client, cleanup_tracker):
        """Test updating entity name and icon."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Name Icon Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Update name and icon
        update_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "name": "Custom Display Name",
                "icon": "mdi:lightbulb",
            },
        )
        update_data = assert_mcp_success(update_result, "Update name and icon")

        entity_entry = update_data.get("entity_entry", {})
        assert entity_entry.get("name") == "Custom Display Name", (
            f"Name not updated: {entity_entry}"
        )
        assert entity_entry.get("icon") == "mdi:lightbulb", (
            f"Icon not updated: {entity_entry}"
        )

        logger.info("Name and icon updated successfully")

        # Clear name and icon using empty strings
        clear_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "name": "", "icon": ""},
        )
        clear_data = assert_mcp_success(clear_result, "Clear name and icon")

        cleared_entry = clear_data.get("entity_entry", {})
        assert cleared_entry.get("name") is None, f"Name not cleared: {cleared_entry}"
        assert cleared_entry.get("icon") is None, f"Icon not cleared: {cleared_entry}"

        logger.info("Name and icon cleared successfully")

        # Cleanup
        await cleaner.cleanup_all()

    async def test_set_entity_nonexistent(self, mcp_client):
        """Test error handling for non-existent entity in ha_set_entity."""
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": "sensor.nonexistent_entity_xyz", "name": "Test Name"},
        )
        data = parse_mcp_result(result)
        assert not data.get("success", False), "Should fail for non-existent entity"

        logger.info("Non-existent entity error handling verified")

    async def test_set_entity_aliases(self, mcp_client, cleanup_tracker):
        """Test setting aliases as string lists."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Aliases Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Set aliases
        aliases = ["test alias one", "test alias two"]

        update_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "aliases": aliases,
            },
        )
        update_data = assert_mcp_success(update_result, "Set aliases")

        entity_entry = update_data.get("entity_entry", {})
        returned_aliases = entity_entry.get("aliases", [])

        assert set(aliases) == set(returned_aliases), (
            f"Aliases mismatch: expected {aliases}, got {returned_aliases}"
        )

        logger.info(f"Aliases set: {returned_aliases}")

        # Test clearing aliases with empty list
        clear_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "aliases": [],
            },
        )
        clear_data = assert_mcp_success(clear_result, "Clear aliases")

        cleared_entry = clear_data.get("entity_entry", {})
        assert len(cleared_entry.get("aliases", [])) == 0, (
            f"Aliases not cleared: {cleared_entry}"
        )

        logger.info("Aliases cleared successfully")

        # Cleanup
        await cleaner.cleanup_all()

    async def test_set_entity_enabled(self, mcp_client, cleanup_tracker):
        """Test disabling and enabling entity via enabled parameter."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Disable Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Disable entity using enabled=False
        disable_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "enabled": False},
        )
        disable_data = assert_mcp_success(disable_result, "Disable entity")
        assert disable_data.get("entity_entry", {}).get("disabled_by") == "user", (
            f"Entity not disabled: {disable_data}"
        )

        logger.info("Entity disabled via enabled=False")

        # Re-enable entity using enabled=True
        enable_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "enabled": True},
        )
        enable_data = assert_mcp_success(enable_result, "Enable entity")
        assert enable_data.get("entity_entry", {}).get("disabled_by") is None, (
            f"Entity not enabled: {enable_data}"
        )

        logger.info("Entity enabled via enabled=True")

        # Cleanup
        await cleaner.cleanup_all()

    async def test_set_entity_hidden(self, mcp_client, cleanup_tracker):
        """Test hiding and unhiding entity via hidden parameter."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Hidden Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Hide entity using hidden=True
        hide_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "hidden": True},
        )
        hide_data = assert_mcp_success(hide_result, "Hide entity")
        assert hide_data.get("entity_entry", {}).get("hidden_by") == "user", (
            f"Entity not hidden: {hide_data}"
        )

        logger.info("Entity hidden via hidden=True")

        # Unhide entity using hidden=False
        unhide_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "hidden": False},
        )
        unhide_data = assert_mcp_success(unhide_result, "Unhide entity")
        assert unhide_data.get("entity_entry", {}).get("hidden_by") is None, (
            f"Entity not unhidden: {unhide_data}"
        )

        logger.info("Entity unhidden via hidden=False")

        # Cleanup
        await cleaner.cleanup_all()

    async def test_get_entity_single(self, mcp_client, cleanup_tracker):
        """Test ha_get_entity with single entity lookup and full field verification."""
        cleaner = EntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Get Entity Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Create a test area
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": "E2E Get Entity Room", "icon": "mdi:room"},
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        cleanup_tracker.track("area", area_id)

        # Set properties using ha_set_entity
        test_aliases = ["test alias", "another alias"]
        await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "area_id": area_id,
                "name": "Custom Get Entity Name",
                "icon": "mdi:lightbulb",
                "aliases": test_aliases,
            },
        )

        # Call ha_get_entity with single entity_id
        get_result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": entity_id},
        )
        get_data = assert_mcp_success(get_result, "Get single entity")

        # Verify response structure
        assert get_data.get("entity_id") == entity_id, f"entity_id mismatch: {get_data}"
        assert "entity_entry" in get_data, f"Missing entity_entry: {get_data}"

        entity_entry = get_data["entity_entry"]

        # Verify all expected fields
        assert entity_entry.get("entity_id") == entity_id, (
            f"entity_entry.entity_id mismatch: {entity_entry}"
        )
        assert entity_entry.get("name") == "Custom Get Entity Name", (
            f"name mismatch: {entity_entry}"
        )
        assert entity_entry.get("icon") == "mdi:lightbulb", (
            f"icon mismatch: {entity_entry}"
        )
        assert entity_entry.get("area_id") == area_id, (
            f"area_id mismatch: {entity_entry}"
        )

        # Verify disabled_by/hidden_by and translated booleans
        assert entity_entry.get("disabled_by") is None, (
            f"disabled_by should be None: {entity_entry}"
        )
        assert entity_entry.get("hidden_by") is None, (
            f"hidden_by should be None: {entity_entry}"
        )
        assert entity_entry.get("enabled") is True, (
            f"enabled should be True when disabled_by is None: {entity_entry}"
        )
        assert entity_entry.get("hidden") is False, (
            f"hidden should be False when hidden_by is None: {entity_entry}"
        )

        # Verify aliases
        returned_aliases = entity_entry.get("aliases", [])
        assert set(test_aliases) == set(returned_aliases), (
            f"aliases mismatch: expected {test_aliases}, got {returned_aliases}"
        )

        logger.info("Single entity lookup verified with all fields")

        # Cleanup
        await cleaner.cleanup_all()
        await mcp_client.call_tool("ha_config_remove_area", {"area_id": area_id})

    async def test_get_entity_multiple(self, mcp_client, cleanup_tracker):
        """Test ha_get_entity with multiple entities returns list response."""
        cleaner = EntityCleaner(mcp_client)

        # Create first test helper
        create_result1 = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Get Entity Multi 1",
                "icon": "mdi:numeric-1",
            },
        )
        data1 = assert_mcp_success(create_result1, "Create first test entity")
        entity_id1 = data1.get("entity_id") or f"input_boolean.{data1['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id1)

        # Create second test helper
        create_result2 = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Get Entity Multi 2",
                "icon": "mdi:numeric-2",
            },
        )
        data2 = assert_mcp_success(create_result2, "Create second test entity")
        entity_id2 = data2.get("entity_id") or f"input_boolean.{data2['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id2)

        # Call ha_get_entity with list of 2 entity_ids
        get_result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": [entity_id1, entity_id2]},
        )
        get_data = assert_mcp_success(get_result, "Get multiple entities")

        # Verify response structure
        assert get_data.get("count") == 2, f"Expected count=2, got: {get_data}"
        assert "entity_entries" in get_data, f"Missing entity_entries: {get_data}"

        entity_entries = get_data["entity_entries"]
        assert len(entity_entries) == 2, f"Expected 2 entries, got: {entity_entries}"

        # Verify both entities are present
        returned_entity_ids = {e.get("entity_id") for e in entity_entries}
        assert entity_id1 in returned_entity_ids, f"entity_id1 missing: {entity_entries}"
        assert entity_id2 in returned_entity_ids, f"entity_id2 missing: {entity_entries}"

        # Verify each entry has expected fields
        for entry in entity_entries:
            assert "entity_id" in entry, f"Missing entity_id: {entry}"
            assert "name" in entry, f"Missing name: {entry}"
            assert "icon" in entry, f"Missing icon: {entry}"
            assert "area_id" in entry, f"Missing area_id: {entry}"
            assert "disabled_by" in entry, f"Missing disabled_by: {entry}"
            assert "hidden_by" in entry, f"Missing hidden_by: {entry}"
            assert "enabled" in entry, f"Missing enabled: {entry}"
            assert "hidden" in entry, f"Missing hidden: {entry}"
            assert "aliases" in entry, f"Missing aliases: {entry}"

        logger.info("Multiple entity lookup verified")

        # Cleanup
        await cleaner.cleanup_all()

    async def test_get_entity_nonexistent(self, mcp_client):
        """Test error handling for non-existent entity in ha_get_entity."""
        result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": "sensor.nonexistent_entity_xyz"},
        )
        data = parse_mcp_result(result)

        assert not data.get("success", True), "Should fail for non-existent entity"
        assert "error" in data, f"Missing error field: {data}"
        assert "suggestions" in data, f"Missing suggestions field: {data}"

        logger.info("Non-existent entity error handling verified")

    async def test_get_entity_empty_list(self, mcp_client):
        """Test ha_get_entity with empty list returns empty result."""
        result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": []},
        )
        data = assert_mcp_success(result, "Get entity with empty list")

        assert data.get("count") == 0, f"Expected count=0, got: {data}"
        assert data.get("entity_entries") == [], f"Expected empty entity_entries: {data}"
        assert data.get("message") == "No entities requested", (
            f"Expected 'No entities requested' message: {data}"
        )

        logger.info("Empty list handling verified")

    async def test_get_entity_partial_failure(self, mcp_client, cleanup_tracker):
        """Test ha_get_entity with mix of valid/invalid entities returns partial results."""
        cleaner = EntityCleaner(mcp_client)

        # Create one valid test entity
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Get Entity Partial",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        valid_entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", valid_entity_id)

        nonexistent_entity_id = "sensor.nonexistent_partial_test"

        # Call ha_get_entity with mix of valid and invalid
        get_result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": [valid_entity_id, nonexistent_entity_id]},
        )
        get_data = assert_mcp_success(get_result, "Get entity partial success")

        # Verify partial success
        assert get_data.get("count") == 1, f"Expected count=1 (partial), got: {get_data}"

        entity_entries = get_data.get("entity_entries", [])
        assert len(entity_entries) == 1, f"Expected 1 entry: {entity_entries}"
        assert entity_entries[0].get("entity_id") == valid_entity_id, (
            f"Valid entity not in results: {entity_entries}"
        )

        # Verify errors array has the invalid entity
        errors = get_data.get("errors", [])
        assert len(errors) == 1, f"Expected 1 error: {errors}"
        assert errors[0].get("entity_id") == nonexistent_entity_id, (
            f"Nonexistent entity not in errors: {errors}"
        )

        logger.info("Partial failure handling verified")

        # Cleanup
        await cleaner.cleanup_all()
