"""Unit tests for bulk_device_control validation in device_control module."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.device_control import DeviceControlTools


class TestBulkDeviceControlValidation:
    """Test bulk_device_control validation logic."""

    @pytest.fixture
    def device_control_tools(self):
        """Create DeviceControlTools with mocked client."""
        # Pass None client - we won't actually make calls for validation tests
        return DeviceControlTools(client=None)

    @pytest.mark.asyncio
    async def test_empty_operations_returns_error(self, device_control_tools):
        """Empty operations list raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control([])
        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False
        assert "No operations provided" in error_data["error"]["message"]

    @pytest.mark.asyncio
    async def test_missing_entity_id_reports_error(self, device_control_tools):
        """Operations missing entity_id are reported in skipped_operations."""
        operations = [
            {"action": "on"},  # Missing entity_id
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["total_operations"] == 1
        assert result["skipped_operations"] == 1
        assert len(result["skipped_details"]) == 1
        assert "entity_id" in result["skipped_details"][0]["error"]["message"]
        assert result["skipped_details"][0]["index"] == 0

    @pytest.mark.asyncio
    async def test_missing_action_reports_error(self, device_control_tools):
        """Operations missing action are reported in skipped_operations."""
        operations = [
            {"entity_id": "light.test"},  # Missing action
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["total_operations"] == 1
        assert result["skipped_operations"] == 1
        assert len(result["skipped_details"]) == 1
        assert "action" in result["skipped_details"][0]["error"]["message"]

    @pytest.mark.asyncio
    async def test_missing_both_fields_reports_both(self, device_control_tools):
        """Operations missing both fields report both missing fields."""
        operations = [
            {},  # Missing both entity_id and action
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["skipped_operations"] == 1
        error_msg = result["skipped_details"][0]["error"]["message"]
        assert "entity_id" in error_msg
        assert "action" in error_msg

    @pytest.mark.asyncio
    async def test_non_dict_operation_reports_error(self, device_control_tools):
        """Non-dict operations are reported as errors."""
        operations = [
            "not a dict",
            123,
            None,
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["total_operations"] == 3
        assert result["skipped_operations"] == 3
        assert len(result["skipped_details"]) == 3
        for detail in result["skipped_details"]:
            assert "not a dict" in detail["error"]["message"]

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_operations(self, device_control_tools):
        """Mix of valid and invalid operations reports skipped ones.

        Note: This test only validates that invalid operations are tracked.
        Valid operations would require a real HA connection to execute.
        """
        operations = [
            {"entity_id": "light.test", "action": "on"},  # Valid (but will fail without HA)
            {"action": "off"},  # Invalid - missing entity_id
            {"entity_id": "switch.test"},  # Invalid - missing action
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["total_operations"] == 3
        assert result["skipped_operations"] == 2
        # The valid operation would be attempted but fail (no client)
        # so we check that skipped operations are properly tracked
        assert len(result["skipped_details"]) == 2

        # Verify indices are tracked correctly
        skipped_indices = [d["index"] for d in result["skipped_details"]]
        assert 1 in skipped_indices  # Missing entity_id
        assert 2 in skipped_indices  # Missing action

    @pytest.mark.asyncio
    async def test_all_invalid_operations_has_suggestions(self, device_control_tools):
        """When operations are skipped, response includes suggestions."""
        operations = [
            {"action": "on"},  # Invalid
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert "suggestions" in result
        assert any("entity_id" in s for s in result["suggestions"])
        assert any("action" in s for s in result["suggestions"])

    @pytest.mark.asyncio
    async def test_skipped_details_includes_original_operation(self, device_control_tools):
        """Skipped details include the original operation for debugging."""
        original_op = {"action": "on", "parameters": {"brightness": 100}}
        operations = [original_op]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["skipped_details"][0]["operation"] == original_op

    @pytest.mark.asyncio
    async def test_sequential_execution_validates_operations(self, device_control_tools):
        """Sequential execution mode also validates operations."""
        operations = [
            {"action": "on"},  # Missing entity_id
        ]
        result = await device_control_tools.bulk_device_control(
            operations, parallel=False
        )

        assert result["skipped_operations"] == 1
        assert result["execution_mode"] == "sequential"


class TestBulkExecutionErrorHandling:
    """Test error handling semantics in parallel and sequential bulk execution."""

    @pytest.fixture
    def tools_with_mock_control(self):
        """Create DeviceControlTools with mocked control_device_smart."""
        tools = DeviceControlTools(client=MagicMock())
        tools._ensure_websocket_listener = AsyncMock()  # type: ignore[method-assign]
        return tools

    @pytest.mark.asyncio
    async def test_sequential_continues_after_tool_error(self, tools_with_mock_control):
        """Sequential execution no longer aborts on a single ToolError (fail-soft)."""
        tools_with_mock_control.control_device_smart = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"entity_id": "light.ok", "command_sent": True, "operation_id": "op1"},
                ToolError(json.dumps(create_error_response(
                    ErrorCode.ENTITY_NOT_FOUND, "Entity not found: light.missing",
                ))),
                {"entity_id": "light.also_ok", "command_sent": True, "operation_id": "op3"},
            ]
        )

        operations = [
            {"entity_id": "light.ok", "action": "on"},
            {"entity_id": "light.missing", "action": "on"},
            {"entity_id": "light.also_ok", "action": "on"},
        ]
        result = await tools_with_mock_control.bulk_device_control(operations, parallel=False)

        assert result["total_operations"] == 3
        assert result["successful_commands"] == 2
        assert len(result["results"]) == 3
        # Middle op's structured code survived, not flattened into a string
        assert result["results"][1]["error"]["code"] == ErrorCode.ENTITY_NOT_FOUND

    @pytest.mark.asyncio
    async def test_parallel_preserves_tool_error_code(self, tools_with_mock_control):
        """Parallel execution preserves the structured ErrorCode from a ToolError."""
        tools_with_mock_control.control_device_smart = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {"entity_id": "light.ok", "command_sent": True, "operation_id": "op1"},
                ToolError(json.dumps(create_error_response(
                    ErrorCode.VALIDATION_INVALID_JSON, "Invalid JSON in parameters",
                ))),
            ]
        )

        operations = [
            {"entity_id": "light.ok", "action": "on"},
            {"entity_id": "light.bad", "action": "on", "parameters": "{not-json"},
        ]
        result = await tools_with_mock_control.bulk_device_control(operations, parallel=True)

        assert result["total_operations"] == 2
        assert result["successful_commands"] == 1
        # Structured code preserved, not flattened to SERVICE_CALL_FAILED
        assert result["results"][1]["error"]["code"] == ErrorCode.VALIDATION_INVALID_JSON
