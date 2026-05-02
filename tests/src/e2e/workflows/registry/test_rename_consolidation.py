"""
Edge Case Tests for Consolidated ha_set_entity Tool

Tests the new_device_name parameter behavior and response format
differences between entity-only and entity+device rename paths.
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.registry
@pytest.mark.cleanup
class TestRenameConsolidationEdgeCases:
    """Test edge cases in consolidated ha_set_entity tool."""

    async def test_rename_without_device_name_returns_simple_format(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Calling ha_set_entity without new_device_name returns
        the simple entity-rename response (no 'results' key).
        """
        original_name = "test_simple_format"
        new_name = "test_simple_format_new"
        logger.info("Testing entity-only rename returns simple response format")

        # Create helper
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "input_boolean", "name": original_name},
        )
        assert create_data.get("success"), f"Failed to create: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        await asyncio.sleep(1.0)

        # Rename without new_device_name
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_set_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
            },
        )

        assert rename_data.get("success"), f"Rename failed: {rename_data}"

        # Simple format should NOT have 'results' key
        assert "results" not in rename_data, (
            f"Simple rename should not have 'results' key: {rename_data.keys()}"
        )

        # Rename should include a warning about updating references
        assert "warning" in rename_data, (
            f"Rename should include a warning about updating references: {rename_data.keys()}"
        )

        logger.info("Verified simple response format (no 'results' key, has warning)")

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"helper_type": "input_boolean", "target": new_name, "confirm": True},
        )

    async def test_rename_with_device_name_returns_combined_format(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Calling ha_set_entity with new_device_name returns the
        combined response format (with 'results' key, old/new entity IDs).
        """
        original_name = "test_combined_format"
        new_name = "test_combined_format_new"
        logger.info("Testing entity+device rename returns combined response format")

        # Create helper (no device, but response format should still be combined)
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "input_boolean", "name": original_name},
        )
        assert create_data.get("success"), f"Failed to create: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        await asyncio.sleep(1.0)

        # Rename WITH new_device_name
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_set_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "new_device_name": "Test Device",
            },
        )

        assert rename_data.get("success"), f"Rename failed: {rename_data}"

        # Consolidated ha_set_entity response format
        assert "old_entity_id" in rename_data, (
            f"Should have old_entity_id: {rename_data.keys()}"
        )
        assert "entity_id" in rename_data, (
            f"Should have entity_id: {rename_data.keys()}"
        )
        assert "device_rename" in rename_data, (
            f"Should have device_rename: {rename_data.keys()}"
        )
        assert rename_data["old_entity_id"] == original_entity_id
        assert rename_data["entity_id"] == new_entity_id

        logger.info(f"Verified combined response format: {list(rename_data.keys())}")

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"helper_type": "input_boolean", "target": new_name, "confirm": True},
        )

    async def test_rename_with_empty_device_name_treated_as_entity_only(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Calling ha_set_entity with new_device_name="" (empty string)
        should be treated as entity-only rename (empty string normalized to None).
        """
        original_name = "test_empty_devname"
        new_name = "test_empty_devname_new"
        logger.info("Testing rename with empty string device name")

        # Create helper
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "input_boolean", "name": original_name},
        )
        assert create_data.get("success"), f"Failed to create: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        await asyncio.sleep(1.0)

        # Rename with empty new_device_name — should be treated as None
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_set_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "new_device_name": "",
            },
        )

        assert rename_data.get("success"), f"Rename failed: {rename_data}"

        # Empty string is normalized to None, so should get simple format
        assert "results" not in rename_data, (
            f"Empty device name should produce simple format (no 'results'): {rename_data.keys()}"
        )

        logger.info("Empty device name correctly treated as entity-only rename")

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_delete_helpers_integrations",
            {"helper_type": "input_boolean", "target": new_name, "confirm": True},
        )
