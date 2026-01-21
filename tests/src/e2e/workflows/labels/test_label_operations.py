"""
Label Operations E2E Tests

Comprehensive tests for ha_manage_entity_labels operations:
- Add: Append labels to existing labels (preserves others)
- Remove: Remove specific labels (preserves remaining)
- Set: Replace all labels (overwrites)
- Bulk: Multiple entities in parallel and sequential modes

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


@pytest.fixture
async def test_entity_ids(mcp_client) -> list[str]:
    """Find multiple suitable entities for testing."""
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "domain_filter": "light", "limit": 20},
    )
    search_data = parse_mcp_result(search_result)
    results = search_data.get("data", search_data).get("results", [])
    if len(results) < 3:
        pytest.skip("Need at least 3 light entities for bulk testing")
    return [r["entity_id"] for r in results[:3]]


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelAddOperation:
    """Test label 'add' operation (append labels, preserve existing)."""

    async def test_add_to_empty(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Add labels to entity with no existing labels."""
        entity_id = test_entity_id

        # Create test labels
        create_result_1 = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Add Test 1"}
        )
        label1_id = parse_mcp_result(create_result_1).get("label_id")
        cleanup_tracker.track("label", label1_id)

        create_result_2 = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Add Test 2"}
        )
        label2_id = parse_mcp_result(create_result_2).get("label_id")
        cleanup_tracker.track("label", label2_id)

        # Clear any existing labels first
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": []},
        )

        # Add first label
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "add", "labels": [label1_id]},
        )
        data = assert_mcp_success(result, "add first label")
        assert len(data.get("labels", [])) == 1
        assert label1_id in data.get("labels", [])
        logger.info(f"Added first label to empty entity: {label1_id}")

        # Add second label (should preserve first)
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "add", "labels": [label2_id]},
        )
        data = assert_mcp_success(result, "add second label")
        assert len(data.get("labels", [])) == 2
        assert label1_id in data.get("labels", [])
        assert label2_id in data.get("labels", [])
        logger.info("Added second label, first label preserved ✅")

    async def test_add_preserves_existing(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Add operation preserves existing labels."""
        entity_id = test_entity_id

        # Create labels
        labels = []
        for i in range(3):
            result = await mcp_client.call_tool(
                "ha_config_set_label", {"name": f"Preserve Test {i+1}"}
            )
            label_id = parse_mcp_result(result).get("label_id")
            cleanup_tracker.track("label", label_id)
            labels.append(label_id)

        # Set initial labels
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": labels[:2]},
        )

        # Add third label
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "add", "labels": [labels[2]]},
        )
        data = assert_mcp_success(result, "add to existing")

        final_labels = data.get("labels", [])
        assert len(final_labels) == 3
        assert all(lbl in final_labels for lbl in labels)
        logger.info("Add operation preserved all existing labels ✅")

    async def test_add_duplicate_idempotent(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Adding duplicate label is idempotent (no error, no duplicates)."""
        entity_id = test_entity_id

        # Create label
        result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Duplicate Test"}
        )
        label_id = parse_mcp_result(result).get("label_id")
        cleanup_tracker.track("label", label_id)

        # Add label twice
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "add", "labels": [label_id]},
        )

        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "add", "labels": [label_id]},
        )
        data = assert_mcp_success(result, "add duplicate")

        # Should still have only 1 instance
        final_labels = data.get("labels", [])
        assert final_labels.count(label_id) == 1
        logger.info("Adding duplicate label is idempotent ✅")


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelRemoveOperation:
    """Test label 'remove' operation (subtract labels, preserve remaining)."""

    async def test_remove_only_label(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Remove the only label (clears all)."""
        entity_id = test_entity_id

        # Create label
        result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Remove Only Test"}
        )
        label_id = parse_mcp_result(result).get("label_id")
        cleanup_tracker.track("label", label_id)

        # Set label
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": [label_id]},
        )

        # Remove label
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "remove", "labels": [label_id]},
        )
        data = assert_mcp_success(result, "remove only label")

        assert len(data.get("labels", [])) == 0
        logger.info("Removed only label, entity now has no labels ✅")

    async def test_remove_preserves_others(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Remove operation preserves non-specified labels."""
        entity_id = test_entity_id

        # Create 3 labels
        labels = []
        for i in range(3):
            result = await mcp_client.call_tool(
                "ha_config_set_label", {"name": f"Remove Preserve {i+1}"}
            )
            label_id = parse_mcp_result(result).get("label_id")
            cleanup_tracker.track("label", label_id)
            labels.append(label_id)

        # Set all 3 labels
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": labels},
        )

        # Remove middle label
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "remove", "labels": [labels[1]]},
        )
        data = assert_mcp_success(result, "remove middle label")

        final_labels = data.get("labels", [])
        assert len(final_labels) == 2
        assert labels[0] in final_labels
        assert labels[1] not in final_labels
        assert labels[2] in final_labels
        logger.info("Remove operation preserved other labels ✅")

    async def test_remove_nonexistent_safe(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Removing non-existent label is safe (no error)."""
        entity_id = test_entity_id

        # Create 2 labels
        labels = []
        for i in range(2):
            result = await mcp_client.call_tool(
                "ha_config_set_label", {"name": f"Remove Safe {i+1}"}
            )
            label_id = parse_mcp_result(result).get("label_id")
            cleanup_tracker.track("label", label_id)
            labels.append(label_id)

        # Set first label only
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": [labels[0]]},
        )

        # Try to remove second label (not present)
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "remove", "labels": [labels[1]]},
        )
        data = assert_mcp_success(result, "remove non-existent label")

        final_labels = data.get("labels", [])
        assert len(final_labels) == 1
        assert labels[0] in final_labels
        logger.info("Removing non-existent label is safe ✅")


@pytest.mark.labels
@pytest.mark.cleanup
class TestLabelSetOperation:
    """Test label 'set' operation (replace all labels)."""

    async def test_set_replaces_all(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Set operation replaces all existing labels."""
        entity_id = test_entity_id

        # Create labels
        labels = []
        for i in range(4):
            result = await mcp_client.call_tool(
                "ha_config_set_label", {"name": f"Set Replace {i+1}"}
            )
            label_id = parse_mcp_result(result).get("label_id")
            cleanup_tracker.track("label", label_id)
            labels.append(label_id)

        # Set first 2 labels
        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": labels[:2]},
        )

        # Set last 2 labels (should replace first 2)
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": labels[2:]},
        )
        data = assert_mcp_success(result, "set replacement")

        final_labels = data.get("labels", [])
        assert len(final_labels) == 2
        assert labels[0] not in final_labels
        assert labels[1] not in final_labels
        assert labels[2] in final_labels
        assert labels[3] in final_labels
        logger.info("Set operation replaced all previous labels ✅")

    async def test_set_empty_clears_all(self, mcp_client, cleanup_tracker, test_entity_id):
        """Test: Set with empty list clears all labels."""
        entity_id = test_entity_id

        # Create and set labels
        labels = []
        for i in range(2):
            result = await mcp_client.call_tool(
                "ha_config_set_label", {"name": f"Set Clear {i+1}"}
            )
            label_id = parse_mcp_result(result).get("label_id")
            cleanup_tracker.track("label", label_id)
            labels.append(label_id)

        await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": labels},
        )

        # Clear all with empty list
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": []},
        )
        data = assert_mcp_success(result, "set empty")

        assert len(data.get("labels", [])) == 0
        logger.info("Set with empty list cleared all labels ✅")


