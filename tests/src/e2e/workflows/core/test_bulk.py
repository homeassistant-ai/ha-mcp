"""
E2E tests for ha_bulk_control tool - bulk device operations.

Tests the bulk control functionality for controlling multiple entities
in a single operation.

Note: ha_bulk_control expects 'operations' parameter as a list of dicts,
each containing 'entity_id' and 'action' keys.
"""

import logging
from uuid import uuid4

import pytest

from ...utilities.assertions import (
    MCPAssertions,
    assert_mcp_success,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_entity_state

logger = logging.getLogger(__name__)


def create_operations(
    entities: list[str], action: str, parameters: dict | None = None
) -> list[dict]:
    """Create operations list for bulk_control."""
    ops = []
    for entity_id in entities:
        op = {"entity_id": entity_id, "action": action}
        if parameters:
            op["parameters"] = parameters
        ops.append(op)
    return ops


def _extract_bulk_boolean_entity_id(data: dict) -> str | None:
    """Extract the input_boolean entity_id from a set_helper response."""
    entity_id = data.get("entity_id")
    if not entity_id:
        helper_id = data.get("data", {}).get("id")
        if helper_id:
            entity_id = f"input_boolean.{helper_id}"
    return entity_id


@pytest.mark.asyncio
@pytest.mark.core
class TestBulkControl:
    """Test ha_bulk_control tool functionality."""

    async def test_selector_dry_run_resolves_area_and_exclusion(self, mcp_client):
        """Preview exact area leaves after applying an entity exclusion."""
        suffix = uuid4().hex[:8]
        area_id: str | None = None
        entity_ids: list[str] = []
        try:
            async with MCPAssertions(mcp_client) as mcp:
                area_data = await mcp.call_tool_success(
                    "ha_set_area_or_floor",
                    {"kind": "area", "name": f"Bulk selector {suffix}"},
                )
                area_id = area_data["area_id"]

                for label in ("included", "excluded"):
                    create_data = await mcp.call_tool_success(
                        "ha_config_set_helper",
                        {
                            "helper_type": "input_boolean",
                            "name": f"Bulk selector {label} {suffix}",
                            "initial": False,
                        },
                    )
                    entity_id = _extract_bulk_boolean_entity_id(create_data)
                    assert entity_id, f"Missing helper entity_id: {create_data}"
                    entity_ids.append(entity_id)
                    await mcp.call_tool_success(
                        "ha_set_entity",
                        {"entity_id": entity_id, "area_id": area_id},
                    )

                for entity_id in entity_ids:
                    assert await wait_for_entity_state(mcp_client, entity_id, "off"), (
                        f"Selector helper {entity_id} was not registered in time"
                    )

                data = await mcp.call_tool_success(
                    "ha_bulk_control",
                    {
                        "selector": {
                            "domain": "input_boolean",
                            "area_ids": [area_id],
                            "exclude_entity_ids": [entity_ids[1]],
                        },
                        "action": "off",
                        "dry_run": True,
                    },
                )

            assert data["dry_run"] is True
            assert data["dispatched"] is False
            assert data["resolution"]["resolved_entity_ids"] == [entity_ids[0]]
            assert data["resolution"]["excluded_entity_ids"] == [entity_ids[1]]
        finally:
            for entity_id in entity_ids:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {
                        "helper_type": "input_boolean",
                        "target": entity_id,
                        "confirm": True,
                    },
                )
            if area_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_area_or_floor",
                    {"kind": "area", "id": area_id},
                )

    async def test_selector_dispatch_turns_off_included_and_spares_excluded(
        self, mcp_client
    ):
        """A real (non-dry-run) dispatch actually spares the excluded entity.

        The dry-run test above only asserts the PREVIEW names the right
        entities; it never observes a real dispatch or the excluded
        entity's actual post-dispatch state. This is the guarantee the PR
        exists to make -- so turn both helpers on, dispatch for real, and
        confirm the excluded one's live state never moved.
        """
        suffix = uuid4().hex[:8]
        area_id: str | None = None
        entity_ids: list[str] = []
        try:
            async with MCPAssertions(mcp_client) as mcp:
                area_data = await mcp.call_tool_success(
                    "ha_set_area_or_floor",
                    {"kind": "area", "name": f"Bulk dispatch {suffix}"},
                )
                area_id = area_data["area_id"]

                for label in ("included", "excluded"):
                    create_data = await mcp.call_tool_success(
                        "ha_config_set_helper",
                        {
                            "helper_type": "input_boolean",
                            "name": f"Bulk dispatch {label} {suffix}",
                            "initial": True,
                        },
                    )
                    entity_id = _extract_bulk_boolean_entity_id(create_data)
                    assert entity_id, f"Missing helper entity_id: {create_data}"
                    entity_ids.append(entity_id)
                    await mcp.call_tool_success(
                        "ha_set_entity",
                        {"entity_id": entity_id, "area_id": area_id},
                    )

                for entity_id in entity_ids:
                    assert await wait_for_entity_state(mcp_client, entity_id, "on"), (
                        f"Bulk dispatch helper {entity_id} was not registered in time"
                    )

                included_id, excluded_id = entity_ids
                data = await mcp.call_tool_success(
                    "ha_bulk_control",
                    {
                        "selector": {
                            "domain": "input_boolean",
                            "area_ids": [area_id],
                            "exclude_entity_ids": [excluded_id],
                        },
                        "action": "off",
                    },
                )

                assert await wait_for_entity_state(mcp_client, included_id, "off"), (
                    f"Included helper {included_id} was not turned off by the dispatch"
                )
                excluded_data = await mcp.call_tool_success(
                    "ha_get_state", {"entity_id": excluded_id}
                )
                assert excluded_data.get("data", {}).get("state") == "on", (
                    f"Excluded helper's real state must never move: got {excluded_data}"
                )

            assert data.get("dry_run") is None
            assert data["resolution"]["resolved_entity_ids"] == [included_id]
            assert data["resolution"]["excluded_entity_ids"] == [excluded_id]
        finally:
            for entity_id in entity_ids:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_helpers_integrations",
                    {
                        "helper_type": "input_boolean",
                        "target": entity_id,
                        "confirm": True,
                    },
                )
            if area_id is not None:
                await safe_call_tool(
                    mcp_client,
                    "ha_remove_area_or_floor",
                    {"kind": "area", "id": area_id},
                )

    async def test_operations_mode_rejects_real_group_and_member_conflict(
        self, mcp_client
    ):
        """A real HA group entity plus one of its own members must be rejected.

        This is the live bug the group-safety gate exists to close: a batch
        built from an entity list (operations mode) that names an aggregate
        alongside one of the individual members it already cascades to. Unit
        tests cover the detection logic against mocked state; this drives the
        same gate through a group.set-created group so the check is proven
        against Home Assistant's real entity_id/member_entity_ids shape, not
        just a fixture.
        """
        # Discovered dynamically, not hardcoded to specific demo-platform
        # entities: mirrors test_bulk_control_multiple_lights' own pattern
        # for finding real lights to build a batch from.
        search_result = await mcp_client.call_tool(
            "ha_search", {"domain_filter": "light", "limit": 5}
        )
        search_data = parse_mcp_result(search_result)
        results = search_data.get("entities", [])
        if len(results) < 2:
            pytest.skip("Need at least 2 lights to build a real group for this test")
        member_entity_ids = [r.get("entity_id") for r in results[:2]]

        object_id = f"test_e2e_bulk_conflict_{uuid4().hex[:8]}"
        group_entity_id = f"group.{object_id}"

        async with MCPAssertions(mcp_client) as mcp:
            create_data = await mcp.call_tool_success(
                "ha_config_set_group",
                {
                    "object_id": object_id,
                    "name": "E2E Bulk Conflict Test",
                    "entities": member_entity_ids,
                },
            )
            assert create_data.get("entity_id") == group_entity_id, (
                f"Entity ID mismatch: {create_data}"
            )

            try:
                # The group and one of its own real members in the same
                # batch -- the exact shape that let a group's cascade
                # silently override an entity meant to be spared.
                await mcp.call_tool_failure(
                    "ha_bulk_control",
                    {
                        "operations": create_operations(
                            [group_entity_id, member_entity_ids[0]], "off"
                        )
                    },
                    expected_error="group/aggregate entity",
                )
                logger.info("Group+member conflict correctly rejected")

                # The group alone is unambiguous and must still dispatch --
                # proves the rejection above is about the conflict, not
                # about the group entity being untouchable.
                await mcp.call_tool_success(
                    "ha_bulk_control",
                    {"operations": create_operations([group_entity_id], "off")},
                )
                logger.info("Group targeted alone dispatched normally")
            finally:
                # safe_call_tool, not mcp.call_tool_success: a cleanup
                # failure here must not raise inside `finally` and mask a
                # real assertion failure from the try block above.
                await safe_call_tool(
                    mcp_client, "ha_config_remove_group", {"object_id": object_id}
                )

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

        operations = create_operations([test_light_entity], "on")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_on single light")

        # Verify response structure
        assert "total_operations" in data, f"Missing total_operations: {data}"
        assert data["total_operations"] == 1, f"Should have 1 operation: {data}"

        logger.info(
            f"Bulk turn_on executed: successful={data.get('successful_commands')}"
        )

        # Verify state changed
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

        operations = create_operations([test_light_entity], "off")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        data = assert_mcp_success(result, "Bulk turn_off single light")
        logger.info(
            f"Bulk turn_off executed: successful={data.get('successful_commands')}"
        )

        # Verify state changed
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
        logger.info(
            f"Bulk toggle executed: successful={data.get('successful_commands')}"
        )

        # Verify state toggled
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
            "ha_search",
            {"domain_filter": "light", "limit": 5},
        )
        search_data = parse_mcp_result(search_result)

        results = search_data.get("entities", [])

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
        operations = create_operations(light_entities, "off")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations},
        )

        assert_mcp_success(result, "Bulk turn_off multiple lights")
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
        logger.info(
            f"Bulk with brightness executed: successful={data.get('successful_commands')}"
        )

        # Verify brightness was applied
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

    async def test_bulk_control_empty_operations(self, mcp_client):
        """Test bulk_control with empty operations list."""
        logger.info("Testing ha_bulk_control with empty operations list")

        data = await safe_call_tool(
            mcp_client,
            "ha_bulk_control",
            {"operations": []},
        )

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
            "ha_search",
            {"domain_filter": "light", "limit": 2},
        )
        light_data = parse_mcp_result(light_result)
        light_results = light_data.get("entities", [])

        switch_result = await mcp_client.call_tool(
            "ha_search",
            {"domain_filter": "switch", "limit": 2},
        )
        switch_data = parse_mcp_result(switch_result)
        switch_results = switch_data.get("entities", [])

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
        logger.info(
            f"Mixed domain bulk toggle executed: total={data.get('total_operations')}"
        )

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
                logger.info(
                    "Bulk operation completed (non-existent entity may be ignored)"
                )
        else:
            logger.info("Bulk operation returned error as expected")

    async def test_bulk_control_parallel_execution(self, mcp_client):
        """Test bulk_control with parallel execution (default)."""
        logger.info("Testing ha_bulk_control parallel execution")

        # Search for lights
        search_result = await mcp_client.call_tool(
            "ha_search",
            {"domain_filter": "light", "limit": 3},
        )
        search_data = parse_mcp_result(search_result)

        results = search_data.get("entities", [])

        if len(results) < 2:
            pytest.skip("Need at least 2 lights for parallel test")

        light_entities = [r.get("entity_id") for r in results[:3]]

        operations = create_operations(light_entities, "on")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations, "parallel": True},
        )

        data = assert_mcp_success(result, "Bulk parallel execution")
        # Verify operations completed
        total = data.get("total_operations", 0)
        assert total >= 2, f"Should have completed operations: {total}"

        exec_mode = data.get("execution_mode", "not_reported")
        logger.info(f"Parallel execution completed: {exec_mode}")

    async def test_bulk_control_sequential_execution(self, mcp_client):
        """Test bulk_control with sequential execution parameter."""
        logger.info("Testing ha_bulk_control sequential execution")

        # Search for lights
        search_result = await mcp_client.call_tool(
            "ha_search",
            {"domain_filter": "light", "limit": 3},
        )
        search_data = parse_mcp_result(search_result)

        results = search_data.get("entities", [])

        if len(results) < 2:
            pytest.skip("Need at least 2 lights for sequential test")

        light_entities = [r.get("entity_id") for r in results[:3]]

        operations = create_operations(light_entities, "off")
        result = await mcp_client.call_tool(
            "ha_bulk_control",
            {"operations": operations, "parallel": False},
        )

        data = assert_mcp_success(result, "Bulk sequential execution")
        # Note: API may or may not report execution_mode; it may always run parallel
        # The important thing is that the operation succeeds with parallel=False
        exec_mode = data.get("execution_mode", "not_reported")
        logger.info(f"Execution completed with mode: {exec_mode}")

        # Verify operations completed
        total = data.get("total_operations", 0)
        assert total >= 2, f"Should have completed operations: {total}"


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
            entity_id = _extract_bulk_boolean_entity_id(create_data)
            if entity_id:
                entity_ids.append(entity_id)
                cleanup_tracker.track("input_boolean", entity_id)
                logger.info(f"Created: {entity_id}")

    if len(entity_ids) < 2:
        pytest.skip("Could not create test input_booleans")

    # Bulk turn on
    operations = create_operations(entity_ids, "on")
    result = await mcp_client.call_tool(
        "ha_bulk_control",
        {"operations": operations},
    )

    data = assert_mcp_success(result, "Bulk turn_on input_booleans")
    logger.info(
        f"Bulk turn_on input_booleans executed: total={data.get('total_operations')}"
    )

    # Verify states changed
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
            "ha_remove_helpers_integrations",
            {"helper_type": "input_boolean", "target": entity_id, "confirm": True},
        )
    logger.info("Test input_booleans cleaned up")
