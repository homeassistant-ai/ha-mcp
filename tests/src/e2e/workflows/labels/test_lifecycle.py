"""
Label Lifecycle E2E Tests

Tests the complete label workflow: Create -> Update -> Assign -> Delete
This represents the critical user journey for Home Assistant label management.

Note: Tests are designed to work with the Docker test environment.
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelLifecycle:
    """Test complete label management workflows."""

    async def _find_test_entity(self, mcp_client) -> str:
        """
        Find a suitable entity for testing label assignment.

        Returns entity_id of a suitable entity for testing.
        """
        # Search for light entities (common and safe to modify)
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 10},
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
                logger.info(f"Using demo/test entity: {entity_id}")
                return entity_id

        # Fall back to first available
        entity_id = results[0].get("entity_id", "")
        if not entity_id:
            pytest.skip("No valid entity found for testing")

        logger.info(f"Using first available entity: {entity_id}")
        return entity_id

    async def _cleanup_test_labels(self, mcp_client, label_ids: list[str]) -> None:
        """Clean up test labels after test completion."""
        for label_id in label_ids:
            try:
                await mcp_client.call_tool(
                    "ha_delete_label",
                    {"label_id": label_id},
                )
                logger.info(f"Cleaned up label: {label_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup label {label_id}: {e}")

    async def test_basic_label_lifecycle(self, mcp_client, cleanup_tracker):
        """
        Test: Create label -> List -> Update -> Delete

        This test validates the fundamental label workflow that most
        users will follow when organizing their Home Assistant setup.
        """
        created_label_ids = []

        try:
            # 1. LIST: Get initial label count
            logger.info("Listing initial labels...")
            list_result = await mcp_client.call_tool("ha_list_labels", {})
            list_data = assert_mcp_success(list_result, "list labels")
            initial_count = list_data.get("count", 0)
            logger.info(f"Initial label count: {initial_count}")

            # 2. CREATE: Create a new label
            label_name = "E2E Test Label"
            logger.info(f"Creating label: {label_name}")
            create_result = await mcp_client.call_tool(
                "ha_create_label",
                {
                    "name": label_name,
                    "color": "blue",
                    "icon": "mdi:test-tube",
                    "description": "Label created by E2E test",
                },
            )

            create_data = assert_mcp_success(create_result, "create label")
            label_id = create_data.get("label_id")
            assert label_id, "Label ID should be returned after creation"
            created_label_ids.append(label_id)
            logger.info(f"Created label with ID: {label_id}")

            # 3. LIST: Verify label was created
            await asyncio.sleep(1)  # Allow for propagation
            list_result = await mcp_client.call_tool("ha_list_labels", {})
            list_data = assert_mcp_success(list_result, "list labels after create")
            new_count = list_data.get("count", 0)
            assert new_count == initial_count + 1, (
                f"Label count should increase by 1. Was {initial_count}, now {new_count}"
            )

            # Find our label in the list
            labels = list_data.get("labels", [])
            our_label = next(
                (lbl for lbl in labels if lbl.get("label_id") == label_id), None
            )
            assert our_label, f"Created label {label_id} not found in label list"
            assert our_label.get("name") == label_name, "Label name mismatch"
            assert our_label.get("color") == "blue", "Label color mismatch"
            assert our_label.get("icon") == "mdi:test-tube", "Label icon mismatch"
            logger.info("Label verified in list")

            # 4. UPDATE: Update the label
            new_name = "E2E Test Label Updated"
            logger.info(f"Updating label to: {new_name}")
            update_result = await mcp_client.call_tool(
                "ha_update_label",
                {
                    "label_id": label_id,
                    "name": new_name,
                    "color": "green",
                    "icon": "mdi:star",
                },
            )

            update_data = assert_mcp_success(update_result, "update label")
            logger.info("Label updated successfully")

            # 5. VERIFY: Check update was applied
            await asyncio.sleep(1)
            list_result = await mcp_client.call_tool("ha_list_labels", {})
            list_data = assert_mcp_success(list_result, "list labels after update")
            labels = list_data.get("labels", [])
            our_label = next(
                (lbl for lbl in labels if lbl.get("label_id") == label_id), None
            )
            assert our_label, f"Updated label {label_id} not found"
            assert our_label.get("name") == new_name, "Updated name not reflected"
            assert our_label.get("color") == "green", "Updated color not reflected"
            assert our_label.get("icon") == "mdi:star", "Updated icon not reflected"
            logger.info("Label update verified")

            # 6. DELETE: Delete the label
            logger.info(f"Deleting label: {label_id}")
            delete_result = await mcp_client.call_tool(
                "ha_delete_label",
                {"label_id": label_id},
            )

            delete_data = assert_mcp_success(delete_result, "delete label")
            created_label_ids.remove(label_id)  # Remove from cleanup list
            logger.info("Label deleted successfully")

            # 7. VERIFY: Label is gone
            await asyncio.sleep(1)
            list_result = await mcp_client.call_tool("ha_list_labels", {})
            list_data = assert_mcp_success(list_result, "list labels after delete")
            final_count = list_data.get("count", 0)
            assert final_count == initial_count, (
                f"Label count should return to {initial_count}, got {final_count}"
            )

            labels = list_data.get("labels", [])
            our_label = next(
                (lbl for lbl in labels if lbl.get("label_id") == label_id), None
            )
            assert our_label is None, f"Deleted label {label_id} still exists"
            logger.info("Label deletion verified")

        finally:
            # Cleanup any remaining test labels
            await self._cleanup_test_labels(mcp_client, created_label_ids)

    async def test_label_assignment_to_entity(self, mcp_client, cleanup_tracker):
        """
        Test: Create label -> Assign to entity -> Verify -> Clear assignment

        This test validates the label assignment workflow for entities.
        """
        created_label_ids = []
        test_entity = None

        try:
            # Find a test entity
            test_entity = await self._find_test_entity(mcp_client)
            logger.info(f"Using test entity: {test_entity}")

            # 1. CREATE: Create a label for testing
            label_name = "E2E Assignment Test"
            logger.info(f"Creating label: {label_name}")
            create_result = await mcp_client.call_tool(
                "ha_create_label",
                {
                    "name": label_name,
                    "color": "red",
                    "icon": "mdi:tag",
                },
            )

            create_data = assert_mcp_success(create_result, "create label")
            label_id = create_data.get("label_id")
            assert label_id, "Label ID should be returned"
            created_label_ids.append(label_id)
            logger.info(f"Created label: {label_id}")

            # 2. ASSIGN: Assign label to entity
            logger.info(f"Assigning label {label_id} to entity {test_entity}")
            assign_result = await mcp_client.call_tool(
                "ha_assign_label",
                {
                    "entity_id": test_entity,
                    "labels": [label_id],
                },
            )

            assign_data = assert_mcp_success(assign_result, "assign label")
            logger.info("Label assigned successfully")

            # 3. VERIFY: Check assignment in entity data
            entity_data = assign_data.get("entity_data", {})
            assigned_labels = entity_data.get("labels", [])
            assert label_id in assigned_labels, (
                f"Label {label_id} should be in entity labels. Got: {assigned_labels}"
            )
            logger.info("Label assignment verified")

            # 4. CLEAR: Remove labels from entity
            logger.info(f"Clearing labels from entity {test_entity}")
            clear_result = await mcp_client.call_tool(
                "ha_assign_label",
                {
                    "entity_id": test_entity,
                    "labels": [],  # Empty list clears all labels
                },
            )

            clear_data = assert_mcp_success(clear_result, "clear labels")
            entity_data = clear_data.get("entity_data", {})
            assigned_labels = entity_data.get("labels", [])
            assert len(assigned_labels) == 0, (
                f"Entity should have no labels. Got: {assigned_labels}"
            )
            logger.info("Labels cleared from entity")

        finally:
            # Cleanup test labels
            await self._cleanup_test_labels(mcp_client, created_label_ids)

    async def test_multiple_labels_assignment(self, mcp_client, cleanup_tracker):
        """
        Test: Create multiple labels -> Assign all to entity -> Verify

        This test validates assigning multiple labels at once.
        """
        created_label_ids = []
        test_entity = None

        try:
            # Find a test entity
            test_entity = await self._find_test_entity(mcp_client)
            logger.info(f"Using test entity: {test_entity}")

            # 1. CREATE: Create multiple labels
            label_configs = [
                {"name": "E2E Multi Label 1", "color": "red"},
                {"name": "E2E Multi Label 2", "color": "green"},
                {"name": "E2E Multi Label 3", "color": "blue"},
            ]

            for config in label_configs:
                create_result = await mcp_client.call_tool(
                    "ha_create_label",
                    config,
                )
                create_data = assert_mcp_success(
                    create_result, f"create label {config['name']}"
                )
                label_id = create_data.get("label_id")
                created_label_ids.append(label_id)
                logger.info(f"Created label: {label_id}")

            # 2. ASSIGN: Assign all labels to entity
            logger.info(f"Assigning {len(created_label_ids)} labels to {test_entity}")
            assign_result = await mcp_client.call_tool(
                "ha_assign_label",
                {
                    "entity_id": test_entity,
                    "labels": created_label_ids,
                },
            )

            assign_data = assert_mcp_success(assign_result, "assign multiple labels")
            entity_data = assign_data.get("entity_data", {})
            assigned_labels = entity_data.get("labels", [])

            for label_id in created_label_ids:
                assert label_id in assigned_labels, (
                    f"Label {label_id} should be assigned. Got: {assigned_labels}"
                )
            logger.info(f"All {len(created_label_ids)} labels assigned successfully")

            # 3. PARTIAL UPDATE: Replace with subset of labels
            subset_labels = created_label_ids[:2]  # First two labels only
            logger.info(f"Updating to subset of labels: {subset_labels}")
            update_result = await mcp_client.call_tool(
                "ha_assign_label",
                {
                    "entity_id": test_entity,
                    "labels": subset_labels,
                },
            )

            update_data = assert_mcp_success(update_result, "update labels subset")
            entity_data = update_data.get("entity_data", {})
            assigned_labels = entity_data.get("labels", [])

            assert len(assigned_labels) == 2, (
                f"Entity should have 2 labels. Got: {len(assigned_labels)}"
            )
            for label_id in subset_labels:
                assert label_id in assigned_labels, (
                    f"Label {label_id} should be assigned"
                )
            assert created_label_ids[2] not in assigned_labels, (
                "Third label should be removed"
            )
            logger.info("Label subset update verified")

            # 4. CLEAR: Clear all labels
            await mcp_client.call_tool(
                "ha_assign_label",
                {
                    "entity_id": test_entity,
                    "labels": [],
                },
            )
            logger.info("Labels cleared from entity")

        finally:
            # Cleanup test labels
            await self._cleanup_test_labels(mcp_client, created_label_ids)


@pytest.mark.labels
class TestLabelValidation:
    """Test label validation and error handling."""

    async def test_update_nonexistent_label(self, mcp_client):
        """Test updating a label that doesn't exist."""
        logger.info("Testing update of nonexistent label...")

        update_result = await mcp_client.call_tool(
            "ha_update_label",
            {
                "label_id": "nonexistent_label_id_12345",
                "name": "New Name",
            },
        )

        update_data = parse_mcp_result(update_result)
        assert not update_data.get("success"), (
            "Updating nonexistent label should fail"
        )
        logger.info("Nonexistent label update correctly rejected")

    async def test_delete_nonexistent_label(self, mcp_client):
        """Test deleting a label that doesn't exist."""
        logger.info("Testing delete of nonexistent label...")

        delete_result = await mcp_client.call_tool(
            "ha_delete_label",
            {"label_id": "nonexistent_label_id_12345"},
        )

        delete_data = parse_mcp_result(delete_result)
        assert not delete_data.get("success"), (
            "Deleting nonexistent label should fail"
        )
        logger.info("Nonexistent label delete correctly rejected")

    async def test_assign_to_nonexistent_entity(self, mcp_client):
        """Test assigning label to entity that doesn't exist."""
        logger.info("Testing assign to nonexistent entity...")

        assign_result = await mcp_client.call_tool(
            "ha_assign_label",
            {
                "entity_id": "light.nonexistent_entity_12345",
                "labels": ["some_label"],
            },
        )

        assign_data = parse_mcp_result(assign_result)
        assert not assign_data.get("success"), (
            "Assigning to nonexistent entity should fail"
        )
        logger.info("Nonexistent entity assignment correctly rejected")

    async def test_update_without_changes(self, mcp_client):
        """Test that update requires at least one field to change."""
        logger.info("Testing update without any fields...")

        update_result = await mcp_client.call_tool(
            "ha_update_label",
            {"label_id": "some_label"},
        )

        update_data = parse_mcp_result(update_result)
        assert not update_data.get("success"), (
            "Update without any fields should fail"
        )
        error_msg = str(update_data.get("error", "")).lower()
        assert "at least one field" in error_msg or "must be provided" in error_msg, (
            f"Error should mention missing fields. Got: {update_data.get('error')}"
        )
        logger.info("Update without fields correctly rejected")


@pytest.mark.labels
async def test_label_list_empty_state(mcp_client):
    """
    Test: List labels returns proper structure even when empty.

    This is a basic sanity check that the list endpoint works.
    """
    logger.info("Testing label list...")

    list_result = await mcp_client.call_tool("ha_list_labels", {})
    list_data = assert_mcp_success(list_result, "list labels")

    # Check structure
    assert "count" in list_data, "Response should include count"
    assert "labels" in list_data, "Response should include labels array"
    assert isinstance(list_data["labels"], list), "Labels should be a list"
    assert list_data["count"] == len(list_data["labels"]), (
        "Count should match labels array length"
    )

    logger.info(f"Label list returned {list_data['count']} labels")
