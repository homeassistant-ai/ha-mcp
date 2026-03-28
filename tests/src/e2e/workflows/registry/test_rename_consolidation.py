"""
Edge Case Tests for Consolidated ha_rename_entity Tool

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
    """Test edge cases in consolidated ha_rename_entity tool."""

    async def test_rename_without_device_name_returns_simple_format(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Calling ha_rename_entity without new_device_name returns
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
            "ha_rename_entity",
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

        logger.info("Verified simple response format (no 'results' key)")

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )

    async def test_rename_with_device_name_returns_combined_format(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Calling ha_rename_entity with new_device_name returns the
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
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "new_device_name": "Test Device",
            },
        )

        assert rename_data.get("success"), f"Rename failed: {rename_data}"

        # Combined format SHOULD have these keys
        assert "results" in rename_data, (
            f"Combined rename should have 'results' key: {rename_data.keys()}"
        )
        assert "old_entity_id" in rename_data, (
            f"Should have old_entity_id: {rename_data.keys()}"
        )
        assert "new_entity_id" in rename_data, (
            f"Should have new_entity_id: {rename_data.keys()}"
        )
        assert rename_data["old_entity_id"] == original_entity_id
        assert rename_data["new_entity_id"] == new_entity_id

        logger.info(f"Verified combined response format: {list(rename_data.keys())}")

        # Cleanup
        await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )

    async def test_rename_with_empty_device_name_treated_as_entity_only(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Calling ha_rename_entity with new_device_name="" (empty string)
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
            "ha_rename_entity",
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
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )
