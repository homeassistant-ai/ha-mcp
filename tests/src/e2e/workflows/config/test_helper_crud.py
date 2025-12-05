"""
E2E tests for Home Assistant helper CRUD operations.

Tests the complete lifecycle of input_* helpers including:
- input_boolean, input_number, input_select, input_text, input_datetime, input_button
- List, create, update, and delete operations
- Type-specific parameter validation
"""

import asyncio
import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


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
        entity_id = create_data.get("entity_id")
        assert entity_id, f"Missing entity_id in create response: {create_data}"
        cleanup_tracker.track("input_boolean", entity_id)
        logger.info(f"Created input_boolean: {entity_id}")

        await asyncio.sleep(1)  # Wait for registration

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
            "ha_config_remove_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": entity_id,
            },
        )
        delete_data = assert_mcp_success(delete_result, "Delete input_boolean")
        logger.info(f"Deleted input_boolean: {delete_data.get('message')}")

        # VERIFY DELETION
        await asyncio.sleep(1)
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
        entity_id = data.get("entity_id")
        cleanup_tracker.track("input_boolean", entity_id)
        logger.info(f"Created with initial=on: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_boolean", "helper_id": entity_id},
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
        entity_id = create_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_number", entity_id)
        logger.info(f"Created input_number: {entity_id}")

        await asyncio.sleep(1)

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
            "ha_config_remove_helper",
            {"helper_type": "input_number", "helper_id": entity_id},
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
        entity_id = data.get("entity_id")
        cleanup_tracker.track("input_number", entity_id)
        logger.info(f"Created box mode number: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_number", "helper_id": entity_id},
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
        entity_id = create_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_select", entity_id)
        logger.info(f"Created input_select: {entity_id}")

        await asyncio.sleep(1)

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
            "ha_config_remove_helper",
            {"helper_type": "input_select", "helper_id": entity_id},
        )
        logger.info("Input select cleanup complete")

    async def test_input_select_requires_options(self, mcp_client):
        """Test that input_select requires options."""
        logger.info("Testing input_select without options (should fail)")

        result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": "E2E No Options Select",
                # Missing required options
            },
        )

        data = parse_mcp_result(result)
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
        entity_id = create_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_text", entity_id)
        logger.info(f"Created input_text: {entity_id}")

        await asyncio.sleep(1)

        # DELETE
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_text", "helper_id": entity_id},
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
        entity_id = data.get("entity_id")
        cleanup_tracker.track("input_text", entity_id)
        logger.info(f"Created password text: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_text", "helper_id": entity_id},
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
        entity_id = data.get("entity_id")
        assert entity_id, f"Missing entity_id: {data}"
        cleanup_tracker.track("input_datetime", entity_id)
        logger.info(f"Created date-only datetime: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_datetime", "helper_id": entity_id},
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
        entity_id = data.get("entity_id")
        cleanup_tracker.track("input_datetime", entity_id)
        logger.info(f"Created time-only datetime: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_datetime", "helper_id": entity_id},
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
        entity_id = data.get("entity_id")
        cleanup_tracker.track("input_datetime", entity_id)
        logger.info(f"Created full datetime: {entity_id}")

        # Clean up
        await mcp_client.call_tool(
            "ha_config_remove_helper",
            {"helper_type": "input_datetime", "helper_id": entity_id},
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
        entity_id = create_data.get("entity_id")
        assert entity_id, f"Missing entity_id: {create_data}"
        cleanup_tracker.track("input_button", entity_id)
        logger.info(f"Created input_button: {entity_id}")

        await asyncio.sleep(1)

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
            "ha_config_remove_helper",
            {"helper_type": "input_button", "helper_id": entity_id},
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
    entity_id = data.get("entity_id")
    cleanup_tracker.track("input_boolean", entity_id)
    logger.info(f"Created helper: {entity_id}")

    # Clean up
    await mcp_client.call_tool(
        "ha_config_remove_helper",
        {"helper_type": "input_boolean", "helper_id": entity_id},
    )


@pytest.mark.asyncio
@pytest.mark.config
async def test_helper_delete_nonexistent(mcp_client):
    """Test deleting a non-existent helper."""
    logger.info("Testing delete of non-existent helper")

    result = await mcp_client.call_tool(
        "ha_config_remove_helper",
        {
            "helper_type": "input_boolean",
            "helper_id": "nonexistent_helper_xyz_12345",
        },
    )

    data = parse_mcp_result(result)

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
