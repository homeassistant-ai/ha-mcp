"""
E2E tests for Home Assistant helper CRUD operations.

Tests the complete lifecycle of input_* helpers including:
- input_boolean, input_number, input_select, input_text, input_datetime, input_button
- List, create, update, and delete operations
- Type-specific parameter validation
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result, safe_call_tool
from ...utilities.wait_helpers import wait_for_condition, wait_for_entity_state

logger = logging.getLogger(__name__)


async def wait_for_entity_registration(mcp_client, entity_id: str, timeout: int = 20) -> bool:
    """
    Wait for entity to be registered and queryable via API.
    Does not check for specific state, only that entity exists.
    """
    import time
    start_time = time.time()
    attempt = 0

    async def entity_exists():
        nonlocal attempt
        attempt += 1
        data = await safe_call_tool(mcp_client, "ha_get_state", {"entity_id": entity_id})
        # Check if 'data' key exists (not 'success' key)
        success = 'data' in data and data['data'] is not None

        # Log every attempt with full details
        elapsed = time.time() - start_time
        logger.info(
            f"[Attempt {attempt} @ {elapsed:.1f}s] Checking {entity_id}: "
            f"success={success}, data keys={list(data.keys())}"
        )

        if success:
            state = data.get("data", {}).get("state", "N/A")
            logger.info(f"✅ Entity {entity_id} EXISTS with state='{state}'")
        else:
            error = data.get("error", "No error message")
            logger.warning(f"❌ Entity {entity_id} check failed: {error}")

        return success

    return await wait_for_condition(
        entity_exists, timeout=timeout, condition_name=f"{entity_id} registration"
    )


def get_entity_id_from_response(data: dict, helper_type: str) -> str | None:
    """Extract entity_id from helper create response.

    The API may return entity_id directly or we may need to construct it
    from helper_data.id.
    """
    entity_id = data.get("entity_id")
    if not entity_id:
        helper_id = data.get("helper_data", {}).get("id")
        if helper_id:
            entity_id = f"{helper_type}.{helper_id}"
    return entity_id


@pytest.mark.asyncio
@pytest.mark.config
class TestInputBooleanCRUD:
    """Test input_boolean helper CRUD operations."""

    async def test_list_input_booleans(self, mcp_client):
        """Test listing all input_boolean helpers."""
        logger.info("Testing ha_config_list_helpers for input_boolean")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_boolean"},
        )

        data = assert_mcp_success(result, "List input_boolean helpers")

        assert "helpers" in data, f"Missing 'helpers' in response: {data}"
        assert "count" in data, f"Missing 'count' in response: {data}"
        assert isinstance(data["helpers"], list), f"helpers should be a list: {data}"

        logger.info(f"Found {data['count']} input_boolean helpers")

    async def test_input_boolean_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete input_boolean lifecycle: create, list, update, delete."""
        logger.info("Testing input_boolean full lifecycle")

        helper_name = "E2E Test Boolean"

        # CREATE
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": helper_name,
                "icon": "mdi:toggle-switch",
            },
        )

        create_data = assert_mcp_success(create_result, "Create input_boolean")
        entity_id = get_entity_id_from_response(create_data, "input_boolean")
        assert entity_id, f"Missing entity_id in create response: {create_data}"
        cleanup_tracker.track("input_boolean", entity_id)
        logger.info(f"✨ Created input_boolean: {entity_id}")
        logger.info(f"📝 Creation response keys: {list(create_data.keys())}")

        # Wait for entity to be registered (existence only, not specific state)
        entity_ready = await wait_for_entity_registration(mcp_client, entity_id)
        assert entity_ready, f"Entity {entity_id} not registered within timeout"

        # LIST - Verify it appears
        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_boolean"},
        )
        list_data = assert_mcp_success(list_result, "List after create")

        found = False
        for helper in list_data.get("helpers", []):
            if helper.get("name") == helper_name:
                found = True
                break
        assert found, f"Created helper not found in list: {helper_name}"
        logger.info("Input boolean verified in list")

        # UPDATE
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": entity_id,
                "name": "E2E Test Boolean Updated",
                "icon": "mdi:checkbox-marked",
            },
        )
        update_data = assert_mcp_success(update_result, "Update input_boolean")
        logger.info(f"Updated input_boolean: {update_data.get('message')}")

        # DELETE
        delete_result = await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {
                "helper_type": "input_boolean",
                "target": entity_id,
                "confirm": True,
            },
        )
        delete_data = assert_mcp_success(delete_result, "Delete input_boolean")
        logger.info(f"Deleted input_boolean: {delete_data.get('message')}")

        # VERIFY DELETION - list operation reflects current state
        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_boolean"},
        )
        list_data = parse_mcp_result(list_result)

        for helper in list_data.get("helpers", []):
            assert helper.get("name") != "E2E Test Boolean Updated", (
                "Helper should be deleted"
            )
        logger.info("Input boolean deletion verified")

    async def test_input_boolean_with_initial_state(self, mcp_client, cleanup_tracker):
        """Test creating input_boolean with initial state."""
        logger.info("Testing input_boolean with initial state")

        # Create with initial=on
        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Initial On Boolean",
                "initial": "on",
            },
        )

        data = assert_mcp_success(result, "Create with initial state")
        entity_id = get_entity_id_from_response(data, "input_boolean")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_boolean", entity_id)
        logger.info(f"Created with initial=on: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_boolean", "target": entity_id, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputNumberCRUD:
    """Test input_number helper CRUD operations."""

    async def test_list_input_numbers(self, mcp_client):
        """Test listing all input_number helpers."""
        logger.info("Testing ha_config_list_helpers for input_number")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_number"},
        )

        data = assert_mcp_success(result, "List input_number helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} input_number helpers")

    async def test_input_number_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete input_number lifecycle with numeric settings."""
        logger.info("Testing input_number full lifecycle")

        helper_name = "E2E Test Number"

        # CREATE with numeric range
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": helper_name,
                "min_value": 0,
                "max_value": 100,
                "step": 5,
                "unit_of_measurement": "%",
                "mode": "slider",
            },
        )

        create_data = assert_mcp_success(create_result, "Create input_number")
        entity_id = get_entity_id_from_response(create_data, "input_number")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_number", entity_id)
        logger.info(f"Created input_number: {entity_id}")

        # Wait for entity to be registered (existence only, not specific state)
        entity_ready = await wait_for_entity_registration(mcp_client, entity_id)
        assert entity_ready, f"Entity {entity_id} not registered within timeout"

        # VERIFY via state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            attrs = state_data.get("data", {}).get("attributes", {})
            assert attrs.get("min") == 0, f"min mismatch: {attrs}"
            assert attrs.get("max") == 100, f"max mismatch: {attrs}"
            assert attrs.get("step") == 5, f"step mismatch: {attrs}"
            logger.info("Input number attributes verified")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_number", "target": entity_id, "confirm": True},
        )
        logger.info("Input number cleanup complete")

    async def test_input_number_box_mode(self, mcp_client, cleanup_tracker):
        """Test creating input_number with box mode."""
        logger.info("Testing input_number with box mode")

        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": "E2E Box Mode Number",
                "min_value": -50,
                "max_value": 50,
                "mode": "box",
            },
        )

        data = assert_mcp_success(result, "Create box mode input_number")
        entity_id = get_entity_id_from_response(data, "input_number")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_number", entity_id)
        logger.info(f"Created box mode number: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_number", "target": entity_id, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputSelectCRUD:
    """Test input_select helper CRUD operations."""

    async def test_list_input_selects(self, mcp_client):
        """Test listing all input_select helpers."""
        logger.info("Testing ha_config_list_helpers for input_select")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_select"},
        )

        data = assert_mcp_success(result, "List input_select helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} input_select helpers")

    async def test_input_select_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete input_select lifecycle with options."""
        logger.info("Testing input_select full lifecycle")

        helper_name = "E2E Test Select"
        options = ["Option A", "Option B", "Option C"]

        # CREATE
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": helper_name,
                "options": options,
                "initial": "Option B",
            },
        )

        create_data = assert_mcp_success(create_result, "Create input_select")
        entity_id = get_entity_id_from_response(create_data, "input_select")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_select", entity_id)
        logger.info(f"Created input_select: {entity_id}")

        # Wait for entity to be registered (existence only, not specific state)
        entity_ready = await wait_for_entity_registration(mcp_client, entity_id)
        assert entity_ready, f"Entity {entity_id} not registered within timeout"

        # VERIFY via state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            attrs = state_data.get("data", {}).get("attributes", {})
            state_options = attrs.get("options", [])
            logger.info(f"Input select options: {state_options}")
            for opt in options:
                assert opt in state_options, f"Option {opt} not in select: {state_options}"

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_select", "target": entity_id, "confirm": True},
        )
        logger.info("Input select cleanup complete")

    async def test_input_select_requires_options(self, mcp_client):
        """Test that input_select requires options."""
        logger.info("Testing input_select without options (should fail)")

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": "E2E No Options Select",
                # Missing required options
            },
        )
        assert data.get("success") is False, (
            f"Should fail without options: {data}"
        )
        logger.info("Input select properly requires options")


