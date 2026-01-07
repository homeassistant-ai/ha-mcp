"""
E2E tests for entity management tools.
"""

import logging

import pytest

from tests.src.e2e.utilities.assertions import assert_mcp_success
from tests.src.e2e.utilities.cleanup import TestEntityCleaner

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.entity
class TestEntityManagement:
    """Test entity enable/disable operations."""

    async def test_set_entity_enabled_cycle(self, mcp_client, cleanup_tracker):
        """Test entity enable/disable cycle."""
        cleaner = TestEntityCleaner(mcp_client)

        # Create test helper for entity
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Entity Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        logger.info(f"Created test entity: {entity_id}")

        # DISABLE entity
        disable_result = await mcp_client.call_tool(
            "ha_set_entity_enabled", {"entity_id": entity_id, "enabled": False}
        )
        data = assert_mcp_success(disable_result, "Disable entity")
        assert not data.get("enabled"), "Entity should be disabled"

        # RE-ENABLE entity
        enable_result = await mcp_client.call_tool(
            "ha_set_entity_enabled", {"entity_id": entity_id, "enabled": True}
        )
        data = assert_mcp_success(enable_result, "Re-enable entity")
        assert data.get("enabled"), "Entity should be enabled"

        # Cleanup
        await cleaner.cleanup_all()

    async def test_set_entity_enabled_string_bool(self, mcp_client, cleanup_tracker):
        """Test that enabled parameter accepts string booleans."""
        cleaner = TestEntityCleaner(mcp_client)

        # Create test helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E String Bool Test",
                "icon": "mdi:test-tube",
            },
        )
        data = assert_mcp_success(create_result, "Create test entity")
        entity_id = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
        cleaner.track_entity("input_boolean", entity_id)

        # Test with string "false"
        disable_result = await mcp_client.call_tool(
            "ha_set_entity_enabled", {"entity_id": entity_id, "enabled": "false"}
        )
        assert_mcp_success(disable_result, "Disable with string false")

        # Test with string "true"
        enable_result = await mcp_client.call_tool(
            "ha_set_entity_enabled", {"entity_id": entity_id, "enabled": "true"}
        )
        assert_mcp_success(enable_result, "Enable with string true")

        # Cleanup
        await cleaner.cleanup_all()

    async def test_set_entity_enabled_nonexistent(self, mcp_client):
        """Test error handling for non-existent entity."""
        result = await mcp_client.call_tool(
            "ha_set_entity_enabled",
            {"entity_id": "sensor.nonexistent_entity", "enabled": True},
        )
        # Should fail - either through validation or API error
        assert not result.get("success", False)
