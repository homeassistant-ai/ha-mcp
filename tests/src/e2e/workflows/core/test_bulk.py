"""
E2E tests for ha_bulk_control tool - bulk device operations.

Tests the bulk control functionality for controlling multiple entities
in a single operation.

Note: ha_bulk_control expects 'operations' parameter as a list of dicts,
each containing 'entity_id' and 'action' keys.
"""

import asyncio
import json
import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


def create_operations(entities: list[str], action: str, parameters: dict | None = None) -> list[dict]:
    """Create operations list for bulk_control."""
    ops = []
    for entity_id in entities:
        op = {"entity_id": entity_id, "action": action}
        if parameters:
            op["parameters"] = parameters
        ops.append(op)
    return ops


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

        operations = create_operations([test_light_entity], "on")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_on single light")

        # Verify response structure
        assert "total_operations" in data, f"Missing total_operations: {data}"
        assert data["total_operations"] == 1, f"Should have 1 operation: {data}"

        logger.info(f"Bulk turn_on executed: successful={data.get('successful_commands')}")

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

        operations = create_operations([test_light_entity], "off")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_off single light")
        logger.info(f"Bulk turn_off executed: successful={data.get('successful_commands')}")

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

        operations = create_operations([test_light_entity], "toggle")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk toggle")
        logger.info(f"Bulk toggle executed: successful={data.get('successful_commands')}")

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
        operations = create_operations(light_entities, "on")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_on multiple lights")

        # Check response indicates multiple entities
        total = data.get("total_operations", 0)
        logger.info(f"Bulk controlled {total} entities")
        assert total >= 2, f"Should control multiple entities: {total}"

        # Bulk turn off
        await asyncio.sleep(1)
        operations = create_operations(light_entities, "off")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_off multiple lights")
        logger.info("Multiple lights bulk turn_off executed")

    async def test_bulk_control_with_parameters(self, mcp_client, test_light_entity):
        """Test bulk_control with additional parameters (brightness)."""
        logger.info(f"Testing ha_bulk_control with parameters on {test_light_entity}")

        operations = [
            {
                "entity_id": test_light_entity,
                "action": "on",
                "parameters": {"brightness_pct": 30},
            }
        ]
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_on with brightness")
        logger.info(f"Bulk with brightness executed: successful={data.get('successful_commands')}")

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

    async def test_bulk_control_json_string_operations(self, mcp_client, test_light_entity):
        """Test bulk_control accepts operations as JSON string."""
        logger.info("Testing ha_bulk_control with JSON string operations")

        # First turn off the light
        await mcp_client.call_tool(
            "ha_call_service",
            {"domain": "light", "service": "turn_off", "entity_id": test_light_entity},
        )
        await asyncio.sleep(0.5)

        # Operations as JSON string
        operations_json = json.dumps([{"entity_id": test_light_entity, "action": "on"}])
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations_json},
        )

        data = assert_mcp_success(result, "Bulk with JSON string operations")
        logger.info(f"JSON string operations accepted: successful={data.get('successful_commands')}")

    async def test_bulk_control_empty_operations(self, mcp_client):
        """Test bulk_control with empty operations list."""
        logger.info("Testing ha_bulk_control with empty operations list")

        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": []},
        )

        data = parse_mcp_result(result)

        # Should return error or indicate no operations
        if data.get("success"):
            total = data.get("total_operations", 0)
            assert total == 0, f"Should have 0 operations: {data}"
        else:
            logger.info("Empty operations list properly returned error")

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

        operations = create_operations(entities, "toggle")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk toggle mixed domains")
        logger.info(f"Mixed domain bulk toggle executed: total={data.get('total_operations')}")

    async def test_bulk_control_nonexistent_entity(self, mcp_client, test_light_entity):
        """Test bulk_control gracefully handles non-existent entities."""
        logger.info("Testing ha_bulk_control with non-existent entity")

        operations = [
            {"entity_id": test_light_entity, "action": "on"},
            {"entity_id": "light.nonexistent_test_xyz_12345", "action": "on"},
        ]
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = parse_mcp_result(result)

        # Response should handle this gracefully - either succeed partially
        # or fail with appropriate error
        if "total_operations" in data:
            failed = data.get("failed_commands", 0)
            if failed > 0:
                logger.info(f"Properly reported failed commands: {failed}")
            else:
                logger.info("Bulk operation completed (non-existent entity may be ignored)")
        else:
            logger.info("Bulk operation returned error as expected")

    async def test_bulk_control_parallel_execution(self, mcp_client):
        """Test bulk_control with parallel execution (default)."""
        logger.info("Testing ha_bulk_control parallel execution")

        # Search for lights
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "light", "limit": 3},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if len(results) < 2:
            pytest.skip("Need at least 2 lights for parallel test")

        light_entities = [r.get("entity_id") for r in results[:3]]

        operations = create_operations(light_entities, "on")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations, "parallel": True},
        )

        data = assert_mcp_success(result, "Bulk parallel execution")
        assert data.get("execution_mode") == "parallel", (
            f"Should be parallel mode: {data.get('execution_mode')}"
        )
        logger.info(f"Parallel execution completed: {data.get('execution_mode')}")

    async def test_bulk_control_sequential_execution(self, mcp_client):
        """Test bulk_control with sequential execution."""
        logger.info("Testing ha_bulk_control sequential execution")

        # Search for lights
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "light", "limit": 3},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if len(results) < 2:
            pytest.skip("Need at least 2 lights for sequential test")

        light_entities = [r.get("entity_id") for r in results[:3]]

        operations = create_operations(light_entities, "off")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations, "parallel": False},
        )

        data = assert_mcp_success(result, "Bulk sequential execution")
        assert data.get("execution_mode") == "sequential", (
            f"Should be sequential mode: {data.get('execution_mode')}"
        )
        logger.info(f"Sequential execution completed: {data.get('execution_mode')}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_bulk_control_with_input_booleans(mcp_client, cleanup_tracker):
    """Test bulk_control with input_boolean helpers."""
    logger.info("Testing ha_bulk_control with input_boolean helpers")

    # Helper function to extract entity_id
    def get_entity_id(data: dict) -> str | None:
        entity_id = data.get("entity_id")
        if not entity_id:
            helper_id = data.get("helper_data", {}).get("id")
            if helper_id:
                entity_id = f"input_boolean.{helper_id}"
        return entity_id

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
            entity_id = get_entity_id(create_data)
            if entity_id:
                entity_ids.append(entity_id)
                cleanup_tracker.track("input_boolean", entity_id)
                logger.info(f"Created: {entity_id}")

    if len(entity_ids) < 2:
        pytest.skip("Could not create test input_booleans")

    await asyncio.sleep(1)  # Wait for registration

    # Bulk turn on
    operations = create_operations(entity_ids, "on")
    result = await mcp_client.call_tool(
        "ha_bulk_control",
        {"operations": operations},
    )

    data = assert_mcp_success(result, "Bulk turn_on input_booleans")
    logger.info(f"Bulk turn_on input_booleans executed: total={data.get('total_operations')}")

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