@pytest.mark.asyncio
@pytest.mark.config
class TestInputTextCRUD:
    """Test input_text helper CRUD operations."""

    async def test_list_input_texts(self, mcp_client):
        """Test listing all input_text helpers."""
        logger.info("Testing ha_config_list_helpers for input_text")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_text"},
        )

        data = assert_mcp_success(result, "List input_text helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} input_text helpers")

    async def test_input_text_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete input_text lifecycle with text settings."""
        logger.info("Testing input_text full lifecycle")

        helper_name = "E2E Test Text"

        # CREATE
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_text",
                "name": helper_name,
                "min_value": 1,  # Min length
                "max_value": 100,  # Max length
                "mode": "text",
                "initial": "Hello E2E",
            },
        )

        create_data = assert_mcp_success(create_result, "Create input_text")
        entity_id = get_entity_id_from_response(create_data, "input_text")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_text", entity_id)
        logger.info(f"Created input_text: {entity_id}")

        # Wait for entity to be registered (existence only, not specific state)
        entity_ready = await wait_for_entity_registration(mcp_client, entity_id)
        assert entity_ready, f"Entity {entity_id} not registered within timeout"

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_text", "target": entity_id, "confirm": True},
        )
        logger.info("Input text cleanup complete")

    async def test_input_text_password_mode(self, mcp_client, cleanup_tracker):
        """Test creating input_text with password mode."""
        logger.info("Testing input_text with password mode")

        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_text",
                "name": "E2E Password Text",
                "mode": "password",
            },
        )

        data = assert_mcp_success(result, "Create password mode input_text")
        entity_id = get_entity_id_from_response(data, "input_text")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_text", entity_id)
        logger.info(f"Created password text: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_text", "target": entity_id, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputDatetimeCRUD:
    """Test input_datetime helper CRUD operations."""

    async def test_list_input_datetimes(self, mcp_client):
        """Test listing all input_datetime helpers."""
        logger.info("Testing ha_config_list_helpers for input_datetime")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_datetime"},
        )

        data = assert_mcp_success(result, "List input_datetime helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} input_datetime helpers")

    async def test_input_datetime_date_only(self, mcp_client, cleanup_tracker):
        """Test creating input_datetime with date only."""
        logger.info("Testing input_datetime date only")

        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_datetime",
                "name": "E2E Date Only",
                "has_date": True,
                "has_time": False,
            },
        )

        data = assert_mcp_success(result, "Create date-only input_datetime")
        entity_id = get_entity_id_from_response(data, "input_datetime")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_datetime", entity_id)
        logger.info(f"Created date-only datetime: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_datetime", "target": entity_id, "confirm": True},
        )

    async def test_input_datetime_time_only(self, mcp_client, cleanup_tracker):
        """Test creating input_datetime with time only."""
        logger.info("Testing input_datetime time only")

        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_datetime",
                "name": "E2E Time Only",
                "has_date": False,
                "has_time": True,
            },
        )

        data = assert_mcp_success(result, "Create time-only input_datetime")
        entity_id = get_entity_id_from_response(data, "input_datetime")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_datetime", entity_id)
        logger.info(f"Created time-only datetime: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_datetime", "target": entity_id, "confirm": True},
        )

    async def test_input_datetime_both(self, mcp_client, cleanup_tracker):
        """Test creating input_datetime with both date and time."""
        logger.info("Testing input_datetime with date and time")

        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_datetime",
                "name": "E2E Full Datetime",
                "has_date": True,
                "has_time": True,
            },
        )

        data = assert_mcp_success(result, "Create full input_datetime")
        entity_id = get_entity_id_from_response(data, "input_datetime")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_datetime", entity_id)
        logger.info(f"Created full datetime: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_datetime", "target": entity_id, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestInputButtonCRUD:
    """Test input_button helper CRUD operations."""

    async def test_list_input_buttons(self, mcp_client):
        """Test listing all input_button helpers."""
        logger.info("Testing ha_config_list_helpers for input_button")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "input_button"},
        )

        data = assert_mcp_success(result, "List input_button helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} input_button helpers")

    async def test_input_button_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete input_button lifecycle."""
        logger.info("Testing input_button full lifecycle")

        helper_name = "E2E Test Button"

        # CREATE
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_button",
                "name": helper_name,
                "icon": "mdi:gesture-tap-button",
            },
        )

        create_data = assert_mcp_success(create_result, "Create input_button")
        entity_id = get_entity_id_from_response(create_data, "input_button")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_button", entity_id)
        logger.info(f"Created input_button: {entity_id}")

        # Wait for entity to be registered (buttons typically start in unknown state)
        state_reached = await wait_for_entity_state(
            mcp_client, entity_id, "unknown", timeout=10
        )
        assert state_reached, f"Entity {entity_id} not registered within timeout"

        # PRESS button via service
        press_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_button",
                "service": "press",
                "entity_id": entity_id,
            },
        )
        press_data = assert_mcp_success(press_result, "Press input_button")
        logger.info(f"Button pressed: {press_data.get('message')}")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "input_button", "target": entity_id, "confirm": True},
        )
        logger.info("Input button cleanup complete")


