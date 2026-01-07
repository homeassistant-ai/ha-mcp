"""
E2E tests for Config Entry Flow API.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.slow
class TestConfigEntryFlow:
    """Test Config Entry Flow helper creation."""

    async def test_get_config_entry(self, mcp_client):
        """Test getting config entry details."""
        # Get any entry first
        list_result = await mcp_client.call_tool("ha_list_integrations", {})
        data = assert_mcp_success(list_result, "List integrations")

        if not data.get("entries"):
            pytest.skip("No config entries found")

        entry_id = data["entries"][0]["entry_id"]
        logger.info(f"Testing get_config_entry with: {entry_id}")

        result = await mcp_client.call_tool(
            "ha_get_config_entry", {"entry_id": entry_id}
        )
        data = assert_mcp_success(result, "Get config entry")
        assert "entry" in data, "Result should include entry data"

    async def test_get_config_entry_nonexistent(self, mcp_client):
        """Test error handling for non-existent config entry."""
        result = await mcp_client.call_tool(
            "ha_get_config_entry", {"entry_id": "nonexistent_entry_id"}
        )
        # Should fail with 404 or similar error
        data = parse_mcp_result(result)
        assert not data.get("success", False)

    # Note: Actual ha_create_config_entry_helper tests are intentionally limited
    # because they require specific configuration for each helper type.
    # These tests would need to be expanded once we understand the exact
    # flow requirements for each of the 15 supported helpers.

    async def test_create_config_entry_helper_exists(self, mcp_client):
        """Test that ha_create_config_entry_helper tool exists."""
        # This is a basic sanity check that the tool is registered
        # We don't actually call it because we don't know valid configs yet

        # Try to call with minimal/invalid config to verify tool exists
        result = await mcp_client.call_tool(
            "ha_create_config_entry_helper",
            {"helper_type": "template", "config": {}},
        )

        # We expect this to fail (invalid config), but it proves tool exists
        # and validates the structure
        data = parse_mcp_result(result)
        assert "success" in data, "Tool should return a result with success field"

        # If it succeeded unexpectedly, that's also fine - means empty config worked
        # If it failed, that's expected - we're just testing tool existence
        logger.info(
            f"Tool response (expected to fail with invalid config): {data.get('success')}"
        )
