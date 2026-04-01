"""
Regression tests for group config tool post-operation verification.

Verifies that group tools confirm entity state after service calls,
preventing the false-success anti-pattern where the tool returns
success before the entity is actually queryable.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.group
class TestGroupVerification:
    """Verify that group operations confirm entity state."""

    async def test_created_group_is_immediately_queryable(
        self, mcp_client, cleanup_tracker
    ):
        """After ha_config_set_group succeeds, the entity must be queryable.

        Regression test: before verification was added, the tool returned
        success with a predicted entity_id that might not exist yet.
        """
        object_id = "test_e2e_verify_create"

        result = await mcp_client.call_tool(
            "ha_config_set_group",
            {
                "object_id": object_id,
                "name": "Verification Test Group",
                "entities": ["light.bed_light"],
            },
        )

        data = assert_mcp_success(result, "Create group")
        entity_id = data.get("entity_id")
        assert entity_id == f"group.{object_id}"
        cleanup_tracker.track("group", object_id)

        # The entity must be queryable immediately after the tool returns
        state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": entity_id}
        )
        state_data = assert_mcp_success(state_result, "Get group state after create")
        assert state_data.get("entity_id") == entity_id, (
            f"Created group not queryable immediately after tool returned success: {state_data}"
        )
        logger.info(f"Group {entity_id} confirmed queryable after create")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_group", {"object_id": object_id}
        )

    async def test_removed_group_is_immediately_gone(
        self, mcp_client, cleanup_tracker
    ):
        """After ha_config_remove_group succeeds, the entity must be gone.

        Regression test: before verification was added, the tool returned
        success but the entity could still be queryable briefly.
        """
        object_id = "test_e2e_verify_remove"

        # Create a group first
        result = await mcp_client.call_tool(
            "ha_config_set_group",
            {
                "object_id": object_id,
                "name": "Removal Verification Group",
                "entities": ["light.bed_light"],
            },
        )
        assert_mcp_success(result, "Create group for removal test")
        cleanup_tracker.track("group", object_id)

        # Remove it
        remove_result = await mcp_client.call_tool(
            "ha_config_remove_group", {"object_id": object_id}
        )
        assert_mcp_success(remove_result, "Remove group")

        # The entity must NOT be queryable after the tool returns
        entity_id = f"group.{object_id}"
        state_data = await safe_call_tool(
            mcp_client, "ha_get_state", {"entity_id": entity_id}
        )
        # Should either fail or return not_found
        is_gone = (
            not state_data.get("success")
            or state_data.get("state") == "unavailable"
            or "not found" in str(state_data).lower()
        )
        assert is_gone, (
            f"Removed group still queryable after tool returned success: {state_data}"
        )
        logger.info(f"Group {entity_id} confirmed gone after remove")
