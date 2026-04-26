"""
E2E tests for integration management tools.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import (
    assert_mcp_success,
    safe_call_tool,
)
from tests.src.e2e.utilities.wait_helpers import wait_for_tool_result

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
        assert entry["disabled_by"] is None, (
            "Integration should not be disabled after re-enable"
        )

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
        data = await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": "fake_id", "confirm": False},
        )
        assert not data.get("success"), "Delete without confirm should fail"
        error = data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "not confirmed" in error_msg.lower()

    async def test_delete_config_entry_string_confirm(self, mcp_client):
        """Test that confirm parameter accepts string booleans."""
        # Test with string "false" - should fail
        data = await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": "fake_id", "confirm": "false"},
        )
        assert not data.get("success"), "Delete with string false should fail"
        error = data.get("error", {})
        error_msg = (
            error.get("message", str(error)) if isinstance(error, dict) else str(error)
        )
        assert "not confirmed" in error_msg.lower()

    async def test_delete_config_entry_create_delete_cycle(self, mcp_client):
        """Test full create → verify → delete → verify-gone cycle.

        Regression test: the config-entry delete path previously used the WebSocket
        command ``config_entries/delete`` which HA does not support, returning
        "Unknown command".  The fix switches to the REST API endpoint.
        """
        # Create a temporary light group helper
        config = {
            "group_type": "light",
            "name": "test_delete_regression_e2e",
            "entities": [],
            "hide_members": False,
        }

        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "group",
                "name": "test_delete_regression_e2e",
                "config": config,
            },
        )
        data = assert_mcp_success(create_result, "Create light group for delete test")
        entry_id = data["entry_id"]
        logger.info(f"Created temporary group helper: {entry_id}")

        # Wait until the entry is registered
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_get_integration",
            arguments={"entry_id": entry_id},
            predicate=lambda d: d.get("success") is True,
            description="group helper is registered",
        )

        # Delete the entry
        delete_result = await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"target": entry_id, "confirm": True},
        )
        delete_data = assert_mcp_success(delete_result, "Delete config entry")
        assert delete_data.get("success") is True
        assert delete_data.get("entry_id") == entry_id
        logger.info(f"Deleted config entry: {entry_id}")

        # Verify the entry is gone
        verify_data = await safe_call_tool(
            mcp_client,
            "ha_get_integration",
            {"entry_id": entry_id},
        )
        assert not verify_data.get("success", False), (
            f"Config entry {entry_id} should not exist after deletion"
        )

    async def test_set_integration_enabled_nonexistent(self, mcp_client):
        """Test error handling for non-existent integration."""
        data = await safe_call_tool(
            mcp_client,
            "ha_set_integration_enabled",
            {"entry_id": "nonexistent_entry_id", "enabled": True},
        )
        # Should fail - either through validation or API error
        assert not data.get("success", False)

    async def test_delete_config_entry_nonexistent_confirmed(self, mcp_client):
        """
        Test: ha_delete_helpers_integrations with a nonexistent entry_id and
        confirm=True returns a structured error, not success=True.

        Source path: confirm_bool=True bypasses the guard; delete_config_entry()
        reaches the HA REST API with an unknown entry_id → Exception →
        exception_to_structured_error(raise_error=True) → ToolError.

        Existing tests cover confirm=False (guard path) and a valid entry_id
        (CRUD cycle). This test covers the third structurally distinct path:
        confirmed deletion of an entry that does not exist.
        """
        data = await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"target": "nonexistent_entry_a7_e2e_xyz", "confirm": True},
        )
        assert not data.get("success", False), (
            f"Expected failure for nonexistent entry_id with confirm=True, "
            f"got success=True: {data}"
        )
        # Verify a structured error is returned (not a silent pass-through)
        assert data.get("error") is not None, (
            f"Expected error details in failure response, got: {data}"
        )
