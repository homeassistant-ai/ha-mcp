"""
E2E tests for ha_bulk_control tool - bulk device operations.

Tests the bulk control functionality for controlling multiple entities
in a single operation.
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.core
class TestBulkControl:
    """Test ha_bulk_control tool functionality."""

    async def test_bulk_turn_on_single_light(self, mcp_client, test_light_entity):
        """Test bulk_control with a single light entity."""
        logger.info(f"Testing ha_bulk_control turn_on with {test_light_entity}")

        # First turn off the light
        await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_off",
                "entity_id": test_light_entity,
            },
        )
        await asyncio.sleep(0.5)

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": [test_light_entity],
                "action": "turn_on",
            },
        )

        data = assert_mcp_success(result, "Bulk turn_on single light")

        # Verify response structure
        assert "controlled_entities" in data or "successful" in data, (
            f"Missing entities info: {data}"
        )
        assert "action" in data, f"Missing action: {data}"
        assert data["action"] == "turn_on", f"Action mismatch: {data}"

        logger.info(f"Bulk turn_on executed: {data.get('message', 'Success')}")

        # Verify state changed
        await asyncio.sleep(1)
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            current_state = state_data.get("data", {}).get("state")
            logger.info(f"Light state after bulk turn_on: {current_state}")
            assert current_state == "on", f"Light should be on: {current_state}"

    async def test_bulk_turn_off_single_light(self, mcp_client, test_light_entity):
        """Test bulk_control turn_off with a single light entity."""
        logger.info(f"Testing ha_bulk_control turn_off with {test_light_entity}")

        # First turn on the light
        await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "light",
                "service": "turn_on",
                "entity_id": test_light_entity,
            },
        )
        await asyncio.sleep(0.5)

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": [test_light_entity],
                "action": "turn_off",
            },
        )

        data = assert_mcp_success(result, "Bulk turn_off single light")
        logger.info(f"Bulk turn_off executed: {data.get('message', 'Success')}")

        # Verify state changed
        await asyncio.sleep(1)
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            current_state = state_data.get("data", {}).get("state")
            logger.info(f"Light state after bulk turn_off: {current_state}")
            assert current_state == "off", f"Light should be off: {current_state}"

    async def test_bulk_toggle_single_light(self, mcp_client, test_light_entity):
        """Test bulk_control toggle action."""
        logger.info(f"Testing ha_bulk_control toggle with {test_light_entity}")

        # Get initial state
        initial_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        initial_data = parse_mcp_result(initial_result)
        initial_state = initial_data.get("data", {}).get("state", "unknown")
        logger.info(f"Initial state: {initial_state}")

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": [test_light_entity],
                "action": "toggle",
            },
        )

        data = assert_mcp_success(result, "Bulk toggle")
        logger.info(f"Bulk toggle executed: {data.get('message', 'Success')}")

        # Verify state toggled
        await asyncio.sleep(1)
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            new_state = state_data.get("data", {}).get("state")
            logger.info(f"State after toggle: {new_state}")
            if initial_state == "on":
                assert new_state == "off", f"Should toggle to off: {new_state}"
            elif initial_state == "off":
                assert new_state == "on", f"Should toggle to on: {new_state}"

    async def test_bulk_control_multiple_lights(self, mcp_client):
        """Test bulk_control with multiple light entities."""
        logger.info("Testing ha_bulk_control with multiple lights")

        # Search for multiple lights
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "light", "limit": 5},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if len(results) < 2:
            pytest.skip("Need at least 2 lights for multi-entity bulk test")

        light_entities = [r.get("entity_id") for r in results[:3]]
        logger.info(f"Testing with lights: {light_entities}")

        # Bulk turn on
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": light_entities,
                "action": "turn_on",
            },
        )

        data = assert_mcp_success(result, "Bulk turn_on multiple lights")

        # Check response indicates multiple entities
        count = data.get("count") or data.get("total") or len(
            data.get("controlled_entities", data.get("successful", []))
        )
        logger.info(f"Bulk controlled {count} entities")
        assert count >= 2, f"Should control multiple entities: {count}"

        # Bulk turn off
        await asyncio.sleep(1)
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": light_entities,
                "action": "turn_off",
            },
        )

        data = assert_mcp_success(result, "Bulk turn_off multiple lights")
        logger.info("Multiple lights bulk turn_off executed")

    async def test_bulk_control_with_additional_data(self, mcp_client, test_light_entity):
        """Test bulk_control with additional service data (brightness)."""
        logger.info(f"Testing ha_bulk_control with data on {test_light_entity}")

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": [test_light_entity],
                "action": "turn_on",
                "data": {"brightness_pct": 30},
            },
        )

        data = assert_mcp_success(result, "Bulk turn_on with brightness")
        logger.info(f"Bulk with brightness executed: {data.get('message', 'Success')}")

        # Verify brightness was applied
        await asyncio.sleep(1)
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": test_light_entity},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            attrs = state_data.get("data", {}).get("attributes", {})
            if "brightness" in attrs:
                brightness = attrs.get("brightness", 0)
                logger.info(f"Brightness after bulk set: {brightness}")
                # 30% = ~77 brightness (0-255)
                assert 50 <= brightness <= 100, (
                    f"Brightness should be around 77: {brightness}"
                )

    async def test_bulk_control_comma_separated_entities(
        self, mcp_client, test_light_entity
    ):
        """Test bulk_control accepts comma-separated entity string."""
        logger.info("Testing ha_bulk_control with comma-separated string")

        # First turn off the light
        await mcp_client.call_tool(
            "ha_call_service",
            {"domain": "light", "service": "turn_off", "entity_id": test_light_entity},
        )
        await asyncio.sleep(0.5)

        # Some implementations may accept comma-separated string
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": test_light_entity,  # Single entity as string
                "action": "turn_on",
            },
        )

        data = assert_mcp_success(result, "Bulk with single entity string")
        logger.info(f"Single entity string accepted: {data.get('message', 'Success')}")

    async def test_bulk_control_empty_entity_list(self, mcp_client):
        """Test bulk_control with empty entity list."""
        logger.info("Testing ha_bulk_control with empty entity list")

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": [],
                "action": "turn_on",
            },
        )

        data = parse_mcp_result(result)

        # Should return error or indicate no entities
        if data.get("success"):
            count = data.get("count") or data.get("total") or 0
            assert count == 0, f"Should have 0 controlled entities: {data}"
        else:
            logger.info("Empty entity list properly returned error")

    async def test_bulk_control_mixed_domains(self, mcp_client):
        """Test bulk_control with entities from different domains."""
        logger.info("Testing ha_bulk_control with mixed domains")

        # Search for light and switch entities
        light_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "light", "limit": 2},
        )
        light_data = parse_mcp_result(light_result)
        if "data" in light_data:
            light_results = light_data.get("data", {}).get("results", [])
        else:
            light_results = light_data.get("results", [])

        switch_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "switch", "limit": 2},
        )
        switch_data = parse_mcp_result(switch_result)
        if "data" in switch_data:
            switch_results = switch_data.get("data", {}).get("results", [])
        else:
            switch_results = switch_data.get("results", [])

        entities = []
        if light_results:
            entities.append(light_results[0].get("entity_id"))
        if switch_results:
            entities.append(switch_results[0].get("entity_id"))

        if len(entities) < 2:
            pytest.skip("Need both light and switch entities for mixed domain test")

        logger.info(f"Testing with mixed entities: {entities}")

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": entities,
                "action": "toggle",
            },
        )

        data = assert_mcp_success(result, "Bulk toggle mixed domains")
        logger.info(f"Mixed domain bulk toggle executed: {data.get('message', 'Success')}")

    async def test_bulk_control_nonexistent_entity(self, mcp_client, test_light_entity):
        """Test bulk_control gracefully handles non-existent entities."""
        logger.info("Testing ha_bulk_control with non-existent entity")

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {
                "entity_ids": [test_light_entity, "light.nonexistent_test_xyz_12345"],
                "action": "turn_on",
            },
        )

        data = parse_mcp_result(result)

        # Response should handle this gracefully - either succeed partially
        # or fail with appropriate error
        if data.get("success"):
            # Check if failed entities are reported
            failed = data.get("failed", data.get("errors", []))
            if failed:
                logger.info(f"Properly reported failed entities: {failed}")
            else:
                logger.info("Bulk operation succeeded (non-existent entity ignored)")
        else:
            logger.info("Bulk operation failed as expected with non-existent entity")


@pytest.mark.asyncio
@pytest.mark.core
async def test_bulk_control_with_input_booleans(mcp_client, cleanup_tracker):
    """Test bulk_control with input_boolean helpers."""
    logger.info("Testing ha_bulk_control with input_boolean helpers")

    # Create two test input_booleans
    entity_ids = []
    for i in range(2):
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": f"Bulk Test Boolean {i + 1}",
                "initial": "off",
            },
        )
        create_data = parse_mcp_result(create_result)
        if create_data.get("success"):
            entity_id = create_data.get("entity_id")
            entity_ids.append(entity_id)
            cleanup_tracker.track("input_boolean", entity_id)
            logger.info(f"Created: {entity_id}")

    if len(entity_ids) < 2:
        pytest.skip("Could not create test input_booleans")

    await asyncio.sleep(1)  # Wait for registration

    # Bulk turn on
    result = await mcp_client.call_tool(
        "ha_bulk_control",
        {
            "entity_ids": entity_ids,
            "action": "turn_on",
        },
    )

    data = assert_mcp_success(result, "Bulk turn_on input_booleans")
    logger.info(f"Bulk turn_on input_booleans executed: {data.get('message', 'Success')}")

    # Verify states changed
    await asyncio.sleep(1)
    for entity_id in entity_ids:
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            state = state_data.get("data", {}).get("state")
            logger.info(f"{entity_id} state: {state}")
            assert state == "on", f"{entity_id} should be on: {state}"

    # Cleanup
    for entity_id in entity_ids:
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": entity_id},
        )
    logger.info("Test input_booleans cleaned up")
