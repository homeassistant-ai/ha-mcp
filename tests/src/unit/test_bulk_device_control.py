"""Unit tests for bulk_device_control validation in device_control module."""

import pytest

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
        """Empty operations list returns error."""
        result = await device_control_tools.bulk_device_control([])
        assert result["success"] is False
        assert "No operations provided" in result["error"]

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
        assert "entity_id" in result["skipped_details"][0]["error"]
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
        assert "action" in result["skipped_details"][0]["error"]

    @pytest.mark.asyncio
    async def test_missing_both_fields_reports_both(self, device_control_tools):
        """Operations missing both fields report both missing fields."""
        operations = [
            {},  # Missing both entity_id and action
        ]
        result = await device_control_tools.bulk_device_control(operations)

        assert result["skipped_operations"] == 1
        error_msg = result["skipped_details"][0]["error"]
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
            assert "not a dict" in detail["error"]

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
