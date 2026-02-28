"""
E2E tests for generic integration options inspection.
"""

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success


@pytest.mark.asyncio
@pytest.mark.integrations
class TestIntegrationOptions:
    """Test generic options-flow inspection for integrations."""

    async def _get_entry_with_options(self, mcp_client):
        result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(result, "list integrations")

        for entry in data.get("entries", []):
            if entry.get("supports_options"):
                return entry
        return None

    async def test_get_integration_options_success(self, mcp_client):
        entry = await self._get_entry_with_options(mcp_client)
        if not entry:
            pytest.skip("No integration with supports_options=true found")

        result = await mcp_client.call_tool(
            "ha_get_integration_options",
            {"entry_id": entry["entry_id"], "include_options_flow": True},
        )
        data = assert_mcp_success(result, "get integration options")

        assert data["entry_id"] == entry["entry_id"]
        assert "options" in data
        assert "options_flow" in data
        assert "type" in data["options_flow"]

    async def test_get_integration_options_without_flow(self, mcp_client):
        entry = await self._get_entry_with_options(mcp_client)
        if not entry:
            pytest.skip("No integration with supports_options=true found")

        result = await mcp_client.call_tool(
            "ha_get_integration_options",
            {"entry_id": entry["entry_id"], "include_options_flow": False},
        )
        data = assert_mcp_success(result, "get integration options without flow")

        assert data["entry_id"] == entry["entry_id"]
        assert "options" in data
        assert "options_flow" not in data