@pytest.mark.asyncio
@pytest.mark.config
async def test_helper_with_area_assignment(mcp_client, cleanup_tracker):
    """Test creating helper with area assignment."""
    logger.info("Testing helper creation with area assignment")

    # First, list areas to find one to use
    # Note: Areas may not exist in test environment
    result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": "E2E Area Boolean",
            # area_id would be set if we had a known area
        },
    )

    data = assert_mcp_success(result, "Create helper")
    entity_id = get_entity_id_from_response(data, "input_boolean")
    assert entity_id, f"Missing entity_id: {data}"
    cleanup_tracker.track("input_boolean", entity_id)
    logger.info(f"Created helper: {entity_id}")

    # Clean up
    await mcp_client.call_tool(
        "ha_delete_helpers_integrations",
        {"helper_type": "input_boolean", "target": entity_id, "confirm": True},
    )


@pytest.mark.asyncio
@pytest.mark.config
async def test_helper_delete_nonexistent(mcp_client):
    """Test deleting a non-existent helper."""
    logger.info("Testing delete of non-existent helper")

    data = await safe_call_tool(
        mcp_client,
        "ha_delete_helpers_integrations",
        {
            "helper_type": "input_boolean",
            "target": "nonexistent_helper_xyz_12345",
            "confirm": True,
        },
    )

    # Should either fail or indicate already deleted
    if data.get("success"):
        # Some implementations return success for idempotent delete
        method = data.get("method", "")
        if "already_deleted" in method:
            logger.info("Non-existent helper properly handled as already deleted")
        else:
            logger.info(f"Delete returned success: {data}")
    else:
        logger.info("Non-existent helper properly returned error")


@pytest.mark.asyncio
@pytest.mark.config
class TestCounterCRUD:
    """Test counter helper CRUD operations."""

    async def test_list_counters(self, mcp_client):
        """Test listing all counter helpers."""
        logger.info("Testing ha_config_list_helpers for counter")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "counter"},
        )

        data = assert_mcp_success(result, "List counter helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} counter helpers")

    async def test_counter_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete counter lifecycle with increment/decrement."""
        logger.info("Testing counter full lifecycle")

        helper_name = "E2E Test Counter"

        # CREATE counter with custom range
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "counter",
                "name": helper_name,
                "icon": "mdi:counter",
                "initial": 5,
                "min_value": 0,
                "max_value": 100,
                "step": 2,
            },
        )

        create_data = assert_mcp_success(create_result, "Create counter")
        entity_id = get_entity_id_from_response(create_data, "counter")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("counter", entity_id)
        logger.info(f"Created counter: {entity_id}")

        # Wait for entity to be registered with initial value
        state_reached = await wait_for_entity_state(
            mcp_client, entity_id, "5", timeout=10
        )
        assert state_reached, f"Entity {entity_id} not registered within timeout"

        # VERIFY via state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            state_value = state_data.get("data", {}).get("state")
            logger.info(f"Counter initial state: {state_value}")

        # INCREMENT counter
        inc_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "counter",
                "service": "increment",
                "entity_id": entity_id,
            },
        )
        assert_mcp_success(inc_result, "Increment counter")
        logger.info("Counter incremented")

        # RESET counter
        reset_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "counter",
                "service": "reset",
                "entity_id": entity_id,
            },
        )
        assert_mcp_success(reset_result, "Reset counter")
        logger.info("Counter reset")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "counter", "target": entity_id, "confirm": True},
        )
        logger.info("Counter cleanup complete")


@pytest.mark.asyncio
@pytest.mark.config
class TestTimerCRUD:
    """Test timer helper CRUD operations."""

    async def test_list_timers(self, mcp_client):
        """Test listing all timer helpers."""
        logger.info("Testing ha_config_list_helpers for timer")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "timer"},
        )

        data = assert_mcp_success(result, "List timer helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} timer helpers")

    async def test_timer_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete timer lifecycle with start/cancel."""
        logger.info("Testing timer full lifecycle")

        helper_name = "E2E Test Timer"

        # CREATE timer with duration
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "timer",
                "name": helper_name,
                "icon": "mdi:timer",
                "duration": "0:05:00",
                "restore": True,
            },
        )

        create_data = assert_mcp_success(create_result, "Create timer")
        entity_id = get_entity_id_from_response(create_data, "timer")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("timer", entity_id)
        logger.info(f"Created timer: {entity_id}")

        # Wait for entity to be registered in idle state
        state_reached = await wait_for_entity_state(
            mcp_client, entity_id, "idle", timeout=10
        )
        assert state_reached, f"Timer {entity_id} not registered in idle state within timeout"
        logger.info("Timer initial state: idle")

        # START timer
        start_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "timer",
                "service": "start",
                "entity_id": entity_id,
            },
        )
        assert_mcp_success(start_result, "Start timer")
        logger.info("Timer started")

        # Wait for timer to reach active state
        state_reached = await wait_for_entity_state(
            mcp_client, entity_id, "active", timeout=5
        )
        assert state_reached, f"Timer {entity_id} did not reach active state after start"

        # CANCEL timer
        cancel_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "timer",
                "service": "cancel",
                "entity_id": entity_id,
            },
        )
        assert_mcp_success(cancel_result, "Cancel timer")
        logger.info("Timer cancelled")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "timer", "target": entity_id, "confirm": True},
        )
        logger.info("Timer cleanup complete")


