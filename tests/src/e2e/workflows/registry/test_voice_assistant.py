"""
Voice Assistant Exposure E2E Tests

Tests for voice assistant exposure and the registry-wide settings:
- ha_set_entity(expose_to=...) - Expose/hide entities from voice assistants
- ha_get_entity() / ha_get_entity(exposed_to=...) - List exposed entities
- ha_get_entity(entity_id=...) - An entity's exposure in its registry options
- ha_set_entity(expose_new_entities=... / entity_id_parts=...) - Registry-wide

Note: These tests may have limited functionality in test environments
without Nabu Casa cloud configured. The tests focus on API functionality
rather than actual voice assistant integration.
"""

import logging

import pytest

from ...utilities.assertions import MCPAssertions, parse_mcp_result
from ...utilities.cleanup import CleanupSteps

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
        logger.info("Testing ha_get_entity without an entity")

        result = await mcp_client.call_tool("ha_get_entity", {})

        data = parse_mcp_result(result)
        assert data.get("success"), f"Failed to list exposed entities: {data}"

        # Check response structure
        assert "entity_id_parts" in data["settings"], data
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
        logger.info("Testing ha_get_entity with exposed_to")

        result = await mcp_client.call_tool(
            "ha_get_entity",
            {"exposed_to": "conversation"},
        )

        data = parse_mcp_result(result)
        assert data.get("success"), f"Failed to list exposed entities: {data}"

        assert data["assistant"] == "conversation"
        assert isinstance(data["expose_new_entities"], bool)
        assert all(v.get("conversation") for v in data["exposed_entities"].values())

        logger.info(f"Listed entities exposed to conversation: {data.get('count')}")

    async def test_list_exposed_entities_invalid_assistant(self, mcp_client):
        """
        Test: Invalid assistant name should fail
        """
        logger.info("Testing ha_get_entity with invalid assistant")

        data = await MCPAssertions(mcp_client).call_tool_failure(
            "ha_get_entity", {"exposed_to": "invalid_assistant"}
        )

        assert data["error"]["code"] == "VALIDATION_INVALID_PARAMETER", data

        logger.info("Invalid assistant correctly rejected")

    async def test_get_entity_exposure(self, mcp_client, cleanup_tracker):
        """
        Test: Get exposure settings for a specific entity
        """
        logger.info("Testing ha_get_entity exposure options")

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

        # A registered entity keeps its exposure in the entry's options
        result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": entity_id},
        )

        data = parse_mcp_result(result)
        assert data.get("success"), f"Failed to get entity: {data}"
        assert data.get("entity_id") == entity_id
        assert "options" in data["entity_entry"], data

        logger.info(f"Entity options: {data['entity_entry']['options']}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": "test_exposure_check",
                "confirm": True,
            },
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

        # Verify exposure: Core stores it in the entry's options
        check_result = await mcp_client.call_tool(
            "ha_get_entity",
            {"entity_id": entity_id},
        )
        check_data = parse_mcp_result(check_result)
        assert check_data.get("success"), f"Failed to check exposure: {check_data}"
        options = check_data["entity_entry"]["options"]
        assert options["conversation"]["should_expose"] is True, options

        # Cleanup
        await mcp_client.call_tool(
            "ha_remove_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": "test_expose_entity",
                "confirm": True,
            },
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
            "ha_remove_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": "test_hide_entity",
                "confirm": True,
            },
        )

    async def test_expose_entity_invalid_assistant(self, mcp_client):
        """
        Test: Invalid assistant name in expose_to should fail
        """
        logger.info("Testing ha_set_entity expose_to with invalid assistant")

        data = await MCPAssertions(mcp_client).call_tool_failure(
            "ha_set_entity",
            {
                "entity_id": "input_boolean.test",
                "expose_to": {"invalid_assistant": True},
            },
        )

        assert data["error"]["code"] == "VALIDATION_INVALID_PARAMETER", data

        logger.info("Invalid assistant correctly rejected")


@pytest.mark.registry
async def test_voice_exposure_basic(mcp_client):
    """
    Quick test: Basic voice exposure listing functionality
    """
    logger.info("Running basic voice exposure test")

    result = await mcp_client.call_tool("ha_get_entity", {})
    data = parse_mcp_result(result)

    assert data.get("success"), f"Failed: {data}"
    assert "exposed_entities" in data
    assert "summary" in data

    logger.info(
        f"Voice exposure test completed: {data.get('count')} entities with settings"
    )


@pytest.mark.registry
async def test_expose_new_entities_round_trip(mcp_client):
    """ha_set_entity writes an assistant's "expose new entities" flag."""
    read = parse_mcp_result(
        await mcp_client.call_tool("ha_get_entity", {"exposed_to": "conversation"})
    )
    before = read["expose_new_entities"]
    try:
        written = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_set_entity", {"expose_new_entities": {"conversation": not before}}
            )
        )
        assert written["previous_expose_new_entities"] == {"conversation": before}
        after = parse_mcp_result(
            await mcp_client.call_tool("ha_get_entity", {"exposed_to": "conversation"})
        )
        assert after["expose_new_entities"] is (not before)
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            await MCPAssertions(mcp_client).call_tool_success(
                "ha_set_entity", {"expose_new_entities": {"conversation": before}}
            )


@pytest.mark.registry
async def test_entity_id_parts_round_trip(mcp_client):
    """ha_set_entity writes the registry's entity_id_parts; Core judges the list."""
    before = parse_mcp_result(await mcp_client.call_tool("ha_get_entity", {}))[
        "settings"
    ]["entity_id_parts"]
    try:
        written = parse_mcp_result(
            await mcp_client.call_tool(
                "ha_set_entity", {"entity_id_parts": ["device", "entity"]}
            )
        )
        assert written["settings"] == {"entity_id_parts": ["device", "entity"]}
        assert written["previous_settings"] == {"entity_id_parts": before}

        refused = await MCPAssertions(mcp_client).call_tool_failure(
            "ha_set_entity", {"entity_id_parts": ["entity"]}
        )
        assert refused["error"]["code"] == "VALIDATION_INVALID_PARAMETER", refused
    finally:
        with CleanupSteps() as cleanup, cleanup.step():
            await MCPAssertions(mcp_client).call_tool_success(
                "ha_set_entity", {"entity_id_parts": before or []}
            )
