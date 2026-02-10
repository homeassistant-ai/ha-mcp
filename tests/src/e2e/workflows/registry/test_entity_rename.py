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
- Voice assistant exposure migration on rename
- Rename entity and device together (convenience wrapper)
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import parse_mcp_result, safe_call_tool

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
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
                "icon": "mdi:toggle-switch",
            },
        )

        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)
        logger.info(f"Created helper: {original_entity_id}")

        # Wait for entity to be registered (retry with backoff)
        state_data = None
        for attempt in range(10):
            await asyncio.sleep(0.5)  # Wait before checking
            state_data = await safe_call_tool(
                mcp_client,
                "ha_get_state",
                {"entity_id": original_entity_id}
            )
            if "data" in state_data and state_data["data"].get("state"):
                break
            logger.info(f"Waiting for entity to register (attempt {attempt + 1}/10)...")

        # 2. VERIFY: Original entity exists
        assert state_data and "data" in state_data and state_data["data"].get(
            "state"
        ), f"Original entity not found after waiting: {state_data}"
        logger.info(f"Verified original entity exists: {original_entity_id}")

        # 3. RENAME: Change entity_id
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
            },
        )

        assert rename_data.get("success"), f"Failed to rename entity: {rename_data}"
        assert rename_data.get("old_entity_id") == original_entity_id
        assert rename_data.get("new_entity_id") == new_entity_id
        logger.info(f"Renamed entity: {original_entity_id} -> {new_entity_id}")

        # Wait for rename to propagate (retry with backoff)
        new_state_data = None
        for attempt in range(10):
            await asyncio.sleep(0.5)
            new_state_data = await safe_call_tool(
                mcp_client,
                "ha_get_state",
                {"entity_id": new_entity_id}
            )
            if "data" in new_state_data and new_state_data["data"].get("state"):
                break
            logger.info(f"Waiting for renamed entity (attempt {attempt + 1}/10)...")

        # 4. VERIFY: New entity exists and works
        assert new_state_data and "data" in new_state_data and new_state_data["data"].get(
            "state"
        ), f"New entity not accessible after waiting: {new_state_data}"
        logger.info(f"Verified new entity exists: {new_entity_id}")

        # 5. VERIFY: Old entity_id no longer exists
        old_state_data = await safe_call_tool(
            mcp_client,
            "ha_get_state",
            {"entity_id": original_entity_id}
        )
        # Should fail or return empty/unavailable
        old_exists = (
            "data" in old_state_data
            and old_state_data["data"].get("state")
            and old_state_data["data"]["state"] != "unavailable"
        )
        assert not old_exists, f"Old entity should not exist: {old_state_data}"
        logger.info(f"Verified old entity no longer exists: {original_entity_id}")

        # 6. CLEANUP: Delete renamed entity
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": new_name,
            },
        )
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
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
                "icon": "mdi:toggle-switch",
            },
        )

        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        # Wait for entity to be registered
        await asyncio.sleep(1.0)

        # 2. RENAME: With name and icon updates
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "name": "My Renamed Toggle",
                "icon": "mdi:lightbulb",
            },
        )

        assert rename_data.get("success"), f"Failed to rename entity: {rename_data}"
        logger.info("Renamed entity with name and icon update")

        # Wait for rename to propagate
        await asyncio.sleep(0.5)

        # 3. VERIFY: New entity has updated attributes
        state_data = await safe_call_tool(
            mcp_client,
            "ha_get_state",
            {"entity_id": new_entity_id}
        )
        assert "data" in state_data, f"Failed to get new entity state: {state_data}"

        # Note: The friendly_name might be set in registry, actual display may vary
        logger.info(f"New entity state: {state_data}")

        # 4. CLEANUP
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": new_name,
            },
        )
        assert delete_data.get("success"), f"Failed to delete helper: {delete_data}"
        logger.info("Cleanup completed")

    async def test_rename_domain_mismatch_rejected(self, mcp_client):
        """
        Test: Attempting to change domain should fail

        Entity renaming cannot change the domain (e.g., light -> switch).
        """
        logger.info("Testing domain mismatch rejection")

        # Attempt to rename with domain change
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity",
            {
                "entity_id": "input_boolean.some_entity",
                "new_entity_id": "input_number.some_entity",
            },
        )

        assert not rename_data.get("success"), "Domain change should be rejected"
        # Error might be in error.message or just error string
        error_msg = rename_data.get("error", "")
        if isinstance(error_msg, dict):
            error_msg = error_msg.get("message", "")
        assert (
            "domain" in str(error_msg).lower()
        ), f"Error should mention domain: {rename_data}"
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
            rename_data = await safe_call_tool(
                mcp_client,
                "ha_rename_entity",
                {
                    "entity_id": "input_boolean.test",
                    "new_entity_id": invalid_id,
                },
            )

            assert not rename_data.get(
                "success"
            ), f"Invalid format should be rejected: {invalid_id}"
            logger.info(f"Invalid format correctly rejected: {invalid_id}")

    async def test_rename_nonexistent_entity(self, mcp_client):
        """
        Test: Renaming non-existent entity should fail gracefully
        """
        logger.info("Testing non-existent entity rename")

        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity",
            {
                "entity_id": "input_boolean.definitely_does_not_exist_12345",
                "new_entity_id": "input_boolean.new_name_12345",
            },
        )

        assert not rename_data.get("success"), "Non-existent entity rename should fail"
        logger.info(
            f"Non-existent entity correctly rejected: {rename_data.get('error')}"
        )


