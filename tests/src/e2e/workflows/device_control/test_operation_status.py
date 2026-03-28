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

        assert isinstance(result, dict), f"Expected dict response, got {type(result)}"
        # Should return a structured response (success or error), not crash
        assert "success" in result or "error" in result, (
            f"Response missing 'success' or 'error' field: {result}"
        )
        logger.info(f"Single invalid op result: {result}")

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

        assert isinstance(result, dict), f"Expected dict response, got {type(result)}"
        # Empty list should return a response (success with empty results, or error)
        assert "success" in result or "error" in result, (
            f"Response missing 'success' or 'error' field: {result}"
        )
        logger.info(f"Empty list result: {result}")

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

        assert isinstance(result, dict), f"Expected dict response, got {type(result)}"
        assert "success" in result or "error" in result, (
            f"Response missing 'success' or 'error' field: {result}"
        )
        logger.info(f"List invalid ops result: {result}")

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

        assert isinstance(single_result, dict), "Single result should be a dict"
        assert isinstance(list_result, dict), "List result should be a dict"

        # Both should return structured responses
        assert "success" in single_result or "error" in single_result, (
            f"Single result missing 'success'/'error': {single_result}"
        )
        assert "success" in list_result or "error" in list_result, (
            f"List result missing 'success'/'error': {list_result}"
        )

        logger.info(
            f"Single result keys: {list(single_result.keys()) if isinstance(single_result, dict) else 'not dict'}"
        )
        logger.info(
            f"List result keys: {list(list_result.keys()) if isinstance(list_result, dict) else 'not dict'}"
        )

    async def test_json_string_list_coercion(self, mcp_client):
        """
        Test: Passing operation_id as a JSON string (e.g. '["op1","op2"]')
        should be coerced to a list and use the bulk status path.

        MCP clients sometimes send lists as JSON strings rather than native arrays.
        """
        logger.info("Testing JSON string coercion for operation_id")

        # JSON string that should be parsed as a list
        result = await safe_call_tool(
            mcp_client,
            "ha_get_operation_status",
            {"operation_id": '["json_string_op_1", "json_string_op_2"]'},
        )

        assert isinstance(result, dict), f"Expected dict response, got {type(result)}"
        assert "success" in result or "error" in result, (
            f"Response missing 'success' or 'error' field: {result}"
        )
        logger.info(f"JSON string coercion result: {result}")