@pytest.mark.labels
@pytest.mark.cleanup
class TestBulkOperations:
    """Test bulk operations (multiple entities)."""

    async def test_bulk_parallel_add(self, mcp_client, cleanup_tracker, test_entity_ids):
        """Test: Bulk add operation in parallel mode."""
        entities = test_entity_ids

        # Create label
        result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Bulk Parallel Test"}
        )
        label_id = parse_mcp_result(result).get("label_id")
        cleanup_tracker.track("label", label_id)

        # Bulk add to multiple entities (parallel)
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {
                "entity_id": entities,
                "operation": "add",
                "labels": [label_id],
                "parallel": True,
            },
        )
        data = assert_mcp_success(result, "bulk parallel add")

        assert data.get("mode") == "bulk"
        assert data.get("execution_mode") == "parallel"
        assert data.get("total_operations") == 3
        assert data.get("successful") == 3
        assert data.get("failed") == 0
        logger.info(f"Bulk parallel add succeeded for {len(entities)} entities ✅")

    async def test_bulk_sequential_add(self, mcp_client, cleanup_tracker, test_entity_ids):
        """Test: Bulk add operation in sequential mode."""
        entities = test_entity_ids

        # Create label
        result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Bulk Sequential Test"}
        )
        label_id = parse_mcp_result(result).get("label_id")
        cleanup_tracker.track("label", label_id)

        # Bulk add to multiple entities (sequential)
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {
                "entity_id": entities,
                "operation": "add",
                "labels": [label_id],
                "parallel": False,
            },
        )
        data = assert_mcp_success(result, "bulk sequential add")

        assert data.get("mode") == "bulk"
        assert data.get("execution_mode") == "sequential"
        assert data.get("total_operations") == 3
        assert data.get("successful") == 3
        logger.info(f"Bulk sequential add succeeded for {len(entities)} entities ✅")

    async def test_bulk_partial_failure(self, mcp_client, cleanup_tracker, test_entity_ids):
        """Test: Bulk operation with some invalid entities (error isolation)."""
        entities = test_entity_ids[:2].copy()

        # Add invalid entity ID
        entities.append("light.nonexistent_entity_12345")

        # Create label
        result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "Bulk Partial Failure Test"}
        )
        label_id = parse_mcp_result(result).get("label_id")
        cleanup_tracker.track("label", label_id)

        # Bulk add (should succeed for valid, fail for invalid)
        result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {
                "entity_id": entities,
                "operation": "add",
                "labels": [label_id],
                "parallel": True,
            },
        )
        data = assert_mcp_success(result, "bulk partial failure")

        assert data.get("mode") == "bulk"
        assert data.get("total_operations") == 3
        assert data.get("successful") == 2
        assert data.get("failed") == 1
        logger.info("Bulk operation isolated failure to invalid entity ✅")