@pytest.mark.registry
async def test_rename_entity_basic(mcp_client, cleanup_tracker):
    """
    Quick test: Basic entity rename functionality

    Simple test that creates, renames, and cleans up a helper entity.
    """
    logger.info("Running basic entity rename test")

    # Create helper
    create_data = await safe_call_tool(
        mcp_client,
        "ha_config_set_helper",
        {
            "helper_type": "input_button",
            "name": "test_quick_rename",
            "icon": "mdi:button-pointer",
        },
    )
    assert create_data.get("success"), f"Failed to create helper: {create_data}"

    original_id = "input_button.test_quick_rename"
    new_id = "input_button.test_quick_renamed"
    cleanup_tracker.track("input_button", new_id)

    # Wait for entity to be registered
    await asyncio.sleep(1.0)

    # Rename
    rename_data = await safe_call_tool(
        mcp_client,
        "ha_rename_entity",
        {
            "entity_id": original_id,
            "new_entity_id": new_id,
        },
    )
    assert rename_data.get("success"), f"Failed to rename: {rename_data}"

    # Wait for rename to propagate
    await asyncio.sleep(0.5)

    # Cleanup
    delete_data = await safe_call_tool(
        mcp_client,
        "ha_config_remove_helper",
        {
            "helper_type": "input_button",
            "helper_id": "test_quick_renamed",
        },
    )
    assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"

    logger.info("Basic entity rename test completed")


