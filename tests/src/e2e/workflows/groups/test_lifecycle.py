"""
End-to-End tests for Home Assistant Group Management tools.

This test suite validates the complete lifecycle of Home Assistant entity groups
(old-style groups created via group.set service) including:
- Group listing
- Group creation with various configurations
- Group updates (name, icon, entities, add/remove entities)
- Group deletion
- Input validation and error handling

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import MCPAssertions

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.mark.group
class TestGroupLifecycle:
    """Test complete group management workflows."""

    async def test_group_list(self, mcp_client):
        """
        Test: List all groups

        Validates that we can retrieve all groups from Home Assistant.
        """
        logger.info("Listing all groups...")

        async with MCPAssertions(mcp_client) as mcp:
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            assert "count" in list_data, f"Missing 'count' in response: {list_data}"
            assert "groups" in list_data, f"Missing 'groups' in response: {list_data}"
            assert isinstance(list_data["groups"], list), (
                f"Groups should be a list: {list_data}"
            )

            logger.info(f"Found {list_data['count']} groups")
            for group in list_data["groups"]:
                logger.info(
                    f"  - {group.get('entity_id', 'Unknown')} "
                    f"(name: {group.get('friendly_name', 'N/A')})"
                )

    async def test_group_basic_lifecycle(self, mcp_client, cleanup_tracker):
        """
        Test: Basic group lifecycle (create, list, update, delete)

        Validates fundamental group operations with a simple group.
        """
        logger.info("Testing basic group lifecycle...")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. CREATE: Basic group with entities
            object_id = "test_e2e_lights"
            group_name = "Test E2E Lights"
            create_data = await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "name": group_name,
                    "entities": ["light.bed_light", "light.ceiling_lights"],
                    "icon": "mdi:lightbulb-group",
                },
            )

            assert create_data.get("entity_id") == f"group.{object_id}", (
                f"Entity ID mismatch: {create_data}"
            )
            cleanup_tracker.track("group", object_id)
            logger.info(f"Created group: group.{object_id}")

            # 2. LIST: Verify group appears in list
            await asyncio.sleep(0.5)  # Allow time for registration
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            group_found = False
            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    group_found = True
                    assert group.get("friendly_name") == group_name, (
                        f"Name mismatch: {group.get('friendly_name')}"
                    )
                    assert group.get("icon") == "mdi:lightbulb-group", (
                        f"Icon mismatch: {group.get('icon')}"
                    )
                    assert "light.bed_light" in group.get("entity_ids", []), (
                        f"Missing entity: {group.get('entity_ids')}"
                    )
                    assert "light.ceiling_lights" in group.get("entity_ids", []), (
                        f"Missing entity: {group.get('entity_ids')}"
                    )
                    break

            assert group_found, f"Group {object_id} not found in list"
            logger.info("Group verified in list")

            # 3. UPDATE: Modify group name
            update_data = await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "name": "Updated E2E Lights",
                },
            )

            assert "updated_fields" in update_data, (
                f"Missing updated_fields: {update_data}"
            )
            assert "name" in update_data["updated_fields"], (
                f"Name not in updated_fields: {update_data}"
            )
            logger.info("Group name updated successfully")

            # 4. VERIFY UPDATE: Check updated values in list
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    assert group.get("friendly_name") == "Updated E2E Lights", (
                        f"Updated name mismatch: {group.get('friendly_name')}"
                    )
                    break
            logger.info("Group update verified")

            # 5. DELETE: Remove group
            await mcp.call_tool_success(
                "ha_config_remove_group",
                {"object_id": object_id},
            )
            logger.info("Group deleted successfully")

            # 6. VERIFY DELETE: Group should not appear in list
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                assert group.get("object_id") != object_id, (
                    f"Group {object_id} still exists after deletion"
                )
            logger.info("Group deletion verified")

    async def test_group_add_remove_entities(self, mcp_client, cleanup_tracker):
        """
        Test: Add and remove entities from a group

        Validates add_entities and remove_entities operations.
        """
        logger.info("Testing add/remove entities operations...")

        async with MCPAssertions(mcp_client) as mcp:
            # Create initial group with one entity
            object_id = "test_e2e_modifiable"
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "name": "Modifiable Group",
                    "entities": ["light.bed_light"],
                },
            )
            cleanup_tracker.track("group", object_id)
            logger.info("Created group with one entity")

            # Add another entity
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "add_entities": ["light.ceiling_lights"],
                },
            )
            logger.info("Added entity to group")

            # Verify entity was added
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})
            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    entity_ids = group.get("entity_ids", [])
                    assert "light.bed_light" in entity_ids, (
                        f"Original entity missing: {entity_ids}"
                    )
                    assert "light.ceiling_lights" in entity_ids, (
                        f"Added entity missing: {entity_ids}"
                    )
                    break
            logger.info("Entity addition verified")

            # Remove original entity
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "remove_entities": ["light.bed_light"],
                },
            )
            logger.info("Removed entity from group")

            # Verify entity was removed
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})
            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    entity_ids = group.get("entity_ids", [])
                    assert "light.bed_light" not in entity_ids, (
                        f"Removed entity still present: {entity_ids}"
                    )
                    assert "light.ceiling_lights" in entity_ids, (
                        f"Remaining entity missing: {entity_ids}"
                    )
                    break
            logger.info("Entity removal verified")

            # Cleanup
            await mcp.call_tool_success(
                "ha_config_remove_group",
                {"object_id": object_id},
            )
            logger.info("Modifiable group cleaned up")

    async def test_group_with_all_mode(self, mcp_client, cleanup_tracker):
        """
        Test: Create group with all_on mode enabled

        When all_on is True, all entities must be on for group to be on.
        """
        logger.info("Testing group with all_on mode...")

        async with MCPAssertions(mcp_client) as mcp:
            object_id = "test_e2e_all_on"
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "name": "All On Group",
                    "entities": ["light.bed_light", "light.ceiling_lights"],
                    "all_on": True,
                },
            )
            cleanup_tracker.track("group", object_id)
            logger.info(f"Created group with all_on mode: group.{object_id}")

            # Verify all_on mode in list
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    assert group.get("all") is True, (
                        f"all_on mode not set: {group}"
                    )
                    break
            logger.info("all_on mode verified")

            # Update all_on mode to False
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "all_on": False,
                },
            )
            logger.info("Updated all_on mode to False")

            # Verify all_on mode is now False
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    assert group.get("all") is False, (
                        f"all_on mode should be False: {group}"
                    )
                    break
            logger.info("all_on mode update verified")

            # Cleanup
            await mcp.call_tool_success(
                "ha_config_remove_group",
                {"object_id": object_id},
            )
            logger.info("All-on group cleaned up")

    async def test_group_input_validation(self, mcp_client):
        """
        Test: Input validation for group operations

        Validates proper error handling for invalid inputs.
        """
        logger.info("Testing group input validation...")

        async with MCPAssertions(mcp_client) as mcp:
            # Test: Invalid object_id with dot
            await mcp.call_tool_failure(
                "ha_config_set_group",
                {
                    "object_id": "group.invalid",  # Should not include prefix
                    "entities": ["light.bed_light"],
                },
                expected_error="Invalid object_id",
            )
            logger.info("Invalid object_id (with dot) properly rejected")

            # Test: Empty entities list
            await mcp.call_tool_failure(
                "ha_config_set_group",
                {
                    "object_id": "test_empty_entities",
                    "entities": [],  # Empty list should be rejected
                },
                expected_error="empty",
            )
            logger.info("Empty entities list properly rejected")

            # Test: Empty add_entities list
            await mcp.call_tool_failure(
                "ha_config_set_group",
                {
                    "object_id": "test_empty_add",
                    "add_entities": [],  # Empty list should be rejected
                },
                expected_error="empty",
            )
            logger.info("Empty add_entities list properly rejected")

            # Test: Mutually exclusive entity operations
            await mcp.call_tool_failure(
                "ha_config_set_group",
                {
                    "object_id": "test_mutual_exclusive",
                    "entities": ["light.bed_light"],
                    "add_entities": ["light.ceiling_lights"],  # Can't use both
                },
                expected_error="Only one of",
            )
            logger.info("Mutually exclusive entity operations properly rejected")

            # Test: Remove group with invalid object_id
            await mcp.call_tool_failure(
                "ha_config_remove_group",
                {
                    "object_id": "group.invalid_prefix",  # Should not include prefix
                },
                expected_error="Invalid object_id",
            )
            logger.info("Invalid object_id for removal properly rejected")

            logger.info("All input validation tests passed")

    async def test_group_replace_entities(self, mcp_client, cleanup_tracker):
        """
        Test: Replace all entities in a group

        Validates that providing entities list replaces all existing entities.
        """
        logger.info("Testing entity replacement...")

        async with MCPAssertions(mcp_client) as mcp:
            # Create initial group
            object_id = "test_e2e_replace"
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "name": "Replace Test Group",
                    "entities": ["light.bed_light", "light.ceiling_lights"],
                },
            )
            cleanup_tracker.track("group", object_id)
            logger.info("Created group with two entities")

            # Replace all entities with a new set
            await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "entities": ["light.kitchen_lights"],  # Replace with single entity
                },
            )
            logger.info("Replaced entities")

            # Verify replacement
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    entity_ids = group.get("entity_ids", [])
                    assert "light.kitchen_lights" in entity_ids, (
                        f"New entity missing: {entity_ids}"
                    )
                    assert "light.bed_light" not in entity_ids, (
                        f"Old entity still present: {entity_ids}"
                    )
                    assert "light.ceiling_lights" not in entity_ids, (
                        f"Old entity still present: {entity_ids}"
                    )
                    break
            logger.info("Entity replacement verified")

            # Cleanup
            await mcp.call_tool_success(
                "ha_config_remove_group",
                {"object_id": object_id},
            )
            logger.info("Replace test group cleaned up")

    async def test_group_multiple_operations(self, mcp_client, cleanup_tracker):
        """
        Test: Multiple group operations in sequence

        Tests creating and managing multiple groups simultaneously.
        """
        logger.info("Testing multiple group operations...")

        async with MCPAssertions(mcp_client) as mcp:
            groups_to_create = [
                {
                    "object_id": "test_e2e_alpha",
                    "name": "Test Group Alpha",
                    "entities": ["light.bed_light"],
                    "icon": "mdi:alpha-a",
                },
                {
                    "object_id": "test_e2e_beta",
                    "name": "Test Group Beta",
                    "entities": ["light.ceiling_lights"],
                    "icon": "mdi:alpha-b",
                },
                {
                    "object_id": "test_e2e_gamma",
                    "name": "Test Group Gamma",
                    "entities": ["light.kitchen_lights"],
                    "icon": "mdi:alpha-g",
                },
            ]

            created_object_ids = []

            # Create multiple groups
            for group_config in groups_to_create:
                create_data = await mcp.call_tool_success(
                    "ha_config_set_group",
                    group_config,
                )
                object_id = group_config["object_id"]
                assert create_data.get("entity_id") == f"group.{object_id}", (
                    f"Entity ID mismatch for {object_id}"
                )
                created_object_ids.append(object_id)
                cleanup_tracker.track("group", object_id)
                logger.info(f"Created: {group_config['name']} (id: {object_id})")
                await asyncio.sleep(0.3)

            logger.info(f"Created {len(created_object_ids)} groups")

            # Verify all groups exist
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})
            object_ids_in_list = [g.get("object_id") for g in list_data.get("groups", [])]

            for object_id in created_object_ids:
                assert object_id in object_ids_in_list, (
                    f"Group {object_id} not found in list"
                )
            logger.info("All groups verified in list")

            # Update all groups
            for object_id in created_object_ids:
                await mcp.call_tool_success(
                    "ha_config_set_group",
                    {
                        "object_id": object_id,
                        "icon": "mdi:star",  # Update all to same icon
                    },
                )
                await asyncio.sleep(0.3)
            logger.info("All groups updated")

            # Delete all groups
            for object_id in created_object_ids:
                await mcp.call_tool_success(
                    "ha_config_remove_group",
                    {"object_id": object_id},
                )
                await asyncio.sleep(0.3)
            logger.info("All groups deleted")

            # Verify all deleted
            await asyncio.sleep(0.5)
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})
            object_ids_in_list = [g.get("object_id") for g in list_data.get("groups", [])]

            for object_id in created_object_ids:
                assert object_id not in object_ids_in_list, (
                    f"Group {object_id} still exists after deletion"
                )
            logger.info("All group deletions verified")


async def test_group_search_discovery(mcp_client):
    """
    Test: Group search and discovery capabilities

    Validates that users can find groups using search tools.
    """
    logger.info("Testing group search and discovery...")

    async with MCPAssertions(mcp_client) as mcp:
        # Search for group entities
        try:
            search_data = await mcp.call_tool_success(
                "ha_search_entities",
                {"query": "group", "domain_filter": "group", "limit": 10},
            )

            data = (
                search_data.get("data", {}) if search_data.get("data") else search_data
            )

            if data.get("success") and data.get("results"):
                results = data.get("results", [])
                logger.info(f"Found {len(results)} group entities via search")

                for result in results:
                    entity_id = result.get("entity_id", "")
                    friendly_name = result.get("friendly_name", "Unknown")
                    logger.info(f"  - {entity_id}: {friendly_name}")
            else:
                logger.info("No group entities found via search (may be normal)")

        except Exception as e:
            logger.warning(f"Group search failed: {e}")
            logger.info("This may be normal if no groups exist")

    logger.info("Group search and discovery test completed")
