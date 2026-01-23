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