@pytest.mark.labels
@pytest.mark.cleanup
@pytest.mark.slow
class TestRegressionIssue396:
    """Regression test for Issue #396: Entity registry corruption from rapid operations."""

    async def test_rapid_operations_no_corruption(self, mcp_client, cleanup_tracker, test_entity_id):
        """
        Test: 13+ rapid label operations don't corrupt entity registry.

        Issue #396 reported that 5+ rapid label operations would corrupt
        the entity registry, making the label UI inaccessible.

        This test validates that the new implementation handles rapid
        operations correctly without corruption.
        """
        entity_id = test_entity_id

        # Create test labels
        labels = []
        for i in range(5):
            result = await mcp_client.call_tool(
                "ha_config_set_label", {"name": f"Rapid Op Label {i+1}"}
            )
            label_id = parse_mcp_result(result).get("label_id")
            cleanup_tracker.track("label", label_id)
            labels.append(label_id)

        logger.info("Starting rapid operation test (13 operations)...")

        # Perform 13 rapid operations (add/remove/set cycle)
        operations = []

        # Cycle 1: Add all labels one by one
        for label in labels:
            operations.append(("add", [label]))

        # Cycle 2: Remove half
        operations.append(("remove", labels[:2]))

        # Cycle 3: Set to different subset
        operations.append(("set", labels[2:4]))

        # Cycle 4: Add back
        for label in labels[:2]:
            operations.append(("add", [label]))

        # Cycle 5: Remove and re-add rapidly
        operations.append(("remove", [labels[0]]))
        operations.append(("add", [labels[0]]))
        operations.append(("remove", [labels[1]]))
        operations.append(("add", [labels[1]]))

        # Execute operations rapidly
        for idx, (operation, lbls) in enumerate(operations, 1):
            result = await mcp_client.call_tool(
                "ha_manage_entity_labels",
                {
                    "entity_id": entity_id,
                    "operation": operation,
                    "labels": lbls,
                },
            )
            assert_mcp_success(result, f"operation {idx}/{len(operations)}")
            logger.info(f"Operation {idx}/{len(operations)}: {operation} completed")

        logger.info(f"Completed {len(operations)} rapid operations")

        # Verify entity registry is still accessible (not corrupted)
        # 1. Can still list labels
        list_result = await mcp_client.call_tool("ha_config_get_label", {})
        assert_mcp_success(list_result, "list labels after rapid operations")

        # 2. Can still get entity state
        state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": entity_id}
        )
        assert_mcp_success(state_result, "get entity state after rapid operations")

        # 3. Can still modify labels
        final_result = await mcp_client.call_tool(
            "ha_manage_entity_labels",
            {"entity_id": entity_id, "operation": "set", "labels": []},
        )
        assert_mcp_success(final_result, "final label modification")

        logger.info("✅ Entity registry not corrupted after 13+ rapid operations")
        logger.info("✅ Issue #396 regression test PASSED")