@pytest.mark.asyncio
@pytest.mark.config
class TestScheduleCRUD:
    """Test schedule helper CRUD operations."""

    async def test_list_schedules(self, mcp_client):
        """Test listing all schedule helpers."""
        logger.info("Testing ha_config_list_helpers for schedule")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "schedule"},
        )

        data = assert_mcp_success(result, "List schedule helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} schedule helpers")

    async def test_schedule_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete schedule lifecycle with weekday times."""
        logger.info("Testing schedule full lifecycle")

        helper_name = "E2E Test Schedule"

        # CREATE schedule with weekday time ranges
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "schedule",
                "name": helper_name,
                "icon": "mdi:calendar-clock",
                "monday": [{"from": "09:00", "to": "17:00"}],
                "tuesday": [{"from": "09:00", "to": "17:00"}],
                "wednesday": [{"from": "09:00", "to": "12:00"}, {"from": "13:00", "to": "17:00"}],
            },
        )

        create_data = assert_mcp_success(create_result, "Create schedule")
        entity_id = get_entity_id_from_response(create_data, "schedule")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("schedule", entity_id)
        logger.info(f"Created schedule: {entity_id}")

        # Wait for entity to be registered (schedule is either on or off depending on current time)
        async def check_schedule_exists():
            data = await safe_call_tool(mcp_client, "ha_get_state", {"entity_id": entity_id})
            # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
            if 'data' in data and data['data'] is not None:
                state = data.get("data", {}).get("state")
                return state in ["on", "off"]
            return False

        state_reached = await wait_for_condition(
            check_schedule_exists, timeout=10, condition_name=f"schedule {entity_id} registration"
        )
        assert state_reached, f"Schedule {entity_id} not registered within timeout"

        # VERIFY via state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        # Check if 'data' key exists (not 'success' key which doesn't exist in parse_mcp_result)
        if 'data' in state_data and state_data['data'] is not None:
            state_value = state_data.get("data", {}).get("state")
            logger.info(f"Schedule state: {state_value}")

        # LIST to verify schedule appears
        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "schedule"},
        )
        list_data = assert_mcp_success(list_result, "List schedules")
        found = any(h.get("name") == helper_name for h in list_data.get("helpers", []))
        assert found, "Created schedule not found in list"
        logger.info("Schedule verified in list")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "schedule", "target": entity_id, "confirm": True},
        )
        logger.info("Schedule cleanup complete")

    async def test_schedule_with_data_field(self, mcp_client, cleanup_tracker):
        """Test creating a schedule with additional data attributes on time blocks."""
        logger.info("Testing schedule with data field on time blocks")

        helper_name = "E2E Test Schedule Data"

        # CREATE schedule with 'data' field on time blocks
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "schedule",
                "name": helper_name,
                "icon": "mdi:calendar-clock",
                "monday": [
                    {"from": "07:00", "to": "22:00", "data": {"mode": "comfort"}},
                    {"from": "22:00", "to": "23:59", "data": {"mode": "sleep"}},
                ],
                "tuesday": [
                    {"from": "07:00", "to": "22:00", "data": {"mode": "comfort"}},
                ],
            },
        )

        create_data = assert_mcp_success(create_result, "Create schedule with data")
        entity_id = get_entity_id_from_response(create_data, "schedule")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("schedule", entity_id)
        logger.info(f"Created schedule with data: {entity_id}")

        # Verify the helper_data includes the data field in time blocks
        helper_data = create_data.get("helper_data", {})
        monday_blocks = helper_data.get("monday", [])
        assert len(monday_blocks) == 2, f"Expected 2 Monday blocks, got {len(monday_blocks)}"

        # Check that data field is preserved in the response
        first_block = monday_blocks[0]
        assert "data" in first_block, f"Missing 'data' in first block: {first_block}"
        assert first_block["data"].get("mode") == "comfort", (
            f"Expected mode='comfort', got: {first_block['data']}"
        )

        second_block = monday_blocks[1]
        assert "data" in second_block, f"Missing 'data' in second block: {second_block}"
        assert second_block["data"].get("mode") == "sleep", (
            f"Expected mode='sleep', got: {second_block['data']}"
        )
        logger.info("Schedule data field verified in creation response")

        # Wait for entity to be registered
        async def check_schedule_exists():
            result = await mcp_client.call_tool("ha_get_state", {"entity_id": entity_id})
            data = parse_mcp_result(result)
            if 'data' in data and data['data'] is not None:
                state = data.get("data", {}).get("state")
                return state in ["on", "off"]
            return False

        state_reached = await wait_for_condition(
            check_schedule_exists, timeout=10, condition_name=f"schedule {entity_id} registration"
        )
        assert state_reached, f"Schedule {entity_id} not registered within timeout"

        # If schedule is currently active (on), verify data attributes are exposed
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if 'data' in state_data and state_data['data'] is not None:
            entity_state = state_data["data"].get("state")
            attrs = state_data["data"].get("attributes", {})
            logger.info(f"Schedule state: {entity_state}, attributes: {attrs}")
            if entity_state == "on":
                # When active, the 'mode' from data should be an attribute
                assert "mode" in attrs, (
                    f"Expected 'mode' attribute when schedule is on: {attrs}"
                )
                logger.info(f"Schedule 'mode' attribute verified: {attrs['mode']}")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "schedule", "target": entity_id, "confirm": True},
        )
        logger.info("Schedule with data cleanup complete")

    async def test_schedule_update_with_data_field(self, mcp_client, cleanup_tracker):
        """Test updating a schedule preserves the data field on time blocks."""
        logger.info("Testing schedule update with data field")

        helper_name = "E2E Test Schedule Update"

        # CREATE schedule with initial data
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "schedule",
                "name": helper_name,
                "monday": [
                    {"from": "07:00", "to": "22:00", "data": {"mode": "comfort"}},
                    {"from": "22:00", "to": "23:59", "data": {"mode": "sleep"}},
                ],
            },
        )

        create_data = assert_mcp_success(create_result, "Create schedule for update test")
        entity_id = get_entity_id_from_response(create_data, "schedule")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("schedule", entity_id)
        logger.info(f"Created schedule for update: {entity_id}")

        # UPDATE schedule — change monday data field values
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "schedule",
                "name": helper_name,
                "helper_id": entity_id,
                "monday": [
                    {"from": "07:00", "to": "22:00", "data": {"mode": "away"}},
                    {"from": "22:00", "to": "23:59", "data": {"mode": "comfort"}},
                ],
            },
        )

        update_data = assert_mcp_success(update_result, "Update schedule with data")
        assert update_data.get("action") == "update", f"Expected update action: {update_data}"
        logger.info("Schedule update returned success")

        # VERIFY via list that data field was persisted
        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "schedule"},
        )
        list_data = assert_mcp_success(list_result, "List schedules after update")

        updated_helper = next(
            (h for h in list_data.get("helpers", []) if h.get("name") == helper_name),
            None,
        )
        assert updated_helper, "Updated schedule not found in list"

        monday_blocks = updated_helper.get("monday", [])
        assert len(monday_blocks) == 2, f"Expected 2 Monday blocks, got {len(monday_blocks)}"
        assert monday_blocks[0].get("data", {}).get("mode") == "away", (
            f"Expected mode='away' after update, got: {monday_blocks[0].get('data')}"
        )
        assert monday_blocks[1].get("data", {}).get("mode") == "comfort", (
            f"Expected mode='comfort' after update, got: {monday_blocks[1].get('data')}"
        )
        logger.info("Schedule update with data field verified via list")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "schedule", "target": entity_id, "confirm": True},
        )
        logger.info("Schedule update test cleanup complete")


@pytest.mark.asyncio
@pytest.mark.config
class TestZoneCRUD:
    """Test zone helper CRUD operations."""

    async def test_list_zones(self, mcp_client):
        """Test listing all zone helpers."""
        logger.info("Testing ha_config_list_helpers for zone")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "zone"},
        )

        data = assert_mcp_success(result, "List zone helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} zone helpers")

    async def test_zone_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete zone lifecycle with coordinates."""
        logger.info("Testing zone full lifecycle")

        helper_name = "E2E Test Zone"

        # CREATE zone with coordinates
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "zone",
                "name": helper_name,
                "icon": "mdi:map-marker",
                "latitude": 37.7749,
                "longitude": -122.4194,
                "radius": 150,
                "passive": False,
            },
        )

        create_data = assert_mcp_success(create_result, "Create zone")
        entity_id = get_entity_id_from_response(create_data, "zone")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("zone", entity_id)
        logger.info(f"Created zone: {entity_id}")

        # Wait for entity to be registered (zones start with state "0" - no people in zone)
        state_reached = await wait_for_entity_state(
            mcp_client, entity_id, "0", timeout=10
        )
        assert state_reached, f"Zone {entity_id} not registered within timeout"

        # VERIFY via state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            attrs = state_data.get("data", {}).get("attributes", {})
            logger.info(f"Zone attributes: {attrs}")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "zone", "target": entity_id, "confirm": True},
        )
        logger.info("Zone cleanup complete")

    async def test_zone_requires_coordinates(self, mcp_client):
        """Test that zone requires latitude and longitude (validated by HA)."""
        logger.info("Testing zone without coordinates (HA should reject)")

        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "zone",
                "name": "E2E No Coords Zone",
                # Missing required latitude/longitude - HA will validate
            },
        )
        assert data.get("success") is False, f"Should fail without coordinates: {data}"
        logger.info("HA properly validates required zone coordinates")

    async def test_zone_update_preserves_coordinates(self, mcp_client, cleanup_tracker):
        """Regression test: zone update must route to zone/update (not just entity registry).

        Before this fix, updating a zone via ha_config_set_helper only called
        config/entity_registry/update, which silently dropped latitude, longitude,
        radius, and passive — resetting them to HA defaults.
        """
        logger.info("Testing zone update preserves coordinates (regression test for config-store routing)")

        # CREATE zone with initial coordinates
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "zone",
                "name": "E2E Zone Update Test",
                "latitude": 40.7128,
                "longitude": -74.0060,
                "radius": 200,
                "passive": False,
            },
        )
        create_data = assert_mcp_success(create_result, "Create zone for update test")
        entity_id = get_entity_id_from_response(create_data, "zone")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("zone", entity_id)
        logger.info(f"Created zone: {entity_id}")

        state_reached = await wait_for_entity_state(mcp_client, entity_id, "0", timeout=10)
        assert state_reached, f"Zone {entity_id} not registered within timeout"

        # UPDATE with new coordinates
        # name is required by the tool schema; helper_id triggers update mode
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "zone",
                "helper_id": entity_id,
                "name": "E2E Zone Update Test",
                "latitude": 51.5074,
                "longitude": -0.1278,
                "radius": 500,
            },
        )
        update_data = assert_mcp_success(update_result, "Update zone coordinates")
        logger.info(f"Zone updated: {update_data.get('message')}")

        # VERIFY coordinates were persisted via entity state attributes
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        assert "data" in state_data and state_data["data"] is not None, (
            f"Zone entity not queryable after update: {state_data}"
        )
        attrs = state_data["data"].get("attributes", {})
        logger.info(f"Zone attributes after update: {attrs}")

        assert abs(attrs.get("latitude", 0) - 51.5074) < 0.001, (
            f"latitude not updated — got {attrs.get('latitude')}, expected ~51.5074. "
            "This indicates zone update is routing to entity registry only (regression)."
        )
        assert abs(attrs.get("longitude", 0) - (-0.1278)) < 0.001, (
            f"longitude not updated — got {attrs.get('longitude')}, expected ~-0.1278"
        )
        assert attrs.get("radius") == 500, (
            f"radius not updated — got {attrs.get('radius')}, expected 500"
        )
        logger.info("Zone coordinates verified after update ✓")

        # CLEANUP
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "zone", "target": entity_id, "confirm": True},
        )


