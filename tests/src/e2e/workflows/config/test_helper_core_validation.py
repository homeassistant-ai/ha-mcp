"""
E2E tests for storage-helper fields judged by Home Assistant itself (#2632).

ha_config_set_helper sends a storage helper's fields to Home Assistant under
its own names, and Home Assistant's schema decides what is valid. These tests
pin that a rejected call reaches the caller as a validation error naming the
problem, writes nothing, and that the few unusable configurations Home
Assistant stores anyway are still refused by the tool.
"""

import uuid

import pytest

from ...utilities.assertions import MCPAssertions, assert_mcp_success


async def _helper_names(mcp_client, helper_type: str) -> set[str]:
    data = assert_mcp_success(
        await mcp_client.call_tool(
            "ha_config_list_helpers", {"helper_type": helper_type, "limit": 500}
        ),
        f"list {helper_type}",
    )
    return {h.get("name") for h in data["helpers"]}


async def _rejected(mcp_client, params: dict, expected_error: str | None) -> dict:
    async with MCPAssertions(mcp_client) as mcp:
        result = await mcp.call_tool_failure(
            "ha_config_set_helper", params, expected_error=expected_error
        )
    assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER", result
    return result


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.helper
class TestHomeAssistantJudgesStorageHelperFields:
    async def test_unknown_key_is_named_by_home_assistant_and_nothing_is_written(
        self, mcp_client
    ) -> None:
        """A misspelt field is reported by name instead of being dropped."""
        name = f"E2E Typo {uuid.uuid4().hex[:6]}"
        await _rejected(
            mcp_client,
            {
                "helper_type": "input_number",
                "name": name,
                "config": {"min": 0, "max": 10, "stepp": 2},
            },
            expected_error="stepp",
        )
        assert name not in await _helper_names(mcp_client, "input_number")

    async def test_field_of_another_helper_type_is_rejected(self, mcp_client) -> None:
        """An input_select option list on an input_boolean is not silently lost
        (issue #1150)."""
        await _rejected(
            mcp_client,
            {
                "helper_type": "input_boolean",
                "name": f"E2E Wrong Field {uuid.uuid4().hex[:6]}",
                "options": ["a", "b"],
            },
            expected_error="options",
        )

    @pytest.mark.parametrize(
        "params",
        [
            {"helper_type": "input_number", "min_value": 100, "max_value": 0},
            {"helper_type": "input_datetime", "has_date": False, "has_time": False},
            {"helper_type": "input_select"},  # options are required
        ],
        ids=["input_number-min-above-max", "input_datetime-neither", "no-options"],
    )
    async def test_invalid_values_are_rejected_by_home_assistant(
        self, mcp_client, params: dict
    ) -> None:
        await _rejected(
            mcp_client,
            {**params, "name": f"E2E Invalid {uuid.uuid4().hex[:6]}"},
            expected_error=None,
        )

    async def test_step_wider_than_the_range_is_refused_by_the_tool(
        self, mcp_client
    ) -> None:
        """Home Assistant stores this counter, but it could never change value."""
        name = f"E2E Wide Step {uuid.uuid4().hex[:6]}"
        await _rejected(
            mcp_client,
            {
                "helper_type": "counter",
                "name": name,
                "config": {"minimum": 0, "maximum": 3, "step": 5},
            },
            expected_error="step",
        )
        assert name not in await _helper_names(mcp_client, "counter")
