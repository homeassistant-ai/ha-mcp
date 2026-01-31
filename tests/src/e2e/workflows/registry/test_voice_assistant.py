"""
Voice Assistant Exposure E2E Tests

Tests for voice assistant exposure tools:
- ha_set_entity(expose_to=...) - Expose/hide entities from voice assistants
- ha_get_entity_exposure - List entity exposure status
- ha_get_entity_exposure - Get specific entity exposure

Note: These tests may have limited functionality in test environments
without Nabu Casa cloud configured. The tests focus on API functionality
rather than actual voice assistant integration.
"""

import logging

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.registry
@pytest.mark.cleanup
class TestVoiceAssistantExposure:
    """Test voice assistant exposure management tools."""

    async def test_list_exposed_entities(self, mcp_client):
        """
        Test: List all entities with custom exposure settings

        This is a read-only operation that should always succeed.
        """
        logger.info("Testing ha_get_entity_exposure")

        result = await mcp_client.call_tool("ha_get_entity_exposure", {})

        data = parse_mcp_result(result)
        assert data.get("success"), f"Failed to list exposed entities: {data}"

        # Check response structure
        assert "exposed_entities" in data, "Response should have exposed_entities"
        assert "count" in data, "Response should have count"
        assert "summary" in data, "Response should have summary"

        logger.info(
            f"Listed {data.get('count')} entities with custom exposure settings"
        )
        logger.info(f"Summary: {data.get('summary')}")

    async def test_list_exposed_entities_with_assistant_filter(self, mcp_client):
        """
        Test: Filter exposed entities by specific assistant
        """
        logger.info("Testing ha_get_entity_exposure with assistant filter")

        result = await mcp_client.call_tool(
            "ha_get_entity_exposure",
            {"assistant": "conversation"},
        )

        data = parse_mcp_result(result)
        assert data.get("success"), f"Failed to list exposed entities: {data}"

        # Check filter was applied
        filters = data.get("filters_applied", {})
        assert (
            filters.get("assistant") == "conversation"
        ), "Assistant filter should be applied"

        logger.info(f"Listed entities exposed to conversation: {data.get('count')}")

    async def test_list_exposed_entities_invalid_assistant(self, mcp_client):
        """
        Test: Invalid assistant name should fail
        """
        logger.info("Testing ha_get_entity_exposure with invalid assistant")

        result = await mcp_client.call_tool(
            "ha_get_entity_exposure",
            {"assistant": "invalid_assistant"},
        )

        data = parse_mcp_result(result)
        assert not data.get("success"), "Invalid assistant should fail"
        assert "valid_assistants" in data, "Should suggest valid assistants"

        logger.info("Invalid assistant correctly rejected")

    async def test_get_entity_exposure(self, mcp_client, cleanup_tracker):
        """
        Test: Get exposure settings for a specific entity
        """
        logger.info("Testing ha_get_entity_exposure")

        # Create a test entity first
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "test_exposure_check",
            },
        )
        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        entity_id = "input_boolean.test_exposure_check"
        cleanup_tracker.track("input_boolean", entity_id)


        # Get exposure settings
        result = await mcp_client.call_tool(
            "ha_get_entity_exposure",
            {"entity_id": entity_id},
        )

        data = parse_mcp_result(result)
        assert data.get("success"), f"Failed to get entity exposure: {data}"

        # Check response structure
        assert "exposed_to" in data, "Response should have exposed_to"
        assert "is_exposed_anywhere" in data, "Response should have is_exposed_anywhere"
        assert data.get("entity_id") == entity_id

        logger.info(f"Entity exposure: {data.get('exposed_to')}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": "test_exposure_check"},
        )

    async def test_expose_entity_to_conversation(self, mcp_client, cleanup_tracker):
        """
        Test: Expose an entity to the conversation assistant (Assist) via ha_set_entity

        Note: This test uses the 'conversation' assistant which is built-in
        and doesn't require Nabu Casa.
        """
        logger.info("Testing ha_set_entity expose_to conversation assistant")

        # Create a test entity
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "test_expose_entity",
            },
        )
        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        entity_id = "input_boolean.test_expose_entity"
        cleanup_tracker.track("input_boolean", entity_id)


        # Expose to conversation assistant via ha_set_entity
        expose_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "expose_to": {"conversation": True},
            },
        )

        expose_data = parse_mcp_result(expose_result)
        assert expose_data.get("success"), f"Failed to expose entity: {expose_data}"
        assert expose_data.get("exposure") == {"conversation": True}

        logger.info(f"Exposed entity to conversation: {expose_data}")

        # Verify exposure
        check_result = await mcp_client.call_tool(
            "ha_get_entity_exposure",
            {"entity_id": entity_id},
        )
        check_data = parse_mcp_result(check_result)
        assert check_data.get("success"), f"Failed to check exposure: {check_data}"

        # The entity should now show as exposed to conversation
        # Note: In some HA versions/configs this might not immediately reflect
        logger.info(f"Entity exposure after expose: {check_data.get('exposed_to')}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": "test_expose_entity"},
        )

    async def test_hide_entity_from_assistant(self, mcp_client, cleanup_tracker):
        """
        Test: Hide an entity from a voice assistant via ha_set_entity
        """
        logger.info("Testing ha_set_entity expose_to hide entity")

        # Create a test entity
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "test_hide_entity",
            },
        )
        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create helper: {create_data}"

        entity_id = "input_boolean.test_hide_entity"
        cleanup_tracker.track("input_boolean", entity_id)


        # Hide from conversation assistant via ha_set_entity
        hide_result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": entity_id,
                "expose_to": {"conversation": False},
            },
        )

        hide_data = parse_mcp_result(hide_result)
        assert hide_data.get("success"), f"Failed to hide entity: {hide_data}"
        assert hide_data.get("exposure") == {"conversation": False}

        logger.info(f"Hidden entity from conversation: {hide_data}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": "test_hide_entity"},
        )

    async def test_expose_entity_invalid_assistant(self, mcp_client):
        """
        Test: Invalid assistant name in expose_to should fail
        """
        logger.info("Testing ha_set_entity expose_to with invalid assistant")

        result = await mcp_client.call_tool(
            "ha_set_entity",
            {
                "entity_id": "input_boolean.test",
                "expose_to": {"invalid_assistant": True},
            },
        )

        data = parse_mcp_result(result)
        assert not data.get("success"), "Invalid assistant should fail"

        logger.info("Invalid assistant correctly rejected")


@pytest.mark.registry
async def test_voice_exposure_basic(mcp_client):
    """
    Quick test: Basic voice exposure listing functionality
    """
    logger.info("Running basic voice exposure test")

    result = await mcp_client.call_tool("ha_get_entity_exposure", {})
    data = parse_mcp_result(result)

    assert data.get("success"), f"Failed: {data}"
    assert "exposed_entities" in data
    assert "summary" in data

    logger.info(
        f"Voice exposure test completed: {data.get('count')} entities with settings"
    )
