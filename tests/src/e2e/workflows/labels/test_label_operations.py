"""
Label Operations E2E Tests

Tests for ha_set_entity labels parameter:
- Set: Replace all labels on an entity (label_operation='set' or default)
- Add: Add labels to existing ones (label_operation='add')
- Remove: Remove specific labels (label_operation='remove')
- Clear: Remove all labels from an entity (labels=[])

Also includes regression test for Issue #396 (entity registry corruption).
"""

import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.fixture
async def test_entity_id(mcp_client) -> str:
    """Find a single suitable entity for testing."""
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "domain_filter": "light", "limit": 1},
    )
    search_data = parse_mcp_result(search_result)
    results = search_data.get("data", search_data).get("results", [])
    if not results:
        pytest.skip("No light entities available for testing")
    return results[0]["entity_id"]


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelSetOperation:
    """Test setting labels on entities via ha_set_entity."""

    async def test_set_labels(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Set labels on an entity."""
        entity_id = test_entity_id
        label = "test_set_label"

        # Create the test label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": label},
        )
        create_data = assert_mcp_success(create_result, "create label")
        label_id = create_data.get("label_id", label)
        cleanup_tracker.track("label", label_id)

        # Set labels on entity
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [label_id]},
        )
        data = assert_mcp_success(result, "set labels")
        assert label_id in data.get("entity_entry", {}).get("labels", [])

        logger.info(f"Set labels on {entity_id}: {data.get('entity_entry', {}).get('labels')}")

        # Clean up: clear labels
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )

    async def test_set_multiple_labels(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Set multiple labels on an entity at once."""
        entity_id = test_entity_id

        # Create two test labels
        labels = []
        for name in ["test_multi_1", "test_multi_2"]:
            create_result = await mcp_client.call_tool(
                "ha_config_set_label",
                {"name": name},
            )
            create_data = assert_mcp_success(create_result, f"create label {name}")
            label_id = create_data.get("label_id", name)
            labels.append(label_id)
            cleanup_tracker.track("label", label_id)

        # Set both labels
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": labels},
        )
        data = assert_mcp_success(result, "set multiple labels")

        entity_labels = data.get("entity_entry", {}).get("labels", [])
        for label_id in labels:
            assert label_id in entity_labels, f"Label {label_id} should be set"

        logger.info(f"Set multiple labels on {entity_id}: {entity_labels}")

        # Clean up: clear labels
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )

    async def test_clear_labels(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Clear all labels from an entity using empty list."""
        entity_id = test_entity_id

        # Create and set a label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "test_clear_label"},
        )
        create_data = assert_mcp_success(create_result, "create label")
        label_id = create_data.get("label_id", "test_clear_label")
        cleanup_tracker.track("label", label_id)

        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [label_id]},
        )

        # Clear all labels
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )
        data = assert_mcp_success(result, "clear labels")

        entity_labels = data.get("entity_entry", {}).get("labels", [])
        assert len(entity_labels) == 0, "Labels should be empty after clearing"

        logger.info(f"Cleared labels on {entity_id}")

    async def test_set_replaces_existing_labels(
        self, mcp_client, cleanup_tracker, test_entity_id
    ):
        """Test: Setting labels replaces all existing labels."""
        entity_id = test_entity_id

        # Create two labels
        labels = []
        for name in ["test_replace_1", "test_replace_2"]:
            create_result = await mcp_client.call_tool(
                "ha_config_set_label",
                {"name": name},
            )
            create_data = assert_mcp_success(create_result, f"create label {name}")
            label_id = create_data.get("label_id", name)
            labels.append(label_id)
            cleanup_tracker.track("label", label_id)

        # Set first label
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [labels[0]]},
        )

        # Replace with second label only
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [labels[1]]},
        )
        data = assert_mcp_success(result, "replace labels")

        entity_labels = data.get("entity_entry", {}).get("labels", [])
        assert labels[1] in entity_labels, "New label should be present"
        assert labels[0] not in entity_labels, "Old label should be replaced"

        logger.info(f"Replaced labels on {entity_id}: {entity_labels}")

        # Clean up
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelEntityRegistryIntegrity:
    """Regression test for Issue #396: Entity registry corruption from label operations."""

    async def test_labels_dont_corrupt_entity_properties(
        self, mcp_client, cleanup_tracker, test_entity_id
    ):
        """Test: Setting labels should not affect other entity properties."""
        entity_id = test_entity_id

        # Get initial entity state
        initial_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        initial_data = parse_mcp_result(initial_result)

        # Create and set a label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "test_integrity"},
        )
        create_data = assert_mcp_success(create_result, "create label")
        label_id = create_data.get("label_id", "test_integrity")
        cleanup_tracker.track("label", label_id)

        set_result = await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [label_id]},
        )
        assert_mcp_success(set_result, "set labels")

        # Verify entity still functions - get state again
        after_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        assert_mcp_success(after_result, "get state after label change")

        logger.info("Entity properties preserved after label operation")

        # Clean up
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelAddRemoveOperations:
    """Test add and remove label operations via ha_set_entity."""

    async def test_add_labels_to_existing(
        self, mcp_client, cleanup_tracker, test_entity_id
    ):
        """Test: label_operation='add' adds labels without removing existing ones."""
        entity_id = test_entity_id

        # Create two test labels
        labels = []
        for name in ["test_add_existing", "test_add_new"]:
            create_result = await mcp_client.call_tool(
                "ha_config_set_label",
                {"name": name},
            )
            create_data = assert_mcp_success(create_result, f"create label {name}")
            label_id = create_data.get("label_id", name)
            labels.append(label_id)
            cleanup_tracker.track("label", label_id)

        # Set the first label using default 'set' operation
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [labels[0]]},
        )

        # Add the second label using 'add' operation
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "labels": [labels[1]],
                "label_operation": "add",
            },
        )
        data = assert_mcp_success(result, "add label")

        entity_labels = data.get("entity_entry", {}).get("labels", [])
        assert labels[0] in entity_labels, "Existing label should still be present"
        assert labels[1] in entity_labels, "New label should be added"

        logger.info(f"Added label to {entity_id}: {entity_labels}")

        # Clean up
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )

    async def test_remove_specific_labels(
        self, mcp_client, cleanup_tracker, test_entity_id
    ):
        """Test: label_operation='remove' removes only specified labels."""
        entity_id = test_entity_id

        # Create two test labels
        labels = []
        for name in ["test_remove_keep", "test_remove_delete"]:
            create_result = await mcp_client.call_tool(
                "ha_config_set_label",
                {"name": name},
            )
            create_data = assert_mcp_success(create_result, f"create label {name}")
            label_id = create_data.get("label_id", name)
            labels.append(label_id)
            cleanup_tracker.track("label", label_id)

        # Set both labels
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": labels},
        )

        # Remove only the second label
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "labels": [labels[1]],
                "label_operation": "remove",
            },
        )
        data = assert_mcp_success(result, "remove label")

        entity_labels = data.get("entity_entry", {}).get("labels", [])
        assert labels[0] in entity_labels, "Unspecified label should remain"
        assert labels[1] not in entity_labels, "Specified label should be removed"

        logger.info(f"Removed label from {entity_id}: {entity_labels}")

        # Clean up
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )

    async def test_add_prevents_duplicates(
        self, mcp_client, cleanup_tracker, test_entity_id
    ):
        """Test: Adding an already-present label doesn't create duplicates."""
        entity_id = test_entity_id

        # Create a test label
        create_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "test_dup_check"},
        )
        create_data = assert_mcp_success(create_result, "create label")
        label_id = create_data.get("label_id", "test_dup_check")
        cleanup_tracker.track("label", label_id)

        # Set the label
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": [label_id]},
        )

        # Try to add the same label again
        result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "labels": [label_id],
                "label_operation": "add",
            },
        )
        data = assert_mcp_success(result, "add duplicate label")

        entity_labels = data.get("entity_entry", {}).get("labels", [])
        # Count occurrences - should only be 1
        count = entity_labels.count(label_id)
        assert count == 1, f"Label should appear exactly once, found {count}"

        logger.info(f"Duplicate prevention verified for {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_set_entity",
            {"entity_id": entity_id, "labels": []},
        )
