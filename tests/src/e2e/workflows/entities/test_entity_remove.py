"""E2E tests for ha_remove_entity tool."""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.registry
class TestEntityRemove:
    """Test ha_remove_entity tool."""

    async def test_remove_entity_success(self, mcp_client):
        """Happy path: create a helper entity, remove it, verify it is gone."""
        # Create a temporary input_boolean to remove
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Remove Entity Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test helper")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        logger.info(f"Created test entity: {entity_id}")

        # Remove the entity
        remove_result = await mcp_client.call_tool(
            "ha_remove_entity",
            {"entity_id": entity_id},
        )
        remove_data = assert_mcp_success(remove_result, "Remove entity")
        assert remove_data.get("success") is True, f"Expected success, got: {remove_data}"
        assert remove_data.get("entity_id") == entity_id

        logger.info(f"Entity removed successfully: {entity_id}")

        # Verify entity is gone — second removal should fail
        verify_data = await safe_call_tool(
            mcp_client,
            "ha_remove_entity",
            {"entity_id": entity_id},
        )
        assert not verify_data.get("success"), (
            f"Entity should be gone after removal, got: {verify_data}"
        )
        logger.info("Entity removal verified — entity no longer exists")

    async def test_remove_entity_nonexistent(self, mcp_client):
        """Removing a non-existent entity should fail gracefully."""
        data = await safe_call_tool(
            mcp_client,
            "ha_remove_entity",
            {"entity_id": "sensor.definitely_not_real_12345"},
        )
        assert not data.get("success"), (
            f"Expected failure for non-existent entity, got: {data}"
        )
        logger.info("Non-existent entity removal error handling verified")
