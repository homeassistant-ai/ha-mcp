"""Unit tests for bulk_device_control validation in device_control module."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.errors import ErrorCode, create_error_response
from ha_mcp.tools.device_control import DeviceControlTools


def _all_invalid_payload(exc_info) -> dict:
    """Parse the structured payload of an all-invalid batch ToolError."""
    payload = json.loads(str(exc_info.value))
    assert payload["success"] is False
    return payload


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
        """A batch where every row is invalid fails the call, not just the rows."""
        device_control_tools._bulk_via_component = AsyncMock()
        operations = [
            {"action": "on"},  # Missing entity_id
        ]

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control(operations)

        payload = _all_invalid_payload(exc_info)
        assert "All 1 operation(s) failed validation" in payload["error"]["message"]
        assert len(payload["skipped_details"]) == 1
        assert "entity_id" in payload["skipped_details"][0]["error"]["message"]
        assert payload["skipped_details"][0]["index"] == 0
        device_control_tools._bulk_via_component.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_missing_action_reports_error(self, device_control_tools):
        """The missing field is named in the raised payload's skipped_details."""
        operations = [
            {"entity_id": "light.test"},  # Missing action
        ]

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control(operations)

        payload = _all_invalid_payload(exc_info)
        assert len(payload["skipped_details"]) == 1
        assert "action" in payload["skipped_details"][0]["error"]["message"]

    @pytest.mark.asyncio
    async def test_missing_both_fields_reports_both(self, device_control_tools):
        """Operations missing both fields report both missing fields."""
        operations = [
            {},  # Missing both entity_id and action
        ]

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control(operations)

        payload = _all_invalid_payload(exc_info)
        error_msg = payload["skipped_details"][0]["error"]["message"]
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

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control(operations)

        payload = _all_invalid_payload(exc_info)
        assert "All 3 operation(s) failed validation" in payload["error"]["message"]
        assert len(payload["skipped_details"]) == 3
        for detail in payload["skipped_details"]:
            assert "not a dict" in detail["error"]["message"]

    @pytest.mark.slow
    @pytest.mark.asyncio
    async def test_mixed_valid_and_invalid_operations(self, device_control_tools):
        """Mix of valid and invalid operations reports skipped ones.

        Note: This test only validates that invalid operations are tracked.
        Valid operations would require a real HA connection to execute.
        """
        operations = [
            {
                "entity_id": "light.test",
                "action": "on",
            },  # Valid (but will fail without HA)
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
    async def test_obsolete_key_skips_only_that_operation(self, device_control_tools):
        """A service-style key fails one row without rejecting the batch."""
        device_control_tools._bulk_via_component = AsyncMock(return_value=None)
        device_control_tools.control_device_smart = AsyncMock(
            return_value={
                "entity_id": "light.valid",
                "command_sent": True,
                "operation_id": "op-valid",
            }
        )
        operations = [
            {"entity_id": "light.invalid", "action": "off", "service": "turn_off"},
            {"entity_id": "light.valid", "action": "off"},
        ]

        result = await device_control_tools.bulk_device_control(operations)

        assert result["skipped_operations"] == 1
        assert result["successful_commands"] == 1
        message = result["skipped_details"][0]["error"]["message"]
        assert "service" in message
        assert result["skipped_details"][0]["index"] == 0
        device_control_tools._bulk_via_component.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_all_invalid_operations_has_suggestions(self, device_control_tools):
        """The raised payload carries the actionable row-shape guidance."""
        operations = [
            {"action": "on"},  # Invalid
        ]

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control(operations)

        suggestions = _all_invalid_payload(exc_info)["error"]["suggestions"]
        assert any("entity_id" in s for s in suggestions)
        assert any("action" in s for s in suggestions)
        assert any("service='turn_off'" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_skipped_details_includes_original_operation(
        self, device_control_tools
    ):
        """Skipped details include the original operation for debugging."""
        original_op = {"action": "on", "parameters": {"brightness": 100}}

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control([original_op])

        payload = _all_invalid_payload(exc_info)
        assert payload["skipped_details"][0]["operation"] == original_op

    @pytest.mark.asyncio
    async def test_sequential_execution_validates_operations(
        self, device_control_tools
    ):
        """Sequential execution mode validates before it reaches the mode split."""
        operations = [
            {"action": "on"},  # Missing entity_id
        ]

        with pytest.raises(ToolError) as exc_info:
            await device_control_tools.bulk_device_control(operations, parallel=False)

        assert len(_all_invalid_payload(exc_info)["skipped_details"]) == 1


class TestRegisteredBulkToolCompatibility:
    """Pin transport validation and model-facing fail-soft responses together."""

    @staticmethod
    async def _registered_tool(device_tools):
        from fastmcp import FastMCP

        from ha_mcp.tools.tools_service import register_service_tools

        mcp = FastMCP("test")
        register_service_tools(mcp, MagicMock(), device_tools=device_tools)
        return await mcp.get_tool("ha_bulk_control")

    @pytest.mark.asyncio
    async def test_obsolete_key_names_key_and_valid_row_still_executes(self):
        tools = DeviceControlTools(client=MagicMock())
        tools._bulk_via_component = AsyncMock(return_value=None)
        tools.control_device_smart = AsyncMock(
            return_value={
                "entity_id": "light.valid",
                "command_sent": True,
                "operation_id": "op-valid",
            }
        )
        tool = await self._registered_tool(tools)

        result = await tool.run(
            {
                "operations": [
                    {
                        "entity_id": "light.invalid",
                        "action": "off",
                        "service": "turn_off",
                    },
                    {"entity_id": "light.valid", "action": "off"},
                ]
            }
        )

        data = result.structured_content
        assert data["successful_commands"] == 1
        assert data["skipped_operations"] == 1
        assert "service" in data["skipped_details"][0]["error"]["message"]

    @pytest.mark.asyncio
    async def test_mixed_schema_invalid_rows_are_reported_without_aborting(self):
        """Transport validation defers malformed rows to skipped_details."""
        tools = DeviceControlTools(client=MagicMock())
        tools._bulk_via_component = AsyncMock(return_value=None)
        tools.control_device_smart = AsyncMock(
            return_value={
                "entity_id": "light.valid",
                "command_sent": True,
                "operation_id": "op-valid",
            }
        )
        tool = await self._registered_tool(tools)

        result = await tool.run(
            {
                "operations": [
                    {"entity_id": "", "action": "off"},
                    {
                        "entity_id": "light.negative_timeout",
                        "action": "off",
                        "timeout_seconds": -0.5,
                    },
                    {
                        "entity_id": "light.bad_parameters",
                        "action": "on",
                        "parameters": "{not-json",
                    },
                    {
                        "entity_id": "light.null_timeout",
                        "action": "off",
                        "timeout_seconds": None,
                    },
                    {
                        "entity_id": "light.null_validation",
                        "action": "off",
                        "validate_first": None,
                    },
                    {"entity_id": "light.valid", "action": "off"},
                    {
                        "entity_id": "light.integer_validation",
                        "action": "off",
                        "validate_first": 1,
                    },
                    {
                        "entity_id": "light.string_validation",
                        "action": "off",
                        "validate_first": "false",
                    },
                    # float(True) is 1.0, so a bool must be rejected outright
                    # rather than silently becoming a one-second timeout.
                    {
                        "entity_id": "light.bool_timeout",
                        "action": "off",
                        "timeout_seconds": True,
                    },
                    # The advertised schema is strict, so a numeric string must
                    # not be quietly coerced at runtime either.
                    {
                        "entity_id": "light.string_timeout",
                        "action": "off",
                        "timeout_seconds": "10",
                    },
                    # Present-but-null parameters are malformed rows, matching
                    # timeout_seconds and validate_first — in both spellings.
                    {
                        "entity_id": "light.null_parameters",
                        "action": "off",
                        "parameters": None,
                    },
                    {
                        "entity_id": "light.json_null_parameters",
                        "action": "off",
                        "parameters": "null",
                    },
                    # float() of an int this size raises OverflowError, which
                    # must skip the row rather than abort the whole batch.
                    {
                        "entity_id": "light.oversized_timeout",
                        "action": "off",
                        "timeout_seconds": 10**400,
                    },
                ]
            }
        )

        data = result.structured_content
        assert data["successful_commands"] == 1
        assert data["skipped_operations"] == 12
        assert [detail["index"] for detail in data["skipped_details"]] == [
            0,
            1,
            2,
            3,
            4,
            6,
            7,
            8,
            9,
            10,
            11,
            12,
        ]
        messages = [detail["error"]["message"] for detail in data["skipped_details"]]
        assert any("required fields" in message for message in messages)
        # The decoder's own reason and offset ride along, so the sender can see
        # where its string went wrong rather than just that it did.
        json_message = next(m for m in messages if "invalid JSON parameters" in m)
        assert "at position 1" in json_message
        assert sum("validate_first" in message for message in messages) == 3
        assert sum("timeout_seconds" in message for message in messages) == 5
        assert sum("must be a JSON object" in message for message in messages) == 2
        tools.control_device_smart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_string_identifiers_and_non_object_parameters_are_skipped(self):
        """Two branches the mixed-row test never reached.

        A numeric ``entity_id`` is truthy, so it clears the missing-fields
        check and lands on the string-type check; a ``parameters`` string that
        parses to a list clears the JSON decode and lands on the object check.
        Both differ from the malformed-JSON case already covered.
        """
        tools = DeviceControlTools(client=MagicMock())
        tools._bulk_via_component = AsyncMock(return_value=None)
        tools.control_device_smart = AsyncMock(
            return_value={
                "entity_id": "light.valid",
                "command_sent": True,
                "operation_id": "op-valid",
            }
        )
        tool = await self._registered_tool(tools)

        result = await tool.run(
            {
                "operations": [
                    {"entity_id": 123, "action": "off"},
                    {
                        "entity_id": "light.list_parameters",
                        "action": "on",
                        "parameters": "[1, 2]",
                    },
                    {"entity_id": "light.valid", "action": "off"},
                ]
            }
        )

        data = result.structured_content
        assert data["successful_commands"] == 1
        assert [detail["index"] for detail in data["skipped_details"]] == [0, 1]
        messages = [detail["error"]["message"] for detail in data["skipped_details"]]
        assert "requires string entity_id and action" in messages[0]
        assert "parameters must be a JSON object" in messages[1]

    @pytest.mark.asyncio
    async def test_nested_json_parameters_round_trip_through_registered_tool(self):
        tools = DeviceControlTools(client=MagicMock())
        tools._bulk_via_component = AsyncMock(return_value=None)
        tools.control_device_smart = AsyncMock(
            return_value={
                "entity_id": "light.kitchen",
                "command_sent": True,
                "operation_id": "op-1",
            }
        )
        tool = await self._registered_tool(tools)

        await tool.run(
            {
                "operations": [
                    {
                        "entity_id": "light.kitchen",
                        "action": "on",
                        "parameters": '{"brightness_pct": 42}',
                    }
                ]
            }
        )

        assert tools.control_device_smart.await_args.kwargs["parameters"] == {
            "brightness_pct": 42
        }


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
                ToolError(
                    json.dumps(
                        create_error_response(
                            ErrorCode.ENTITY_NOT_FOUND,
                            "Entity not found: light.missing",
                        )
                    )
                ),
                {
                    "entity_id": "light.also_ok",
                    "command_sent": True,
                    "operation_id": "op3",
                },
            ]
        )

        operations = [
            {"entity_id": "light.ok", "action": "on"},
            {"entity_id": "light.missing", "action": "on"},
            {"entity_id": "light.also_ok", "action": "on"},
        ]
        result = await tools_with_mock_control.bulk_device_control(
            operations, parallel=False
        )

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
                ToolError(
                    json.dumps(
                        create_error_response(
                            ErrorCode.VALIDATION_INVALID_JSON,
                            "Invalid JSON in parameters",
                        )
                    )
                ),
            ]
        )

        operations = [
            {"entity_id": "light.ok", "action": "on"},
            {"entity_id": "light.bad", "action": "on"},
        ]
        result = await tools_with_mock_control.bulk_device_control(
            operations, parallel=True
        )

        assert result["total_operations"] == 2
        assert result["successful_commands"] == 1
        # Structured code preserved, not flattened to SERVICE_CALL_FAILED
        assert (
            result["results"][1]["error"]["code"] == ErrorCode.VALIDATION_INVALID_JSON
        )


class TestLightParameterForwarding:
    """Light action parameters must survive service-call construction."""

    def test_brightness_pct_is_forwarded(self):
        tools = DeviceControlTools(client=MagicMock())

        service_call = tools._build_service_call(
            "light.kitchen", "light", "on", {"brightness_pct": 37}
        )

        assert service_call["data"]["brightness_pct"] == 37
