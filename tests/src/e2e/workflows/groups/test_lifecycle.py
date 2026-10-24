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

import logging
import uuid

import pytest

from ...utilities.assertions import MCPAssertions, safe_call_tool

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
            # A fresh id: the create assertion below fails on a leftover group.
            object_id = f"test_e2e_lights_{uuid.uuid4().hex[:8]}"
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
            # Passing name= on a create must still read as a create.
            assert (
                create_data["message"]
                == f"Successfully created group: group.{object_id}"
            )
            cleanup_tracker.track("group", object_id)
            logger.info(f"Created group: group.{object_id}")

            # 2. SEARCH: Verify the public search contract against a real HA group.
            search_data = await mcp.call_tool_success(
                "ha_search",
                {
                    "query": f"group.{object_id}",
                    "domain_filter": "group",
                    "exact_match": True,
                    "result_fields": [
                        "entity_id",
                        "is_group",
                        "member_entity_ids",
                    ],
                },
            )
            search_record = next(
                (
                    entity
                    for entity in search_data["entities"]
                    if entity["entity_id"] == f"group.{object_id}"
                ),
                None,
            )
            assert search_record is not None, search_data
            assert search_record["is_group"] is True
            assert search_record["member_entity_ids"] == [
                "light.bed_light",
                "light.ceiling_lights",
            ]

            # 3. LIST: Verify group appears in list
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
                    # Core marks group.set groups with ``auto``.
                    assert group["auto"] is True, group
                    # State fields the tool used to drop now reach the caller.
                    assert "last_changed" in group, group
                    assert "context" in group, group
                    break

            assert group_found, f"Group {object_id} not found in list"
            logger.info("Group verified in list")

            # 4. UPDATE: Modify group name
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
            assert (
                update_data["message"]
                == f"Successfully updated group: group.{object_id}"
            )
            assert "warnings" not in update_data, update_data
            logger.info("Group name updated successfully")

            # 4. VERIFY UPDATE: Check updated values in list
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    assert group.get("friendly_name") == "Updated E2E Lights", (
                        f"Updated name mismatch: {group.get('friendly_name')}"
                    )
                    break
            logger.info("Group update verified")

            # 6. DELETE: Remove group
            await mcp.call_tool_success(
                "ha_config_remove_group",
                {"object_id": object_id},
            )
            logger.info("Group deleted successfully")

            # 6. VERIFY DELETE: Group should not appear in list
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            for group in list_data.get("groups", []):
                assert group.get("object_id") != object_id, (
                    f"Group {object_id} still exists after deletion"
                )
            logger.info("Group deletion verified")

    async def test_group_list_pagination(self, mcp_client, cleanup_tracker):
        """
        Test: limit/offset paginate the group listing (issue #1869)

        Drives the pagination contract through the MCP layer, which the unit
        suite cannot reach: it calls the tool function directly and so never
        exercises the parameter schema. Every assertion holds against the
        response it came from rather than a baseline captured earlier, because
        other modules create groups too and the collection can grow between
        calls.
        """
        logger.info("Testing group list pagination...")

        async with MCPAssertions(mcp_client) as mcp:
            # Three groups guarantee at least two pages at limit=2.
            for i in range(3):
                object_id = f"test_e2e_page_{i}"
                await mcp.call_tool_success(
                    "ha_config_set_group",
                    {
                        "object_id": object_id,
                        "name": f"Test E2E Page {i}",
                        "entities": ["light.bed_light"],
                    },
                )
                cleanup_tracker.track("group", object_id)

            # Each response is asserted against itself rather than a baseline
            # captured earlier: other modules create groups too, so the
            # collection can grow between calls.
            first = await mcp.call_tool_success(
                "ha_config_list_groups", {"limit": 2, "offset": 0}
            )
            assert len(first["groups"]) == 2, f"limit=2 should cut the page: {first}"
            assert first["count"] == 2, (
                f"count is the page size, not the total: {first}"
            )
            assert first["total_count"] >= 3, f"the three created groups: {first}"
            assert first["offset"] == 0
            assert first["limit"] == 2
            assert first["has_more"] is True
            assert first["next_offset"] == 2

            # Second page: offset advanced the window, so no record repeats.
            second = await mcp.call_tool_success(
                "ha_config_list_groups", {"limit": 2, "offset": 2}
            )
            assert second["offset"] == 2
            first_ids = {g["entity_id"] for g in first["groups"]}
            second_ids = {g["entity_id"] for g in second["groups"]}
            assert not (first_ids & second_ids), (
                f"pages must not overlap: {first_ids & second_ids}"
            )

            # A limit above the collection returns it whole, in one page.
            full = await mcp.call_tool_success("ha_config_list_groups", {"limit": 500})
            assert len(full["groups"]) == full["total_count"]
            assert full["count"] == full["total_count"]
            assert full["has_more"] is False
            assert full["next_offset"] is None

            # Offset past the end: an empty final page, not an error.
            past = await mcp.call_tool_success(
                "ha_config_list_groups", {"offset": full["total_count"] + 10}
            )
            assert past["groups"] == []
            assert past["count"] == 0
            assert past["has_more"] is False
            assert past["next_offset"] is None

            logger.info(f"Pagination verified across {full['total_count']} groups")

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

        NOTE: Home Assistant keeps the all/any mode internal and exposes no
        state attribute for it, so the list cannot report it.
        """
        logger.info("Testing group with all_on mode...")

        async with MCPAssertions(mcp_client) as mcp:
            object_id = "test_e2e_all_on"
            result = await mcp.call_tool_success(
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

            # Verify 'all' is in the updated_fields (service parameter was passed)
            assert "all" in result.get("updated_fields", []), (
                f"'all' parameter not in updated_fields: {result}"
            )
            logger.info("all_on parameter was sent to service")

            # Verify group appears in list
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})

            group_found = False
            for group in list_data.get("groups", []):
                if group.get("object_id") == object_id:
                    group_found = True
                    assert "all" not in group, group
                    break
            assert group_found, f"Group {object_id} not found in list"
            logger.info("Group with all_on created successfully")

            # Update all_on mode to False - verify service call succeeds
            result = await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "all_on": False,
                },
            )
            assert "all" in result.get("updated_fields", []), (
                f"'all' parameter not in updated_fields: {result}"
            )
            logger.info("all_on mode update service call succeeded")

            # Cleanup
            await mcp.call_tool_success(
                "ha_config_remove_group",
                {"object_id": object_id},
            )
            logger.info("All-on group cleaned up")

    async def test_group_input_validation(self, mcp_client):
        """
        Test: Home Assistant's own validation reaches the caller

        group.set validation belongs to Home Assistant; the tool passes its
        rejection through over WebSocket, where the message survives.
        """
        logger.info("Testing group input validation...")

        async with MCPAssertions(mcp_client) as mcp:
            # Core's cv.slug rejects the prefix; its "try group_invalid" names
            # another group, so the tool adds a hint to drop the prefix.
            await mcp.call_tool_failure(
                "ha_config_set_group",
                {
                    "object_id": "group.invalid",
                    "entities": ["light.bed_light"],
                },
                expected_error="invalid slug group.invalid (try group_invalid)",
            )

            await mcp.call_tool_failure(
                "ha_config_set_group",
                {
                    "object_id": "test_mutual_exclusive",
                    "entities": ["light.bed_light"],
                    "add_entities": ["light.ceiling_lights"],
                },
                expected_error="two or more values in the same group of exclusion",
            )

            # Core accepts an empty member list and creates an empty group.
            object_id = f"test_e2e_empty_members_{uuid.uuid4().hex[:8]}"
            try:
                created = await mcp.call_tool_success(
                    "ha_config_set_group",
                    {"object_id": object_id, "entities": []},
                )
                assert (
                    created["message"]
                    == f"Successfully created group: group.{object_id}"
                )
                list_data = await mcp.call_tool_success("ha_config_list_groups", {})
                group = next(
                    (g for g in list_data["groups"] if g["object_id"] == object_id),
                    None,
                )
                assert group is not None, list_data
                assert group["entity_ids"] == [], group
            finally:
                await safe_call_tool(
                    mcp_client, "ha_config_remove_group", {"object_id": object_id}
                )

            # Core's group.remove only logs a warning for a missing group; the
            # tool must not report it as removed.
            await mcp.call_tool_failure(
                "ha_config_remove_group",
                {"object_id": "test_e2e_never_created"},
                expected_error="Group 'group.test_e2e_never_created' not found",
            )

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

            logger.info(f"Created {len(created_object_ids)} groups")

            # Verify all groups exist
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})
            object_ids_in_list = [
                g.get("object_id") for g in list_data.get("groups", [])
            ]

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
            logger.info("All groups updated")

            # Delete all groups
            for object_id in created_object_ids:
                await mcp.call_tool_success(
                    "ha_config_remove_group",
                    {"object_id": object_id},
                )
            logger.info("All groups deleted")

            # Verify all deleted
            list_data = await mcp.call_tool_success("ha_config_list_groups", {})
            object_ids_in_list = [
                g.get("object_id") for g in list_data.get("groups", [])
            ]

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
                "ha_search",
                {"query": "group", "domain_filter": "group", "limit": 10},
            )

            if search_data.get("success") and search_data.get("entities"):
                results = search_data.get("entities", [])
                logger.info(f"Found {len(results)} group entities via search")

                for result in results:
                    entity_id = result.get("entity_id", "")
                    friendly_name = result.get("friendly_name", "Unknown")
                    logger.info(f"  - {entity_id}: {friendly_name}")
            else:
                logger.info("No group entities found via search (may be normal)")

        except Exception as e:  # noqa: BLE001
            logger.warning(f"Group search failed: {e}")
            logger.info("This may be normal if no groups exist")

    logger.info("Group search and discovery test completed")
