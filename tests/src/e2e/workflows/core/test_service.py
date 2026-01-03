"""
E2E tests for ha_call_service tool - service execution.

Tests the fundamental service call functionality that controls all
Home Assistant entities and executes automations.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.core
class TestCallService:
    """Test ha_call_service tool functionality."""

    async def test_call_service_light_turn_on(self, mcp_client, test_light_entity):
        """Test calling light.turn_on service."""
        logger.info(f"Testing ha_call_service light.turn_on on {test_light_entity}")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": test_light_entity,
            },
        )

        data = assert_mcp_success(result, "Light turn_on service call")

        # Verify response structure
        assert data.get("domain") == "light", f"Domain mismatch: {data}"
        assert data.get("service") == "turn_on", f"Service mismatch: {data}"
        assert test_light_entity in str(data.get("entity_id")), (
            f"Entity ID mismatch: {data}"
        )

        logger.info(f"Light turn_on executed successfully: {data.get('message')}")

        # Verify state changed
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            current_state = state_data.get("data", {}).get("state")
            logger.info(f"Light state after turn_on: {current_state}")
            assert current_state == "on", f"Light should be on: {current_state}"

    async def test_call_service_light_turn_off(self, mcp_client, test_light_entity):
        """Test calling light.turn_off service."""
        logger.info(f"Testing ha_call_service light.turn_off on {test_light_entity}")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_off",
                "entity_id": test_light_entity,
            },
        )

        data = assert_mcp_success(result, "Light turn_off service call")

        # Verify response structure
        assert data.get("domain") == "light", f"Domain mismatch: {data}"
        assert data.get("service") == "turn_off", f"Service mismatch: {data}"

        logger.info(f"Light turn_off executed successfully: {data.get('message')}")

        # Verify state changed
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            current_state = state_data.get("data", {}).get("state")
            logger.info(f"Light state after turn_off: {current_state}")
            assert current_state == "off", f"Light should be off: {current_state}"

    async def test_call_service_light_toggle(self, mcp_client, test_light_entity):
        """Test calling light.toggle service."""
        logger.info(f"Testing ha_call_service light.toggle on {test_light_entity}")

        # Get initial state
        initial_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        initial_data = parse_mcp_result(initial_result)
        initial_state = initial_data.get("data", {}).get("state", "unknown")
        logger.info(f"Initial light state: {initial_state}")

        # Toggle the light
        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "toggle",
                "entity_id": test_light_entity,
            },
        )

        data = assert_mcp_success(result, "Light toggle service call")
        logger.info(f"Light toggle executed successfully: {data.get('message')}")

        # Verify state changed
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            new_state = state_data.get("data", {}).get("state")
            logger.info(f"Light state after toggle: {new_state}")
            # State should be opposite of initial
            if initial_state == "on":
                assert new_state == "off", f"Toggle should turn off: {new_state}"
            elif initial_state == "off":
                assert new_state == "on", f"Toggle should turn on: {new_state}"

    async def test_call_service_with_data_brightness(
        self, mcp_client, test_light_entity
    ):
        """Test calling light.turn_on with brightness data."""
        logger.info(f"Testing ha_call_service with brightness on {test_light_entity}")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": test_light_entity,
                "data": {"brightness_pct": 50},
            },
        )

        data = assert_mcp_success(result, "Light turn_on with brightness")

        logger.info(f"Light turn_on with brightness executed: {data.get('message')}")

        # Verify brightness was applied
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            attrs = state_data.get("data", {}).get("attributes", {})
            if "brightness" in attrs:
                # brightness_pct 50 = brightness ~128
                brightness = attrs.get("brightness", 0)
                logger.info(f"Light brightness after set: {brightness}")
                assert 100 <= brightness <= 155, (
                    f"Brightness should be around 128: {brightness}"
                )

    async def test_call_service_homeassistant_toggle(
        self, mcp_client, test_light_entity
    ):
        """Test calling homeassistant.toggle universal service."""
        logger.info(f"Testing ha_call_service homeassistant.toggle on {test_light_entity}")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "homeassistant",
                "service": "toggle",
                "entity_id": test_light_entity,
            },
        )

        data = assert_mcp_success(result, "Universal toggle service call")

        assert data.get("domain") == "homeassistant", f"Domain mismatch: {data}"
        assert data.get("service") == "toggle", f"Service mismatch: {data}"

        logger.info(f"Universal toggle executed successfully: {data.get('message')}")

    async def test_call_service_automation_trigger(self, mcp_client, cleanup_tracker, test_data_factory):
        """Test triggering an automation via service call."""
        # Create a test automation first
        test_light = "light.bed_light"
        automation_name = "Service Trigger Test"
        config = test_data_factory.automation_config(
            automation_name,
            trigger=[{"platform": "time", "at": "23:59:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
            initial_state=True,
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {"config": config},
        )
        create_data = assert_mcp_success(create_result, "Create test automation")
        automation_entity = create_data.get("entity_id")
        if not automation_entity:
            automation_entity = f"automation.{automation_name.lower().replace(' ', '_')}_e2e"
        cleanup_tracker.track("automation", automation_entity)

        logger.info(f"Testing automation.trigger on {automation_entity}")


        # Trigger the automation
        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_entity,
            },
        )

        data = assert_mcp_success(result, "Automation trigger service call")

        assert data.get("domain") == "automation", f"Domain mismatch: {data}"
        assert data.get("service") == "trigger", f"Service mismatch: {data}"

        logger.info(f"Automation trigger executed: {data.get('message')}")

        # Cleanup
        await mcp_client.call_tool(
            "ha_config_remove_automation",
            {"identifier": automation_entity},
        )

    async def test_call_service_invalid_domain(self, mcp_client):
        """Test calling service with invalid domain."""
        logger.info("Testing ha_call_service with invalid domain")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "invalid_domain_xyz",
                "service": "some_service",
                "entity_id": "some.entity",
            },
        )

        data = parse_mcp_result(result)

        # Should return error
        assert data.get("success") is False or "error" in data, (
            f"Expected error for invalid domain: {data}"
        )

        logger.info("Invalid domain properly returned error")

    async def test_call_service_invalid_service(self, mcp_client, test_light_entity):
        """Test calling invalid service on valid domain."""
        logger.info("Testing ha_call_service with invalid service")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "invalid_service_xyz",
                "entity_id": test_light_entity,
            },
        )

        data = parse_mcp_result(result)

        # Should return error
        assert data.get("success") is False or "error" in data, (
            f"Expected error for invalid service: {data}"
        )

        logger.info("Invalid service properly returned error")

    async def test_call_service_scene_turn_on(self, mcp_client):
        """Test calling scene.turn_on service if scenes exist."""
        logger.info("Testing ha_call_service with scene")

        # Search for a scene
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "scene", "limit": 5},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if not results:
            pytest.skip("No scene entities available for testing")

        scene_entity = results[0].get("entity_id")
        logger.info(f"Testing scene.turn_on with {scene_entity}")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "scene",
                "service": "turn_on",
                "entity_id": scene_entity,
            },
        )

        data = assert_mcp_success(result, "Scene turn_on service call")
        logger.info(f"Scene activation executed: {data.get('message')}")

    async def test_call_service_data_as_json_string(
        self, mcp_client, test_light_entity
    ):
        """Test calling service with data provided as JSON string."""
        logger.info("Testing ha_call_service with JSON string data")

        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": test_light_entity,
                "data": '{"brightness_pct": 75}',  # JSON string
            },
        )

        data = assert_mcp_success(result, "Service with JSON string data")
        logger.info(f"Service with JSON string executed: {data.get('message')}")

    async def test_call_service_without_entity_id(self, mcp_client):
        """Test calling domain-wide service without entity_id."""
        logger.info("Testing ha_call_service without entity_id")

        # Use homeassistant.check_config as it works without entity_id
        result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "homeassistant",
                "service": "check_config",
            },
        )

        # This service may or may not succeed depending on HA config
        data = parse_mcp_result(result)
        logger.info(f"Service without entity_id result: {data}")


def get_entity_id_from_response(data: dict, helper_type: str) -> str | None:
    """Extract entity_id from helper create response."""
    entity_id = data.get("entity_id")
    if not entity_id:
        # Try to get from helper_data.id
        helper_id = data.get("helper_data", {}).get("id")
        if helper_id:
            entity_id = f"{helper_type}.{helper_id}"
    return entity_id


@pytest.mark.asyncio
@pytest.mark.core
async def test_call_service_input_boolean_toggle(mcp_client, cleanup_tracker):
    """Test toggling an input_boolean helper."""
    logger.info("Testing ha_call_service with input_boolean")

    # Create a test input_boolean
    create_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": "Service Test Boolean",
            "initial": "off",
        },
    )
    create_data = parse_mcp_result(create_result)

    if not create_data.get("success"):
        pytest.skip(f"Could not create test input_boolean: {create_data}")

    entity_id = get_entity_id_from_response(create_data, "input_boolean")
    if not entity_id:
        pytest.skip(f"Could not determine entity_id from response: {create_data}")

    cleanup_tracker.track("input_boolean", entity_id)
    logger.info(f"Created test input_boolean: {entity_id}")


    # Toggle the input_boolean
    result = await mcp_client.call_tool(
        "ha_call_service",
        {
            "domain": "input_boolean",
            "service": "toggle",
            "entity_id": entity_id,
        },
    )

    data = assert_mcp_success(result, "Input boolean toggle")
    logger.info(f"Input boolean toggle executed: {data.get('message')}")

    # Verify state changed to on
    state_result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": entity_id},
    )
    state_data = parse_mcp_result(state_result)
    if state_data.get("success"):
        current_state = state_data.get("data", {}).get("state")
        logger.info(f"Input boolean state after toggle: {current_state}")
        assert current_state == "on", f"Should be on after toggle from off: {current_state}"

    # Cleanup
    await mcp_client.call_tool(
        "ha_config_remove_helper",
        {"helper_type": "input_boolean", "helper_id": entity_id},
    )
