"""
Negative-input tests for ha_get_automation_traces.

Tests the domain-guard pre-flight validation path.
Placement: workflows/automation/ alongside happy-path traces tests.
"""

import logging

import pytest

from ...utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.automation
class TestGetAutomationTracesNegativeInputs:
    """Negative-input tests for ha_get_automation_traces.

    Covers the domain-guard path with no prior hard coverage.
    """

    async def test_wrong_domain_prefix_rejected(self, mcp_client):
        """ha_get_automation_traces with a non-automation/script entity_id is rejected pre-flight.

        Code path: tools_traces.py — domain-guard checks automation. and script. prefixes.
        Input "sensor.some_entity" matches neither → raise_tool_error(VALIDATION_INVALID_PARAMETER).
        No WebSocket call is made.
        No prior hard coverage in unit or E2E suite.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_get_automation_traces",
            {"automation_id": "sensor.some_entity"},
        )

        inner = result.get("data", result)

        assert inner["success"] is False, (
            f"Expected success=False for automation_id='sensor.some_entity', got: {inner}"
        )
        assert inner["error"]["code"] == "VALIDATION_INVALID_PARAMETER", (
            f"Expected VALIDATION_INVALID_PARAMETER (domain-guard), got: {inner}"
        )
