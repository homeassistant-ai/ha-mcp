"""
E2E tests for integration management tools.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import (
    assert_mcp_failure,
    assert_mcp_success,
)

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.integrations
class TestIntegrationManagement:
    """Test integration enable/disable/delete operations."""

    async def test_set_integration_enabled_cycle(self, mcp_client):
        """Test full enable/disable/re-enable cycle."""
        # Find suitable integration (supports_unload=True)
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(list_result, "List integrations")

        # Find test integration
        test_entry = None
        for entry in data.get("entries", []):
            if entry.get("supports_unload") and entry.get("state") == "loaded":
                test_entry = entry
                break

        if not test_entry:
            pytest.skip("No suitable integration found for testing")

        entry_id = test_entry["entry_id"]
        logger.info(f"Testing with integration: {test_entry['title']}")

        # DISABLE
        disable_result = await mcp_client.call_tool(
            "ha_set_integration_enabled", {"entry_id": entry_id, "enabled": False}
        )
        assert_mcp_success(disable_result, "Disable integration")

        # Verify disabled
        list_result = await mcp_client.call_tool(
            "ha_get_integration", {"query": test_entry["domain"]}
        )
        data = assert_mcp_success(list_result, "List after disable")
        entry = next(e for e in data["entries"] if e["entry_id"] == entry_id)
        assert entry["disabled_by"] == "user", "Integration should be disabled by user"

        # RE-ENABLE
        enable_result = await mcp_client.call_tool(
            "ha_set_integration_enabled", {"entry_id": entry_id, "enabled": True}
        )
        assert_mcp_success(enable_result, "Re-enable integration")

        # Verify re-enabled
        list_result = await mcp_client.call_tool(
            "ha_get_integration", {"query": test_entry["domain"]}
        )
        data = assert_mcp_success(list_result, "List after enable")
        entry = next(e for e in data["entries"] if e["entry_id"] == entry_id)
        assert (
            entry["disabled_by"] is None
        ), "Integration should not be disabled after re-enable"

    async def test_set_integration_enabled_string_bool(self, mcp_client):
        """Test that enabled parameter accepts string booleans."""
        # Find suitable integration
        list_result = await mcp_client.call_tool("ha_get_integration", {})
        data = assert_mcp_success(list_result, "List integrations")

        test_entry = None
        for entry in data.get("entries", []):
            if entry.get("supports_unload") and entry.get("state") == "loaded":
                test_entry = entry
                break

        if not test_entry:
            pytest.skip("No suitable integration found for testing")

        entry_id = test_entry["entry_id"]

        # Test with string "false"
        disable_result = await mcp_client.call_tool(
            "ha_set_integration_enabled", {"entry_id": entry_id, "enabled": "false"}
        )
        assert_mcp_success(disable_result, "Disable with string false")

        # Test with string "true"
        enable_result = await mcp_client.call_tool(
            "ha_set_integration_enabled", {"entry_id": entry_id, "enabled": "true"}
        )
        assert_mcp_success(enable_result, "Enable with string true")

    async def test_delete_config_entry_requires_confirm(self, mcp_client):
        """Test deletion safety check."""
        result = await mcp_client.call_tool(
            "ha_delete_config_entry", {"entry_id": "fake_id", "confirm": False}
        )
        data = assert_mcp_failure(result, "Delete without confirm")
        assert "not confirmed" in data.get("error", "").lower()

    async def test_delete_config_entry_string_confirm(self, mcp_client):
        """Test that confirm parameter accepts string booleans."""
        # Test with string "false" - should fail
        result = await mcp_client.call_tool(
            "ha_delete_config_entry", {"entry_id": "fake_id", "confirm": "false"}
        )
        data = assert_mcp_failure(result, "Delete with string false")
        assert "not confirmed" in data.get("error", "").lower()

    async def test_set_integration_enabled_nonexistent(self, mcp_client):
        """Test error handling for non-existent integration."""
        from tests.src.e2e.utilities.assertions import parse_mcp_result

        result = await mcp_client.call_tool(
            "ha_set_integration_enabled",
            {"entry_id": "nonexistent_entry_id", "enabled": True},
        )
        # Should fail - either through validation or API error
        data = parse_mcp_result(result)
        assert not data.get("success", False)