@pytest.mark.registry
@pytest.mark.cleanup
class TestEntityRenameVoiceExposure:
    """Test voice assistant exposure migration during entity rename."""

    async def test_rename_with_voice_exposure_migration(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Rename entity with voice exposure settings migration

        1. Create entity
        2. Expose it to conversation assistant
        3. Rename entity
        4. Verify exposure is migrated to new entity_id
        """
        original_name = "test_rename_expose"
        new_name = "test_rename_expose_new"
        logger.info("Testing rename with voice exposure migration")

        # 1. CREATE: Helper entity
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
            },
        )
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        # Wait for entity to be registered
        await asyncio.sleep(1.0)

        # 2. EXPOSE: Entity to conversation assistant
        expose_data = await safe_call_tool(
            mcp_client,
            "ha_set_entity",
            {
                "entity_id": original_entity_id,
                "expose_to": {"conversation": True},
            },
        )
        assert expose_data.get("success"), f"Failed to expose entity: {expose_data}"
        logger.info(f"Exposed {original_entity_id} to conversation")


        # 3. RENAME: Entity with exposure migration (default behavior)
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
            },
        )
        assert rename_data.get("success"), f"Failed to rename entity: {rename_data}"

        # Check voice exposure migration info in response
        assert (
            "voice_exposure_migration" in rename_data
        ), "Response should include voice_exposure_migration info"
        migration_info = rename_data.get("voice_exposure_migration", {})
        logger.info(f"Voice exposure migration result: {migration_info}")

        # Wait for rename to propagate
        await asyncio.sleep(0.5)

        # 4. VERIFY: New entity has exposure settings
        check_data = await safe_call_tool(
            mcp_client,
            "ha_get_entity_exposure",
            {"entity_id": new_entity_id},
        )
        assert check_data.get("success"), f"Failed to check exposure: {check_data}"
        logger.info(f"New entity exposure: {check_data.get('exposed_to')}")

        # 5. CLEANUP
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )
        assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"
        logger.info("Cleanup completed")

    async def test_rename_without_exposure_migration(self, mcp_client, cleanup_tracker):
        """
        Test: Rename entity without migrating voice exposure settings
        """
        original_name = "test_rename_no_migrate"
        new_name = "test_rename_no_migrate_new"
        logger.info("Testing rename without voice exposure migration")

        # 1. CREATE: Helper entity
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
            },
        )
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        # Wait for entity to be registered
        await asyncio.sleep(1.0)

        # 2. RENAME: With preserve_voice_exposure=False
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "preserve_voice_exposure": False,
            },
        )
        assert rename_data.get("success"), f"Failed to rename entity: {rename_data}"

        # Should NOT have voice_exposure_migration in response
        assert (
            "voice_exposure_migration" not in rename_data
        ), "Response should not include voice_exposure_migration when disabled"
        logger.info("Renamed entity without exposure migration")


        # 3. CLEANUP
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )
        assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"


@pytest.mark.registry
@pytest.mark.cleanup
class TestRenameEntityAndDevice:
    """Test the ha_rename_entity_and_device convenience wrapper."""

    async def test_rename_entity_and_device_helper(self, mcp_client, cleanup_tracker):
        """
        Test: Rename helper entity (no device) using convenience wrapper

        Helper entities don't have associated devices, so only entity rename should occur.
        """
        original_name = "test_combo_rename"
        new_name = "test_combo_rename_new"
        logger.info("Testing ha_rename_entity_and_device with helper entity")

        # 1. CREATE: Helper entity
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
            },
        )
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        # Wait for entity to be registered
        await asyncio.sleep(1.0)

        # 2. RENAME: Using convenience wrapper
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity_and_device",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "new_device_name": "Test Device Name",  # Won't apply - no device
            },
        )

        assert rename_data.get("success"), f"Failed to rename: {rename_data}"
        assert rename_data.get("old_entity_id") == original_entity_id
        assert rename_data.get("new_entity_id") == new_entity_id

        # Check device rename was skipped (no device for helper)
        results = rename_data.get("results", {})
        device_result = results.get("device_rename", {})
        assert device_result.get(
            "skipped"
        ), "Device rename should be skipped for helper entity"
        logger.info(f"Device rename result: {device_result}")

        # Wait for rename to propagate
        await asyncio.sleep(0.5)

        # 3. VERIFY: New entity exists
        state_data = await safe_call_tool(
            mcp_client,
            "ha_get_state",
            {"entity_id": new_entity_id}
        )
        assert "data" in state_data, f"New entity not found: {state_data}"

        # 4. CLEANUP
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )
        assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"
        logger.info("Cleanup completed")

    async def test_rename_entity_without_device_name(self, mcp_client, cleanup_tracker):
        """
        Test: Rename entity without providing new_device_name

        Should rename entity and skip device rename.
        """
        original_name = "test_entity_only_rename"
        new_name = "test_entity_only_rename_new"
        logger.info("Testing ha_rename_entity_and_device without device name")

        # 1. CREATE: Helper entity
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
            },
        )
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        # Wait for entity to be registered
        await asyncio.sleep(1.0)

        # 2. RENAME: Without new_device_name
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity_and_device",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
            },
        )

        assert rename_data.get("success"), f"Failed to rename: {rename_data}"

        logger.info(f"Rename result: {rename_data.get('message')}")


        # 3. CLEANUP
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )
        assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"

    async def test_rename_entity_and_device_with_friendly_name(
        self, mcp_client, cleanup_tracker
    ):
        """
        Test: Rename entity with new friendly name using convenience wrapper
        """
        original_name = "test_friendly_rename"
        new_name = "test_friendly_rename_new"
        logger.info("Testing ha_rename_entity_and_device with friendly name")

        # 1. CREATE: Helper entity
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": original_name,
            },
        )
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        original_entity_id = f"input_boolean.{original_name}"
        new_entity_id = f"input_boolean.{new_name}"
        cleanup_tracker.track("input_boolean", new_entity_id)

        # Wait for entity to be registered
        await asyncio.sleep(1.0)

        # 2. RENAME: With new entity friendly name
        rename_data = await safe_call_tool(
            mcp_client,
            "ha_rename_entity_and_device",
            {
                "entity_id": original_entity_id,
                "new_entity_id": new_entity_id,
                "new_entity_name": "My Friendly Test Entity",
            },
        )

        assert rename_data.get("success"), f"Failed to rename: {rename_data}"

        logger.info(f"Rename result: {rename_data}")


        # 3. CLEANUP
        delete_data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": new_name},
        )
        assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"


@pytest.mark.registry
async def test_rename_entity_and_device_basic(mcp_client, cleanup_tracker):
    """
    Quick test: Basic ha_rename_entity_and_device functionality
    """
    logger.info("Running basic entity and device rename test")

    # Create helper
    create_data = await safe_call_tool(
        mcp_client,
        "ha_config_set_helper",
        {
            "helper_type": "input_button",
            "name": "test_combo_quick",
        },
    )
    assert create_data.get("success"), f"Failed to create helper: {create_data}"

    original_id = "input_button.test_combo_quick"
    new_id = "input_button.test_combo_quick_new"
    cleanup_tracker.track("input_button", new_id)

    # Wait for entity to be registered
    await asyncio.sleep(1.0)

    # Rename using convenience wrapper
    rename_data = await safe_call_tool(
        mcp_client,
        "ha_rename_entity_and_device",
        {
            "entity_id": original_id,
            "new_entity_id": new_id,
        },
    )
    assert rename_data.get("success"), f"Failed to rename: {rename_data}"

    # Wait for rename to propagate
    await asyncio.sleep(0.5)

    # Cleanup
    delete_data = await safe_call_tool(
        mcp_client,
        "ha_config_remove_helper",
        {
            "helper_type": "input_button",
            "helper_id": "test_combo_quick_new",
        },
    )
    assert delete_data.get("success"), f"Failed to cleanup: {delete_data}"

    logger.info("Basic entity and device rename test completed")