@pytest.mark.asyncio
@pytest.mark.config
class TestPersonCRUD:
    """Test person helper CRUD operations."""

    async def test_list_persons(self, mcp_client):
        """Test listing all person helpers."""
        logger.info("Testing ha_config_list_helpers for person")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "person"},
        )

        data = assert_mcp_success(result, "List person helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} person helpers")

    async def test_person_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete person lifecycle."""
        logger.info("Testing person full lifecycle")

        helper_name = "E2E Test Person"

        # CREATE person (note: person doesn't support icon parameter)
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "person",
                "name": helper_name,
            },
        )

        create_data = assert_mcp_success(create_result, "Create person")
        entity_id = get_entity_id_from_response(create_data, "person")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("person", entity_id)
        logger.info(f"Created person: {entity_id}")

        # Wait for entity to be registered (person typically starts with "unknown" state)
        state_reached = await wait_for_entity_state(
            mcp_client, entity_id, "unknown", timeout=10
        )
        assert state_reached, f"Person {entity_id} not registered within timeout"

        # VERIFY via state
        state_result = await mcp_client.call_tool(
            "ha_get_state",
            {"entity_id": entity_id},
        )
        state_data = parse_mcp_result(state_result)
        if state_data.get("success"):
            state_value = state_data.get("data", {}).get("state")
            logger.info(f"Person state: {state_value}")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "person", "target": entity_id, "confirm": True},
        )
        logger.info("Person cleanup complete")

    async def test_person_update_preserves_config(self, mcp_client, cleanup_tracker):
        """Regression test: person update must route to person/update (not just entity registry).

        Before this fix, updating a person via ha_config_set_helper only called
        config/entity_registry/update, which silently dropped device_trackers,
        user_id, and picture. The person/update API is full-replace, so the old code
        effectively cleared all domain-specific fields on every update.
        """
        logger.info("Testing person update preserves config (regression test for config-store routing)")

        # CREATE person
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "person",
                "name": "E2E Person Update Test",
            },
        )
        create_data = assert_mcp_success(create_result, "Create person for update test")
        entity_id = get_entity_id_from_response(create_data, "person")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("person", entity_id)
        logger.info(f"Created person: {entity_id}")

        state_reached = await wait_for_entity_state(mcp_client, entity_id, "unknown", timeout=10)
        assert state_reached, f"Person {entity_id} not registered within timeout"

        # UPDATE with a name change — this exercises the full fetch-merge-update path
        # even without real device_trackers available in the test environment
        # helper_id triggers update mode (no "action" parameter needed)
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "person",
                "helper_id": entity_id,
                "name": "E2E Person Update Test Renamed",
            },
        )
        update_data = assert_mcp_success(update_result, "Update person name")
        logger.info(f"Person updated: {update_data.get('message')}")

        # VERIFY the update succeeded and returned person config (not entity registry entry)
        updated = update_data.get("updated_data", {})
        assert updated, f"No updated_data in response: {update_data}"
        # person/update response includes the person config with 'name' and 'id'
        assert updated.get("name") == "E2E Person Update Test Renamed", (
            f"Name not updated in config store response — got: {updated.get('name')}. "
            "This may indicate routing to entity registry only (regression)."
        )
        logger.info(f"Person config after update: name={updated.get('name')}, "
                    f"device_trackers={updated.get('device_trackers', [])}")

        # CLEANUP
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "person", "target": entity_id, "confirm": True},
        )
        logger.info("Person update test cleanup complete")


@pytest.mark.asyncio
@pytest.mark.config
class TestTagCRUD:
    """Test tag helper CRUD operations."""

    async def test_list_tags(self, mcp_client):
        """Test listing all tag helpers."""
        logger.info("Testing ha_config_list_helpers for tag")

        result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "tag"},
        )

        data = assert_mcp_success(result, "List tag helpers")
        assert "helpers" in data, f"Missing 'helpers': {data}"
        logger.info(f"Found {data.get('count', 0)} tag helpers")

    async def test_tag_full_lifecycle(self, mcp_client, cleanup_tracker):
        """Test complete tag lifecycle."""
        logger.info("Testing tag full lifecycle")

        helper_name = "E2E Test Tag"
        test_tag_id = "e2e-test-tag-001"

        # CREATE tag with custom ID
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "tag",
                "name": helper_name,
                "tag_id": test_tag_id,
                "description": "Test tag for E2E testing",
            },
        )

        create_data = assert_mcp_success(create_result, "Create tag")
        entity_id = get_entity_id_from_response(create_data, "tag")
        # Tag may not return entity_id in same format
        tag_id = create_data.get("helper_data", {}).get("id") or test_tag_id
        cleanup_tracker.track("tag", tag_id)
        logger.info(f"Created tag: {tag_id}")

        # LIST to verify tag appears (tags don't have entity state, list is authoritative)
        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "tag"},
        )
        list_data = assert_mcp_success(list_result, "List tags")
        logger.info(f"Tags after create: {list_data.get('count', 0)}")

        # DELETE
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "tag", "target": tag_id, "confirm": True},
        )
        logger.info("Tag cleanup complete")

    async def test_tag_update_preserves_description(self, mcp_client, cleanup_tracker):
        """Regression test: tag update must route to tag/update (not entity registry).

        Before this fix, updating a tag via ha_config_set_helper only called
        config/entity_registry/update, which silently dropped the description field.
        Tags don't have entity registry entries, so both name and description
        are sent to tag/update directly.
        """
        logger.info("Testing tag update preserves description (regression test for config-store routing)")

        test_tag_id = "e2e-tag-update-test-001"

        # CREATE tag with initial description
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "tag",
                "name": "E2E Tag Update Test",
                "tag_id": test_tag_id,
                "description": "Initial description",
            },
        )
        create_data = assert_mcp_success(create_result, "Create tag for update test")
        tag_id = create_data.get("helper_data", {}).get("id") or test_tag_id
        cleanup_tracker.track("tag", tag_id)
        logger.info(f"Created tag: {tag_id}")

        # UPDATE description and name via tag/update
        # helper_id triggers update mode (no "action" parameter needed)
        update_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "tag",
                "helper_id": tag_id,
                "name": "E2E Tag Update Test Renamed",
                "description": "Updated description",
            },
        )
        update_data = assert_mcp_success(update_result, "Update tag description and name")
        logger.info(f"Tag updated: {update_data.get('message')}")

        # VERIFY description was persisted by reading back via list
        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": "tag"},
        )
        list_data = assert_mcp_success(list_result, "List tags after update")

        updated_tag = None
        for tag in list_data.get("helpers", []):
            if tag.get("id") == tag_id:
                updated_tag = tag
                break

        assert updated_tag is not None, f"Tag {tag_id} not found in list after update"
        assert updated_tag.get("description") == "Updated description", (
            f"description not updated — got: {updated_tag.get('description')}. "
            "This indicates tag update is not routing to tag/update (regression)."
        )
        logger.info(f"Tag description verified after update: {updated_tag.get('description')} ✓")

        # CLEANUP
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"helper_type": "tag", "target": tag_id, "confirm": True},
        )
        logger.info("Tag update test cleanup complete")


@pytest.mark.asyncio
@pytest.mark.config
@pytest.mark.helper
class TestSetHelperNegativeInputs:
    """Negative-input tests for ha_config_set_helper pre-flight guards."""

    async def test_create_requires_name(self, mcp_client) -> None:
        """Rejects a create call when name is empty.

        Guard: tools_config_helpers.py — raises VALIDATION_INVALID_PARAMETER
        before any WebSocket I/O when action is "create" and name is falsy.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {"helper_type": "input_boolean", "name": ""},
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_input_number_invalid_range(self, mcp_client) -> None:
        """Rejects input_number when min_value > max_value.

        Guard: tools_config_helpers.py — raises VALIDATION_INVALID_PARAMETER
        when min_value is greater than max_value.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": "Invalid Range",
                "min_value": 100,
                "max_value": 0,
            },
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_input_datetime_both_date_and_time_false(
        self, mcp_client
    ) -> None:
        """Rejects input_datetime when both has_date and has_time are False.

        Guard: tools_config_helpers.py — raises VALIDATION_INVALID_PARAMETER
        when both fields are explicitly False.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_datetime",
                "name": "Invalid DateTime",
                "has_date": False,
                "has_time": False,
            },
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"

    async def test_input_select_requires_options(self, mcp_client) -> None:
        """Rejects input_select when options is absent.

        Guard: tools_config_helpers.py — raises VALIDATION_INVALID_PARAMETER
        before any WebSocket I/O when helper_type is "input_select" and
        options is falsy.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": "Missing Options",
            },
        )
        assert result["success"] is False
        assert result["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


@pytest.mark.asyncio
@pytest.mark.config
class TestHelperRegistryClear:
    """Test clearing area/labels on helpers by passing empty string / empty list (#1012).

    The consolidated ha_config_set_helper follows the same convention as
    ha_set_entity: None means 'not provided' (no change), empty string / empty
    list means 'explicit clear'. See test_entity_management.py::test_set_entity_clear_area
    for the entity-level analogue.
    """

    async def test_helper_clear_area_with_empty_string(
        self, mcp_client, cleanup_tracker
    ):
        """Setting area_id='' on an existing helper clears the area assignment."""
        logger.info("Testing helper area clear via empty string")

        # Create a dedicated area
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": "E2E Helper Clear Area"},
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        assert area_id, f"Missing area_id in response: {area_data}"
        cleanup_tracker.track("area", area_id)

        # Create helper assigned to that area
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Clear Area Helper",
                "area_id": area_id,
            },
        )
        data = assert_mcp_success(create_result, "Create helper with area")
        entity_id = get_entity_id_from_response(data, "input_boolean")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_boolean", entity_id)

        # Verify area was actually set on creation
        get_after_create = await mcp_client.call_tool(
            "ha_get_entity", {"entity_id": entity_id}
        )
        create_entry = assert_mcp_success(
            get_after_create, "Get entity after create"
        )
        assigned = create_entry.get("entity_entry", {}).get("area_id")
        assert assigned == area_id, (
            f"Area was not assigned on create: expected {area_id!r}, got {assigned!r}"
        )

        # Clear area using empty string. `name` is required by the tool schema
        # even on update; we pass the existing name as a no-op rename.
        clear_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": entity_id,
                "name": "E2E Clear Area Helper",
                "area_id": "",
            },
        )
        assert_mcp_success(clear_result, "Clear helper area")

        # Verify area is actually cleared (registry update does not round-trip
        # area_id back into the tool response, so we re-read from HA)
        get_after_clear = await mcp_client.call_tool(
            "ha_get_entity", {"entity_id": entity_id}
        )
        clear_entry = assert_mcp_success(get_after_clear, "Get entity after clear")
        cleared = clear_entry.get("entity_entry", {}).get("area_id")
        assert cleared is None, (
            f"Area was not cleared: expected None, got {cleared!r}"
        )

        logger.info("Helper area cleared successfully via empty string")

    async def test_helper_clear_labels_with_empty_list(
        self, mcp_client, cleanup_tracker
    ):
        """Setting labels=[] on an existing helper clears all labels."""
        logger.info("Testing helper labels clear via empty list")

        # Create a dedicated label
        label_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "E2E Helper Clear Label"},
        )
        label_data = assert_mcp_success(label_result, "Create test label")
        label_id = label_data.get("label_id")
        assert label_id, f"Missing label_id in response: {label_data}"
        cleanup_tracker.track("label", label_id)

        # Create helper assigned to that label
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Clear Labels Helper",
                "labels": [label_id],
            },
        )
        data = assert_mcp_success(create_result, "Create helper with labels")
        entity_id = get_entity_id_from_response(data, "input_boolean")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_boolean", entity_id)

        # Verify labels were actually set on creation
        get_after_create = await mcp_client.call_tool(
            "ha_get_entity", {"entity_id": entity_id}
        )
        create_entry = assert_mcp_success(
            get_after_create, "Get entity after create"
        )
        assigned_labels = create_entry.get("entity_entry", {}).get("labels") or []
        assert label_id in assigned_labels, (
            f"Label was not assigned on create: expected {label_id!r} in labels, got {assigned_labels!r}"
        )

        # Clear labels using empty list. `name` required by schema even on update.
        clear_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": entity_id,
                "name": "E2E Clear Labels Helper",
                "labels": [],
            },
        )
        assert_mcp_success(clear_result, "Clear helper labels")

        # Verify labels are actually cleared (registry update does not round-trip
        # labels back into the tool response, so we re-read from HA)
        get_after_clear = await mcp_client.call_tool(
            "ha_get_entity", {"entity_id": entity_id}
        )
        clear_entry = assert_mcp_success(get_after_clear, "Get entity after clear")
        cleared_labels = clear_entry.get("entity_entry", {}).get("labels") or []
        assert cleared_labels == [], (
            f"Labels were not cleared: expected [], got {cleared_labels!r}"
        )

        logger.info("Helper labels cleared successfully via empty list")

    @pytest.mark.slow
    async def test_flow_helper_clear_area_with_empty_string(
        self, mcp_client, cleanup_tracker
    ):
        """Clearing area on a FLOW helper (min_max) via area_id='' works.

        Covers the _handle_flow_helper branch of the fix (not the SIMPLE path
        tested above). Uses min_max because it's a single-step form flow with
        demo sensors guaranteed to exist in the test HA instance.
        """
        logger.info("Testing FLOW helper area clear via empty string")

        # Create a dedicated area
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": "E2E Flow Clear Area"},
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        assert area_id, f"Missing area_id in response: {area_data}"
        cleanup_tracker.track("area", area_id)

        # Create min_max helper with area assigned — FLOW path
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "min_max",
                "name": "E2E Flow Clear Area Helper",
                "config": {
                    "name": "E2E Flow Clear Area Helper",
                    "entity_ids": [
                        "sensor.demo_temperature",
                        "sensor.demo_outside_temperature",
                    ],
                    "type": "min",
                },
                "area_id": area_id,
            },
        )
        create_data = assert_mcp_success(create_result, "Create min_max with area")
        entry_id = create_data.get("entry_id")
        assert entry_id, f"Missing entry_id: {create_data}"

        entity_ids = create_data.get("entity_ids") or []
        assert entity_ids, f"Flow helper returned no entity_ids: {create_data}"
        target_entity = entity_ids[0]
        logger.info(f"Created flow helper entry={entry_id}, entity={target_entity}")

        try:
            # Verify area was applied to the flow-generated entity
            get_after_create = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": target_entity}
            )
            create_entry = assert_mcp_success(
                get_after_create, "Get flow entity after create"
            )
            assigned = create_entry.get("entity_entry", {}).get("area_id")
            assert assigned == area_id, (
                f"Area was not assigned on flow create: expected {area_id!r}, got {assigned!r}"
            )

            # Clear area using empty string on the same flow helper.
            # The options flow needs valid config to proceed, so we re-supply
            # the same entity_ids + type — the clear is driven purely by the
            # top-level area_id="" parameter, not by the config payload.
            # `name` is required by the tool schema (docstring notes it is
            # typically ignored on flow-based updates).
            clear_result = await mcp_client.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "helper_id": entry_id,
                    "name": "E2E Flow Clear Area Helper",
                    "config": {
                        "entity_ids": [
                            "sensor.demo_temperature",
                            "sensor.demo_outside_temperature",
                        ],
                        "type": "min",
                    },
                    "area_id": "",
                },
            )
            assert_mcp_success(clear_result, "Clear flow helper area")

            # Verify area is actually cleared on the entity
            get_after_clear = await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": target_entity}
            )
            clear_entry = assert_mcp_success(
                get_after_clear, "Get flow entity after clear"
            )
            cleared = clear_entry.get("entity_entry", {}).get("area_id")
            assert cleared is None, (
                f"Flow helper area was not cleared: expected None, got {cleared!r}"
            )

            logger.info("Flow helper area cleared successfully via empty string")
        finally:
            # Config-entry helpers are cleaned via ha_delete_helpers_integrations (not cleanup_tracker)
            await safe_call_tool(
                mcp_client,
                "ha_delete_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )

    async def test_helper_clear_area_and_labels_together(
        self, mcp_client, cleanup_tracker
    ):
        """Clearing area and labels in a single call: neither clear silently swallows the other.

        Targets the interaction between area_id and labels updates in
        _apply_registry_updates_to_entity — a single registry-update WS call
        carries both payloads, so a bug in one field could regress the other.
        """
        logger.info("Testing combined area+labels clear in one call")

        # Create area + label
        area_result = await mcp_client.call_tool(
            "ha_config_set_area", {"name": "E2E Combined Clear Area"}
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        assert area_id, f"Missing area_id: {area_data}"
        cleanup_tracker.track("area", area_id)

        label_result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "E2E Combined Clear Label"}
        )
        label_data = assert_mcp_success(label_result, "Create test label")
        label_id = label_data.get("label_id")
        assert label_id, f"Missing label_id: {label_data}"
        cleanup_tracker.track("label", label_id)

        # Create helper with both
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": "E2E Combined Clear Helper",
                "area_id": area_id,
                "labels": [label_id],
            },
        )
        data = assert_mcp_success(create_result, "Create helper with area+labels")
        entity_id = get_entity_id_from_response(data, "input_boolean")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_boolean", entity_id)

        # Verify both set
        before = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": entity_id}
            ),
            "Get entity before clear",
        )
        assert before.get("entity_entry", {}).get("area_id") == area_id
        assert label_id in (before.get("entity_entry", {}).get("labels") or [])

        # Clear both in a single call
        clear_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": entity_id,
                "name": "E2E Combined Clear Helper",
                "area_id": "",
                "labels": [],
            },
        )
        assert_mcp_success(clear_result, "Clear area+labels in one call")

        # Verify both cleared
        after = assert_mcp_success(
            await mcp_client.call_tool(
                "ha_get_entity", {"entity_id": entity_id}
            ),
            "Get entity after combined clear",
        )
        cleared_area = after.get("entity_entry", {}).get("area_id")
        cleared_labels = after.get("entity_entry", {}).get("labels") or []
        assert cleared_area is None, (
            f"Combined clear dropped area_id: expected None, got {cleared_area!r}"
        )
        assert cleared_labels == [], (
            f"Combined clear dropped labels: expected [], got {cleared_labels!r}"
        )

        logger.info("Combined area+labels clear in one call works correctly")

    @pytest.mark.slow
    async def test_flow_helper_clear_labels_with_empty_list(
        self, mcp_client, cleanup_tracker
    ):
        """Clearing labels on a FLOW helper (min_max) via labels=[] works.

        Symmetric to test_flow_helper_clear_area_with_empty_string but exercises
        the labels clear path through _handle_flow_helper.
        """
        logger.info("Testing FLOW helper labels clear via empty list")

        # Create label
        label_result = await mcp_client.call_tool(
            "ha_config_set_label", {"name": "E2E Flow Clear Label"}
        )
        label_data = assert_mcp_success(label_result, "Create test label")
        label_id = label_data.get("label_id")
        assert label_id, f"Missing label_id: {label_data}"
        cleanup_tracker.track("label", label_id)

        # Create min_max helper with label assigned — FLOW path
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "min_max",
                "name": "E2E Flow Clear Labels Helper",
                "config": {
                    "name": "E2E Flow Clear Labels Helper",
                    "entity_ids": [
                        "sensor.demo_temperature",
                        "sensor.demo_outside_temperature",
                    ],
                    "type": "min",
                },
                "labels": [label_id],
            },
        )
        create_data = assert_mcp_success(create_result, "Create min_max with labels")
        entry_id = create_data.get("entry_id")
        assert entry_id, f"Missing entry_id: {create_data}"
        entity_ids = create_data.get("entity_ids") or []
        assert entity_ids, f"Flow helper returned no entity_ids: {create_data}"
        target_entity = entity_ids[0]

        try:
            # Verify label assigned
            before = assert_mcp_success(
                await mcp_client.call_tool(
                    "ha_get_entity", {"entity_id": target_entity}
                ),
                "Get flow entity before clear",
            )
            assigned_labels = before.get("entity_entry", {}).get("labels") or []
            assert label_id in assigned_labels, (
                f"Label not assigned on flow create: expected {label_id!r} in {assigned_labels!r}"
            )

            # Clear labels on flow helper
            clear_result = await mcp_client.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "min_max",
                    "helper_id": entry_id,
                    "name": "E2E Flow Clear Labels Helper",
                    "config": {
                        "entity_ids": [
                            "sensor.demo_temperature",
                            "sensor.demo_outside_temperature",
                        ],
                        "type": "min",
                    },
                    "labels": [],
                },
            )
            assert_mcp_success(clear_result, "Clear flow helper labels")

            # Verify labels cleared
            after = assert_mcp_success(
                await mcp_client.call_tool(
                    "ha_get_entity", {"entity_id": target_entity}
                ),
                "Get flow entity after clear",
            )
            cleared_labels = after.get("entity_entry", {}).get("labels") or []
            assert cleared_labels == [], (
                f"Flow labels not cleared: expected [], got {cleared_labels!r}"
            )

            logger.info("Flow helper labels cleared successfully via empty list")
        finally:
            await safe_call_tool(
                mcp_client,
                "ha_delete_helpers_integrations",
                {"target": entry_id, "confirm": True},
            )


class TestMultiEntityFlowHelper:
    """Test that area_id / labels propagate to every entity of a multi-entity
    flow helper (e.g. utility_meter with tariffs produces 1 select + N sensors
    under a single config entry — see #1012).

    The other TestHelperRegistryClear tests cover single-entity flow helpers
    (min_max), which only exercise one iteration of the per-entity registry
    update loop. This class exercises the loop itself.
    """

    async def test_utility_meter_tariffs_area_and_labels_propagate_to_all_entities(
        self, mcp_client
    ):
        """utility_meter with 2 tariffs creates 3 entities; area_id and labels
        applied to all of them.
        """
        logger.info("Testing utility_meter multi-entity area/labels propagation")

        # Create a dedicated area + label
        area_result = await mcp_client.call_tool(
            "ha_config_set_area",
            {"name": "E2E UM Multi-Entity Area"},
        )
        area_data = assert_mcp_success(area_result, "Create test area")
        area_id = area_data.get("area_id")
        assert area_id, f"Missing area_id: {area_data}"

        label_result = await mcp_client.call_tool(
            "ha_config_set_label",
            {"name": "e2e_um_multi", "color": "blue"},
        )
        label_data = assert_mcp_success(label_result, "Create test label")
        label_id = label_data.get("label_id") or label_data.get("name")
        assert label_id, f"Missing label_id: {label_data}"

        entry_id = None
        try:
            create_result = await mcp_client.call_tool(
                "ha_config_set_helper",
                {
                    "helper_type": "utility_meter",
                    "name": "e2e_um_multi",
                    "config": {
                        # sensor.demo_temperature satisfies the sensor-domain
                        # selector; the utility_meter flow does not validate
                        # state_class at create time.
                        "source": "sensor.demo_temperature",
                        "cycle": "daily",
                        "offset": 0,
                        "tariffs": ["peak", "offpeak"],
                        "net_consumption": False,
                        "delta_values": False,
                        "periodically_resetting": True,
                    },
                    "area_id": area_id,
                    "labels": [label_id],
                },
            )
            create_data = assert_mcp_success(
                create_result, "Create utility_meter with 2 tariffs"
            )
            entry_id = create_data.get("entry_id")
            assert entry_id, f"Missing entry_id: {create_data}"

            entity_ids = create_data.get("entity_ids") or []
            # 2 tariffs → 1 select (tariff chooser) + 2 sensor (one per tariff) = 3
            assert len(entity_ids) == 3, (
                f"Expected 3 entities (1 select + 2 tariff sensors), got "
                f"{len(entity_ids)}: {entity_ids}"
            )
            logger.info(f"utility_meter multi-entity created: {entity_ids}")

            # Assert area_id propagated to every entity
            for eid in entity_ids:
                get_result = await mcp_client.call_tool(
                    "ha_get_entity", {"entity_id": eid}
                )
                get_data = assert_mcp_success(get_result, f"Get {eid}")
                entry = get_data.get("entity_entry", {})
                assigned_area = entry.get("area_id")
                assert assigned_area == area_id, (
                    f"Area not applied to {eid}: expected {area_id!r}, "
                    f"got {assigned_area!r}"
                )
                assigned_labels = entry.get("labels") or []
                assert label_id in assigned_labels, (
                    f"Label not applied to {eid}: expected {label_id!r} "
                    f"in {assigned_labels!r}"
                )

            logger.info("area_id and labels propagated to all 3 entities")
        finally:
            if entry_id:
                await safe_call_tool(
                    mcp_client,
                    "ha_delete_helpers_integrations",
                    {"target": entry_id, "confirm": True},
                )
            await safe_call_tool(
                mcp_client,
                "ha_config_remove_label",
                {"label_id": label_id},
            )
            await safe_call_tool(
                mcp_client,
                "ha_config_remove_area",
                {"area_id": area_id},
            )
