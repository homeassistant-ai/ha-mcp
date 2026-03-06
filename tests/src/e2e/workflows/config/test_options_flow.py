"""
E2E tests for Config Entry Options Flow tools.

Covers:
- Starting and aborting an options flow (inspect without saving)
- Submitting an invalid flow_id returns a structured error
- Aborting an already-gone flow is handled gracefully
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


async def _find_options_entry_id(mcp_client) -> str | None:
    """Return the first config entry that supports an options flow, or None."""
    result = await mcp_client.call_tool("ha_get_integration", {})
    data = assert_mcp_success(result, "List integrations")
    for entry in data.get("entries", []):
        if entry.get("supports_options"):
            return entry["entry_id"]
    return None


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestOptionsFlow:
    """Tests for ha_start_options_flow, ha_submit_options_flow_step, ha_abort_options_flow."""

    async def test_start_and_abort_options_flow(self, mcp_client):
        """Starting an options flow returns a valid flow_id; aborting cleans it up."""
        entry_id = await _find_options_entry_id(mcp_client)
        if entry_id is None:
            pytest.skip("No config entries with supports_options=true in test environment")

        # Start the flow
        result = await mcp_client.call_tool(
            "ha_start_options_flow", {"entry_id": entry_id}
        )
        data = assert_mcp_success(result, "Start options flow")

        assert data.get("flow_id") is not None, "Expected flow_id in response"
        assert data.get("type") in ("form", "menu"), (
            f"Expected type 'form' or 'menu', got '{data.get('type')}'"
        )
        flow_id = data["flow_id"]
        logger.info(f"Options flow started: flow_id={flow_id}, type={data.get('type')}")

        # Abort without saving
        abort_result = await mcp_client.call_tool(
            "ha_abort_options_flow", {"flow_id": flow_id}
        )
        abort_data = assert_mcp_success(abort_result, "Abort options flow")
        assert abort_data.get("success") is True

    async def test_start_options_flow_nonexistent_entry(self, mcp_client):
        """Starting an options flow for a non-existent entry returns an error."""
        data = await safe_call_tool(
            mcp_client,
            "ha_start_options_flow",
            {"entry_id": "nonexistent_entry_00000"},
        )
        assert data.get("success") is not True, "Should fail for nonexistent entry"

    async def test_submit_options_flow_invalid_flow_id(self, mcp_client):
        """Submitting to a non-existent flow_id returns a structured error."""
        data = await safe_call_tool(
            mcp_client,
            "ha_submit_options_flow_step",
            {"flow_id": "invalid_flow_id_xyz", "data": {"key": "value"}},
        )
        assert data.get("success") is not True, "Should fail for invalid flow_id"

    async def test_abort_nonexistent_flow_is_graceful(self, mcp_client):
        """Aborting a flow that doesn't exist returns an error (not a crash)."""
        data = await safe_call_tool(
            mcp_client,
            "ha_abort_options_flow",
            {"flow_id": "nonexistent_flow_xyz"},
        )
        # Should fail with a structured error, not crash
        assert data.get("success") is not True, "Should fail for nonexistent flow_id"
