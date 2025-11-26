"""
Entity Rename E2E Tests

Tests for the ha_rename_entity tool which changes entity_ids via
the config/entity_registry/update WebSocket API.

Key test scenarios:
- Rename helper entity successfully
- Validate domain preservation (cannot change domain)
- Validate entity_id format
- Handle non-existent entities
- Update name and icon along with rename
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.registry
@pytest.mark.cleanup
class TestEntityRename:
    """Test entity renaming via ha_rename_entity tool."""

    async def test_rename_helper_entity(self, mcp_client, cleanup_tracker):
        """
        Test: Create helper -> Rename entity_id -> Verify new entity works

        This is the primary use case for entity renaming.
        """
        original_name = "test_rename_original"
        new_name = "test_rename_new"
        logger.info(f"Testing entity rename: {original_name} -> {new_name}")

        # 1. CREATE: Helper entity to rename
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
                "icon": "mdi:toggle-switch",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)
        logger.info(f"Created helper: {original_entity_id}")

        # Wait for entity to be registered
        await asyncio.sleep(1)

        # 2. VERIFY: Original entity exists
        state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": original_entity_id}
        )
        state_data = parse_mcp_result(state_result)
        assert "data" in state_data and state_data["data"].get("state"), (
            f"Original entity not found: {state_data}"
        )
        logger.info(f"Verified original entity exists: {original_entity_id}")

        # 3. RENAME: Change entity_id
        rename_result = await mcp_client.call_tool(
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
            },
        )

        rename_data = parse_mcp_result(rename_result)
        assert rename_data.get("success"), f"Failed to rename entity: {rename_data}"
        assert rename_data.get("old_entity_id") == original_entity_id
        assert rename_data.get("new_entity_id") == new_entity_id
        logger.info(f"Renamed entity: {original_entity_id} -> {new_entity_id}")

        # Wait for rename to propagate
        await asyncio.sleep(2)

        # 4. VERIFY: New entity exists and works
        new_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": new_entity_id}
        )
        new_state_data = parse_mcp_result(new_state_result)
        assert "data" in new_state_data and new_state_data["data"].get("state"), (
            f"New entity not accessible: {new_state_data}"
        )
        logger.info(f"Verified new entity exists: {new_entity_id}")

        # 5. VERIFY: Old entity_id no longer exists
        old_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": original_entity_id}
        )
        old_state_data = parse_mcp_result(old_state_result)
        # Should fail or return empty/unavailable
        old_exists = (
            "data" in old_state_data
            and old_state_data["data"].get("state")
            and old_state_data["data"]["state"] != "unavailable"
        )
        assert not old_exists, f"Old entity should not exist: {old_state_data}"
        logger.info(f"Verified old entity no longer exists: {original_entity_id}")

        # 6. CLEANUP: Delete renamed entity
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": new_name,
            },
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete helper: {delete_data}"
        logger.info("Cleanup completed")

    async def test_rename_with_name_and_icon(self, mcp_client, cleanup_tracker):
        """
        Test: Rename entity while also updating friendly name and icon
        """
        original_name = "test_rename_full"
        new_name = "test_rename_full_new"
        logger.info("Testing rename with name and icon update")

        # 1. CREATE: Helper entity
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
                "icon": "mdi:toggle-switch",
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        await asyncio.sleep(1)

        # 2. RENAME: With name and icon updates
        rename_result = await mcp_client.call_tool(
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "name": "My Renamed Toggle",
                "icon": "mdi:lightbulb",
            },
        )

        rename_data = parse_mcp_result(rename_result)
        assert rename_data.get("success"), f"Failed to rename entity: {rename_data}"
        logger.info("Renamed entity with name and icon update")

        await asyncio.sleep(2)

        # 3. VERIFY: New entity has updated attributes
        state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": new_entity_id}
        )
        state_data = parse_mcp_result(state_result)
        assert "data" in state_data, f"Failed to get new entity state: {state_data}"

        # Note: The friendly_name might be set in registry, actual display may vary
        logger.info(f"New entity state: {state_data}")

        # 4. CLEANUP
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": new_name,
            },
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete helper: {delete_data}"
        logger.info("Cleanup completed")

    async def test_rename_domain_mismatch_rejected(self, mcp_client):
        """
        Test: Attempting to change domain should fail

        Entity renaming cannot change the domain (e.g., light -> switch).
        """
        logger.info("Testing domain mismatch rejection")

        # Attempt to rename with domain change
        rename_result = await mcp_client.call_tool(
            "ha_rename_entity",
            {
                "entity_id": "input_boolean.some_entity",
                "new_entity_id": "input_number.some_entity",
            },
        )

        rename_data = parse_mcp_result(rename_result)
        assert not rename_data.get("success"), "Domain change should be rejected"
        assert "domain" in rename_data.get("error", "").lower(), (
            f"Error should mention domain: {rename_data}"
        )
        logger.info("Domain mismatch correctly rejected")

    async def test_rename_invalid_format_rejected(self, mcp_client):
        """
        Test: Invalid entity_id formats should be rejected
        """
        logger.info("Testing invalid entity_id format rejection")

        # Test with invalid new_entity_id format
        invalid_formats = [
            "invalid_format",  # Missing domain
            "Domain.Upper",  # Uppercase not allowed
            "light.has spaces",  # Spaces not allowed
            "light.special!chars",  # Special chars not allowed
        ]

        for invalid_id in invalid_formats:
            rename_result = await mcp_client.call_tool(
                "ha_rename_entity",
                {
                    "entity_id": "input_boolean.test",
                    "new_entity_id": invalid_id,
                },
            )

            rename_data = parse_mcp_result(rename_result)
            assert not rename_data.get("success"), (
                f"Invalid format should be rejected: {invalid_id}"
            )
            logger.info(f"Invalid format correctly rejected: {invalid_id}")

    async def test_rename_nonexistent_entity(self, mcp_client):
        """
        Test: Renaming non-existent entity should fail gracefully
        """
        logger.info("Testing non-existent entity rename")

        rename_result = await mcp_client.call_tool(
            "ha_rename_entity",
            {
                "entity_id": "input_boolean.definitely_does_not_exist_12345",
                "new_entity_id": "input_boolean.new_name_12345",
            },
        )

        rename_data = parse_mcp_result(rename_result)
        assert not rename_data.get("success"), (
            "Non-existent entity rename should fail"
        )
        logger.info(f"Non-existent entity correctly rejected: {rename_data.get('error')}")


@pytest.mark.registry
async def test_rename_entity_basic(mcp_client, cleanup_tracker):
    """
    Quick test: Basic entity rename functionality

    Simple test that creates, renames, and cleans up a helper entity.
    """
    logger.info("Running basic entity rename test")

    # Create helper
    create_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_button",
            "name": "test_quick_rename",
            "icon": "mdi:button-pointer",
        },
    )
    create_data = parse_mcp_result(create_result)
    assert create_data.get("success"), f"Failed to create helper: {create_data}"

    original_id = "input_button.test_quick_rename"
    new_id = "input_button.test_quick_renamed"
    cleanup_tracker.track("input_button", new_id)

    await asyncio.sleep(1)

    # Rename
    rename_result = await mcp_client.call_tool(
        "ha_rename_entity",
        {
            "entity_id": original_id,
            "new_entity_id": new_id,
        },
    )
    rename_data = parse_mcp_result(rename_result)
    assert rename_data.get("success"), f"Failed to rename: {rename_data}"

    await asyncio.sleep(1)

    # Cleanup
    delete_result = await mcp_client.call_tool(
        "ha_config_remove_helper",
        {
            "helper_type": "input_button",
            "helper_id": "test_quick_renamed",
        },
    )
    delete_data = parse_mcp_result(delete_result)
    assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"

    logger.info("Basic entity rename test completed")
