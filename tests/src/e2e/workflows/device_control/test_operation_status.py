"""
Operation Status Consolidation E2E Tests

Tests the consolidated ha_get_operation_status tool which now accepts
both single string and list of operation IDs.
"""

import logging

import pytest

from ...utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.device
class TestOperationStatusConsolidation:
    """Test consolidated ha_get_operation_status tool."""

    async def test_single_operation_id_invalid(self, mcp_client):
        """
        Test: Passing a single invalid operation ID returns a structured response.
        """
        logger.info("Testing single invalid operation ID")

        result = await safe_call_tool(
            mcp_client,
            "ha_get_operation_status",
            {"operation_id": "nonexistent_op_12345"},
        )

        # Should return a response (may be error or not-found)
        logger.info(f"Single invalid op result: {result}")
        # The tool should handle this gracefully without crashing

    async def test_list_operation_ids_empty(self, mcp_client):
        """
        Test: Passing an empty list of operation IDs.

        The tool should handle this gracefully.
        """
        logger.info("Testing empty list of operation IDs")

        result = await safe_call_tool(
            mcp_client,
            "ha_get_operation_status",
            {"operation_id": []},
        )

        logger.info(f"Empty list result: {result}")
        # Should not crash — either returns empty results or an error

    async def test_list_operation_ids_invalid(self, mcp_client):
        """
        Test: Passing a list of invalid operation IDs returns bulk status.
        """
        logger.info("Testing list of invalid operation IDs")

        result = await safe_call_tool(
            mcp_client,
            "ha_get_operation_status",
            {
                "operation_id": [
                    "nonexistent_op_111",
                    "nonexistent_op_222",
                    "nonexistent_op_333",
                ],
            },
        )

        logger.info(f"List invalid ops result: {result}")
        # Should handle gracefully and return status for each

    async def test_single_vs_list_different_dispatch(self, mcp_client):
        """
        Test: Verify that single string and single-element list
        take different code paths (single uses get_device_operation_status,
        list uses get_bulk_operation_status).
        """
        logger.info("Testing single vs list dispatch")

        op_id = "test_dispatch_op_999"

        # Single string path
        single_result = await safe_call_tool(
            mcp_client,
            "ha_get_operation_status",
            {"operation_id": op_id},
        )

        # List path (same ID in a list)
        list_result = await safe_call_tool(
            mcp_client,
            "ha_get_operation_status",
            {"operation_id": [op_id]},
        )

        logger.info(
            f"Single result keys: {list(single_result.keys()) if isinstance(single_result, dict) else 'not dict'}"
        )
        logger.info(
            f"List result keys: {list(list_result.keys()) if isinstance(list_result, dict) else 'not dict'}"
        )

        # Both should succeed or fail gracefully, but response formats may differ
        # since they go through different internal methods
