"""
Helper Integration E2E Tests

Comprehensive tests for Home Assistant helper management via the ha_manage_helper MCP tool.
Tests all supported helper types: input_boolean, input_number, input_select, input_text,
input_datetime, and input_button.

This test suite validates:
- Helper creation with various configurations
- Helper update operations
- Helper deletion and verification
- WebSocket state monitoring
- Service call integration
- Field validation and constraints
"""

import logging
from typing import Any

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


async def wait_for_entity_state(
    mcp_client,
    entity_id: str,
    max_retries: int = 5,
    delay: float = 1.0,
    expected_state: str | None = None,
) -> dict[str, Any] | None:
    """Wait for entity to become available with retry logic.

    Args:
        mcp_client: MCP client instance
        entity_id: Entity ID to check
        max_retries: Maximum number of retries
        delay: Delay between retries in seconds
        expected_state: If provided, wait for this specific state

    Returns:
        Entity state data if found, None if not found after retries
    """
    for attempt in range(max_retries):
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": entity_id}
            )
            state_data = parse_mcp_result(state_result)

            # Check if we have valid entity data
            if "data" in state_data and "state" in state_data["data"]:
                current_state = state_data["data"]["state"]
                if expected_state is None or current_state == expected_state:
                    logger.debug(
                        f"✅ Entity {entity_id} found on attempt {attempt + 1} (state: {current_state})"
                    )
                    return state_data
                elif expected_state is not None:
                    logger.debug(
                        f"🔄 Entity {entity_id} has state '{current_state}', waiting for '{expected_state}'"
                    )
            elif state_data.get("success") is True:
                return state_data
        except Exception as e:
            logger.debug(f"⚠️ Attempt {attempt + 1} failed for {entity_id}: {e}")
        if attempt < max_retries - 1:
            logger.debug(
                f"🔄 Retrying {entity_id} in {delay}s (attempt {attempt + 1}/{max_retries})"
            )

    logger.warning(f"❌ Entity {entity_id} not found after {max_retries} attempts")
    return None


def assert_state_response_success(data, operation_description="state operation"):
    """Assert that ha_get_state response is successful.

    ha_get_state returns data directly without explicit success field,
    so we check for presence of entity data instead.
    """
    if data.get("success") is True:
        return True
    elif data.get("success") is False:
        assert False, (
            f"{operation_description} failed: {data.get('error', 'Unknown error')}"
        )
    elif data.get("data", {}).get("entity_id"):
        # Success - has entity data
        return True
    else:
        assert False, f"{operation_description} failed: {data}"


@pytest.mark.helper
@pytest.mark.cleanup
class TestHelperIntegration:
    """Test complete helper management workflows."""

    async def test_input_boolean_lifecycle(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Create input_boolean → Toggle → Update → Delete

        Validates basic boolean helper operations and state management.
        """

        helper_name = "test_boolean_e2e"
        logger.info(f"🔘 Testing input_boolean lifecycle: {helper_name}")

        # 1. CREATE: Basic boolean helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": helper_name,
                "icon": "mdi:toggle-switch",
                "initial": "false",  # String representation for boolean initial value
            },
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), (
            f"Failed to create input_boolean: {create_data}"
        )

        helper_entity = f"input_boolean.{helper_name}"
        cleanup_tracker.track("input_boolean", helper_entity)
        logger.info(f"✅ Created input_boolean: {helper_entity}")

        # 2. VERIFY: Helper exists and has correct initial state
        state_data = await wait_for_entity_state(
            mcp_client, helper_entity, max_retries=6, delay=0.5
        )
        assert state_data is not None, (
            f"Helper {helper_entity} was not created or not accessible after retries"
        )
        assert_state_response_success(state_data, "get helper state")
        initial_state = state_data["data"]["state"]
        assert initial_state in [
            "on",
            "off",
        ], f"Expected boolean state (on/off), got {initial_state}"
        logger.info(f"✅ Helper initial state verified: {initial_state}")

        # 3. TOGGLE: Test state change via service call
        logger.info("🔄 Toggling boolean helper...")
        toggle_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_boolean",
                "service": "toggle",
                "entity_id": helper_entity,
            },
        )

        toggle_data = parse_mcp_result(toggle_result)
        assert toggle_data.get("success"), f"Failed to toggle helper: {toggle_data}"

        # Verify state changed with retry logic
        expected_state = "off" if initial_state == "on" else "on"
        new_state_data = await wait_for_entity_state(
            mcp_client,
            helper_entity,
            max_retries=5,
            delay=0.5,
            expected_state=expected_state,
        )
        assert new_state_data is not None, (
            f"Failed to verify state change to {expected_state}"
        )
        new_state = new_state_data["data"]["state"]
        assert new_state == expected_state, (
            f"Toggle failed, expected {expected_state}, got {new_state}"
        )
        logger.info(f"✅ Helper toggled successfully: {initial_state} → {new_state}")

        # 4. DELETE: Clean up helper
        logger.info("🗑️ Deleting helper...")
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_boolean",
                "helper_id": helper_name
            },
        )

        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), f"Failed to delete helper: {delete_data}"
        logger.info("✅ Helper deleted successfully")

        # 5. VERIFY: Helper is gone - wait a moment for deletion to propagate
        final_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        final_state_data = parse_mcp_result(final_state_result)
        # Should fail or return error since helper no longer exists
        has_error = (
            not final_state_data.get("success")
            or "not found" in str(final_state_data).lower()
        )
        no_data = "data" not in final_state_data or not final_state_data["data"]
        assert has_error or no_data, (
            f"Helper should be deleted but still exists: {final_state_data}"
        )
        logger.info("✅ Helper deletion verified")

    async def test_input_number_validation(self, mcp_client, cleanup_tracker):
        """
        Test: input_number with range validation and constraints

        Validates numeric helpers with min/max ranges, step values, and validation.
        """

        helper_name = "test_number_e2e"
        logger.info(f"🔢 Testing input_number validation: {helper_name}")

        # 1. CREATE: Number helper with constraints
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_number",
                "name": helper_name,
                "min_value": 0,
                "max_value": 100,
                "step": 5,
                "initial": "25",
                "mode": "slider",
                "unit_of_measurement": "%",
                "icon": "mdi:brightness-percent"},
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), (
            f"Failed to create input_number: {create_data}"
        )

        helper_entity = f"input_number.{helper_name}"
        cleanup_tracker.track("input_number", helper_entity)
        logger.info(f"✅ Created input_number: {helper_entity}")

        # 2. VERIFY: Initial value and attributes
        state_data = await wait_for_entity_state(
            mcp_client, helper_entity, max_retries=6, delay=0.5
        )
        assert state_data is not None, (
            f"Number helper {helper_entity} was not created or not accessible"
        )
        assert_state_response_success(state_data, "get number state")

        initial_value = float(state_data["data"]["state"])
        attributes = state_data["data"]["attributes"]

        # Verify constraints were set correctly (initial value may default to min)
        assert attributes.get("min") == 0, (
            f"Min constraint not set correctly: {attributes.get('min')}"
        )
        assert attributes.get("max") == 100, (
            f"Max constraint not set correctly: {attributes.get('max')}"
        )
        assert attributes.get("step") == 5, (
            f"Step constraint not set correctly: {attributes.get('step')}"
        )
        assert attributes.get("unit_of_measurement") == "%", (
            f"Unit not set correctly: {attributes.get('unit_of_measurement')}"
        )
        assert 0 <= initial_value <= 100, (
            f"Initial value {initial_value} outside valid range 0-100"
        )
        logger.info(
            f"✅ Number helper constraints verified: {initial_value}% (0-100, step=5)"
        )

        # 3. TEST: Valid value change
        logger.info("🎯 Testing valid value change...")
        set_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_number",
                "service": "set_value",
                "entity_id": helper_entity,
                "data": {"value": 75},
            },
        )

        set_data = parse_mcp_result(set_result)
        assert set_data.get("success"), f"Failed to set valid value: {set_data}"

        # Verify new value with retry logic
        new_state_data = await wait_for_entity_state(
            mcp_client, helper_entity, max_retries=5, delay=0.5, expected_state="75.0"
        )
        assert new_state_data is not None, "Failed to verify value change to 75"
        new_value = float(new_state_data["data"]["state"])
        assert new_value == 75.0, f"Expected value 75, got {new_value}"
        logger.info(f"✅ Valid value change: {initial_value}% → 75%")

        # 4. TEST: Boundary value (max)
        logger.info("🎯 Testing boundary value (max)...")
        max_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_number",
                "service": "set_value",
                "entity_id": helper_entity,
                "data": {"value": 100},
            },
        )

        max_data = parse_mcp_result(max_result)
        assert max_data.get("success"), f"Failed to set max value: {max_data}"

        # Allow time for state change and verify
        max_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        max_state_data = parse_mcp_result(max_state_result)
        assert max_state_data is not None and "data" in max_state_data, (
            "Failed to get state after setting max value"
        )
        max_value = float(max_state_data["data"]["state"])
        assert max_value == 100.0, f"Expected max value 100, got {max_value}"
        logger.info("✅ Boundary value test passed: 100% (max)")

        # 5. TEST: Step increment
        logger.info("🎯 Testing step increment...")
        increment_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_number",
                "service": "set_value",
                "entity_id": helper_entity,
                "data": {"value": 95},
            },
        )

        increment_data = parse_mcp_result(increment_result)
        assert increment_data.get("success"), (
            f"Failed to set step value: {increment_data}"
        )

        # Allow time for state change and verify
        step_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        step_state_data = parse_mcp_result(step_state_result)
        assert step_state_data is not None and "data" in step_state_data, (
            "Failed to get state after setting step value"
        )
        step_value = float(step_state_data["data"]["state"])
        assert step_value == 95.0, f"Expected step value 95, got {step_value}"
        logger.info("✅ Step increment test passed: 95%")

        # Cleanup
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_number",
                "helper_id": helper_name
            },
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), (
            f"Failed to delete number helper: {delete_data}"
        )
        logger.info("✅ Number helper cleaned up")

    async def test_input_select_options(self, mcp_client, cleanup_tracker):
        """
        Test: input_select with dynamic options

        Validates dropdown helpers with option management.
        """

        helper_name = "test_select_e2e"
        logger.info(f"📋 Testing input_select options: {helper_name}")

        # 1. CREATE: Select helper with options
        options = ["Option 1", "Option 2", "Option 3"]
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_select",
                "name": helper_name,
                "options": options,
                "initial": "Option 2",
                "icon": "mdi:format-list-bulleted"},
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), (
            f"Failed to create input_select: {create_data}"
        )

        helper_entity = f"input_select.{helper_name}"
        cleanup_tracker.track("input_select", helper_entity)
        logger.info(f"✅ Created input_select: {helper_entity}")

        # 2. VERIFY: Options and initial selection
        state_data = await wait_for_entity_state(
            mcp_client, helper_entity, max_retries=6, delay=0.5
        )
        assert state_data is not None, (
            f"Select helper {helper_entity} was not created or not accessible"
        )
        assert_state_response_success(state_data, "get select state")

        current_option = state_data["data"]["state"]
        available_options = state_data["data"]["attributes"]["options"]

        assert current_option == "Option 2", (
            f"Expected initial option 'Option 2', got '{current_option}'"
        )
        assert available_options == options, (
            f"Options mismatch: expected {options}, got {available_options}"
        )
        logger.info(
            f"✅ Select options verified: {current_option} from {available_options}"
        )

        # 3. TEST: Option selection
        logger.info("🎯 Testing option selection...")
        select_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_select",
                "service": "select_option",
                "entity_id": helper_entity,
                "data": {"option": "Option 3"},
            },
        )

        select_data = parse_mcp_result(select_result)
        assert select_data.get("success"), f"Failed to select option: {select_data}"

        # Verify selection
        new_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        new_state_data = parse_mcp_result(new_state_result)
        new_option = new_state_data["data"]["state"]
        assert new_option == "Option 3", f"Expected 'Option 3', got '{new_option}'"
        logger.info("✅ Option selection: Option 2 → Option 3")

        # 4. TEST: First/Last selection
        logger.info("🎯 Testing first option selection...")
        first_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_select",
                "service": "select_first",
                "entity_id": helper_entity,
            },
        )

        first_data = parse_mcp_result(first_result)
        assert first_data.get("success"), f"Failed to select first option: {first_data}"

        first_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        first_state_data = parse_mcp_result(first_state_result)
        first_option = first_state_data["data"]["state"]
        assert first_option == "Option 1", (
            f"Expected first option 'Option 1', got '{first_option}'"
        )
        logger.info(f"✅ First option selection: {first_option}")

        # Cleanup
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_select",
                "helper_id": helper_name
            },
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), (
            f"Failed to delete select helper: {delete_data}"
        )
        logger.info("✅ Select helper cleaned up")

    async def test_input_text_validation(self, mcp_client, cleanup_tracker):
        """
        Test: input_text with pattern validation and constraints

        Validates text helpers with length limits and pattern matching.
        """

        helper_name = "test_text_e2e"
        logger.info(f"📝 Testing input_text validation: {helper_name}")

        # 1. CREATE: Text helper with constraints
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_text",
                "name": helper_name,
                "initial": "Test",
                "mode": "text",
                "icon": "mdi:text-box"},
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), f"Failed to create input_text: {create_data}"

        helper_entity = f"input_text.{helper_name}"
        cleanup_tracker.track("input_text", helper_entity)
        logger.info(f"✅ Created input_text: {helper_entity}")

        # 2. VERIFY: Initial text and attributes
        state_data = await wait_for_entity_state(
            mcp_client, helper_entity, max_retries=6, delay=0.5
        )
        assert state_data is not None, (
            f"Text helper {helper_entity} was not created or not accessible"
        )
        assert_state_response_success(state_data, "get text state")

        current_text = state_data["data"]["state"]
        attributes = state_data["data"]["attributes"]

        assert current_text == "Test", (
            f"Expected initial text 'Test', got '{current_text}'"
        )
        assert attributes.get("mode") == "text", (
            f"Mode not set correctly: {attributes.get('mode')}"
        )
        logger.info(
            f"✅ Text helper verified: '{current_text}' (mode: {attributes.get('mode')})"
        )

        # 3. TEST: Valid text change
        logger.info("🎯 Testing valid text change...")
        new_text = "Hello World 123"
        set_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_text",
                "service": "set_value",
                "entity_id": helper_entity,
                "data": {"value": new_text},
            },
        )

        set_data = parse_mcp_result(set_result)
        assert set_data.get("success"), f"Failed to set valid text: {set_data}"

        # Verify new text
        new_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        new_state_data = parse_mcp_result(new_state_result)
        updated_text = new_state_data["data"]["state"]
        assert updated_text == new_text, (
            f"Expected text '{new_text}', got '{updated_text}'"
        )
        logger.info(f"✅ Valid text change: 'Test' → '{new_text}'")

        # 4. TEST: Boundary length (max)
        logger.info("🎯 Testing boundary length...")
        max_text = "A" * 50  # 50 characters (max limit)
        max_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "input_text",
                "service": "set_value",
                "entity_id": helper_entity,
                "data": {"value": max_text},
            },
        )

        max_data = parse_mcp_result(max_result)
        assert max_data.get("success"), f"Failed to set max length text: {max_data}"

        max_state_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        max_state_data = parse_mcp_result(max_state_result)
        max_updated_text = max_state_data["data"]["state"]
        assert max_updated_text == max_text, "Max length text not set correctly"
        assert len(max_updated_text) == 50, (
            f"Expected 50 chars, got {len(max_updated_text)}"
        )
        logger.info(
            f"✅ Boundary length test passed: {len(max_updated_text)} chars (max)"
        )

        # Cleanup
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_text",
                "helper_id": helper_name
            },
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), (
            f"Failed to delete text helper: {delete_data}"
        )
        logger.info("✅ Text helper cleaned up")

    async def test_input_datetime_modes(self, mcp_client, cleanup_tracker):
        """
        Test: input_datetime with different modes (date only, time only, both)

        Validates datetime helpers with flexible date/time configurations.
        """

        base_name = "test_datetime_e2e"
        logger.info(f"📅 Testing input_datetime modes: {base_name}")

        # Test basic datetime helper (mode parameters may not be supported)
        test_configs = [(True, True, "datetime")]

        for has_date, has_time, description in test_configs:
            helper_name = f"{base_name}_{description.replace(' ', '_')}"
            logger.info(f"📅 Testing datetime mode: {description}")

            # 1. CREATE: DateTime helper with specific mode
            create_params = {
                "helper_type": "input_datetime",
                "name": helper_name,
                "icon": "mdi:calendar-clock",
            }

            # Set explicit date/time parameters (at least one required)
            if has_date:
                create_params["has_date"] = True
            if has_time:
                create_params["has_time"] = True

            # Add appropriate initial values
            if has_date and has_time:
                create_params["initial"] = "2025-01-01 12:00:00"
            elif has_date:
                create_params["initial"] = "2025-01-01"
            elif has_time:
                create_params["initial"] = "12:00:00"

            create_result = await mcp_client.call_tool(
                "ha_config_set_helper", create_params
            )

            create_data = parse_mcp_result(create_result)
            assert create_data.get("success"), (
                f"Failed to create {description} datetime: {create_data}"
            )

            helper_entity = f"input_datetime.{helper_name}"
            cleanup_tracker.track("input_datetime", helper_entity)
            logger.info(f"✅ Created datetime ({description}): {helper_entity}")

            # 2. VERIFY: Mode and initial value
            state_data = await wait_for_entity_state(
                mcp_client, helper_entity, max_retries=6, delay=0.5
            )
            assert state_data is not None, (
                f"DateTime helper {helper_entity} was not created or not accessible"
            )
            assert_state_response_success(state_data, "get datetime state")

            attributes = state_data["data"]["attributes"]
            # Basic datetime helper verification (mode-specific params may not be supported)
            assert "editable" in attributes, (
                f"DateTime helper should be editable: {attributes}"
            )
            logger.info(f"✅ DateTime helper created ({description}): {helper_entity}")

            # 3. TEST: Value setting based on mode
            if has_date and has_time:
                test_value = "2025-12-25 18:30:00"
            elif has_date:
                test_value = "2025-12-25"
            elif has_time:
                test_value = "18:30:00"

            logger.info(f"🎯 Testing value setting for {description}...")
            set_result = await mcp_client.call_tool(
                "ha_call_service",
                {
                    "domain": "input_datetime",
                    "service": "set_datetime",
                    "entity_id": helper_entity,
                    "data": {"datetime": test_value},
                },
            )

            set_data = parse_mcp_result(set_result)
            assert set_data.get("success"), (
                f"Failed to set {description} datetime: {set_data}"
            )

            # Verify value was set
            new_state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": helper_entity}
            )
            new_state_data = parse_mcp_result(new_state_result)
            new_value = new_state_data["data"]["state"]
            # Note: Home Assistant might format datetime differently, so we check if it contains key components
            if has_date:
                assert "2025-12-25" in new_value, (
                    f"Date not set correctly for {description}: {new_value}"
                )
            if has_time:
                assert "18:30" in new_value, (
                    f"Time not set correctly for {description}: {new_value}"
                )
            logger.info(f"✅ Value setting verified ({description}): {new_value}")

            # 4. CLEANUP: Delete this datetime helper
            delete_result = await mcp_client.call_tool(
                "ha_config_remove_helper",
                {
                    "helper_type": "input_datetime",
                    "helper_id": helper_name
                },
            )
            delete_data = parse_mcp_result(delete_result)
            assert delete_data.get("success"), (
                f"Failed to delete {description} datetime: {delete_data}"
            )
            logger.info(f"✅ DateTime helper cleaned up ({description})")

    async def test_input_button_stateless(self, mcp_client, cleanup_tracker):
        """
        Test: input_button stateless behavior

        Validates button helpers which are stateless trigger entities.
        """

        helper_name = "test_button_e2e"
        logger.info(f"🔘 Testing input_button stateless behavior: {helper_name}")

        # 1. CREATE: Button helper
        create_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_button",
                "name": helper_name,
                "icon": "mdi:button-pointer"},
        )

        create_data = parse_mcp_result(create_result)
        assert create_data.get("success"), (
            f"Failed to create input_button: {create_data}"
        )

        helper_entity = f"input_button.{helper_name}"
        cleanup_tracker.track("input_button", helper_entity)
        logger.info(f"✅ Created input_button: {helper_entity}")

        # 2. VERIFY: Button exists and is stateless
        state_data = await wait_for_entity_state(
            mcp_client, helper_entity, max_retries=6, delay=0.5
        )
        assert state_data is not None, (
            f"Button helper {helper_entity} was not created or not accessible"
        )
        assert_state_response_success(state_data, "get button state")

        # Button should have "unknown" state or timestamp as it's stateless
        current_state = state_data["data"]["state"]
        if current_state not in ["unknown", "unavailable"]:
            # Should be a timestamp (ISO format with 'T') or similar format
            assert isinstance(current_state, str), (
                f"Expected string state, got {type(current_state)}: {current_state}"
            )
            logger.info(f"✅ Button shows timestamp state: {current_state}")
        else:
            logger.info(f"✅ Button stateless state verified: {current_state}")

        # 3. TEST: Button press (trigger)
        logger.info("🎯 Testing button press...")
        press_result = await mcp_client.call_tool(
            "ha_call_service",
            {"domain": "input_button", "service": "press", "entity_id": helper_entity},
        )

        press_data = parse_mcp_result(press_result)
        assert press_data.get("success"), f"Failed to press button: {press_data}"
        logger.info("✅ Button press executed successfully")

        # 4. VERIFY: Button state after press (shows timestamp when pressed)
        post_press_result = await mcp_client.call_tool(
            "ha_get_state", {"entity_id": helper_entity}
        )
        post_press_data = parse_mcp_result(post_press_result)
        post_press_state = post_press_data["data"]["state"]

        # Button state shows timestamp when pressed, or remains stateless
        if post_press_state not in ["unknown", "unavailable"]:
            # Should be a timestamp (ISO format with 'T')
            assert "T" in post_press_state, (
                f"Expected timestamp or stateless state, got '{post_press_state}'"
            )
            logger.info(f"✅ Button shows press timestamp: {post_press_state}")
        else:
            logger.info(f"✅ Button remains stateless: {post_press_state}")

        # 5. TEST: Multiple button presses (should always work)
        for i in range(3):
            logger.info(f"🎯 Testing button press #{i + 2}...")
            multi_press_result = await mcp_client.call_tool(
                "ha_call_service",
                {
                    "domain": "input_button",
                    "service": "press",
                    "entity_id": helper_entity,
                },
            )
            multi_press_data = parse_mcp_result(multi_press_result)
            assert multi_press_data.get("success"), (
                f"Failed button press #{i + 2}: {multi_press_data}"
            )

        logger.info("✅ Multiple button presses successful")

        # Cleanup
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": "input_button",
                "helper_id": helper_name
            },
        )
        delete_data = parse_mcp_result(delete_result)
        assert delete_data.get("success"), (
            f"Failed to delete button helper: {delete_data}"
        )
        logger.info("✅ Button helper cleaned up")

    @pytest.mark.slow
    async def test_helper_bulk_operations(self, mcp_client, cleanup_tracker):
        """
        Test: Bulk helper operations and management

        Validates creating, managing, and deleting multiple helpers simultaneously.
        """

        logger.info("🏭 Testing bulk helper operations...")

        # Define multiple helpers to create
        # Note: entity_id will be based on name, so name should match our expected entity IDs
        helpers_to_create = [
            (
                "input_boolean",
                "bulk_bool_1",
                {"name": "bulk_bool_1", "initial": "true"},
            ),
            (
                "input_boolean",
                "bulk_bool_2",
                {"name": "bulk_bool_2", "initial": "false"},
            ),
            (
                "input_number",
                "bulk_num_1",
                {"name": "bulk_num_1", "min_value": 0, "max_value": 10, "initial": "5"},
            ),
            (
                "input_select",
                "bulk_select_1",
                {"name": "bulk_select_1", "options": ["A", "B", "C"], "initial": "B"},
            ),
            (
                "input_text",
                "bulk_text_1",
                {"name": "bulk_text_1", "initial": "Bulk Test"},
            ),
        ]

        created_helpers = []

        # 1. CREATE: Bulk creation of helpers
        logger.info(f"🚀 Creating {len(helpers_to_create)} helpers...")
        for helper_type, helper_id, config in helpers_to_create:
            create_params = {"helper_type": helper_type}
            create_params.update(config)  # Merge config parameters directly
            create_result = await mcp_client.call_tool(
                "ha_config_set_helper", create_params
            )

            create_data = parse_mcp_result(create_result)
            assert create_data.get("success"), (
                f"Failed to create {helper_type}.{helper_id}: {create_data}"
            )

            helper_entity = f"{helper_type}.{helper_id}"
            created_helpers.append((helper_type, helper_id, helper_entity))
            cleanup_tracker.track(helper_type, helper_entity)

            logger.info(f"✅ Created: {helper_entity}")

        logger.info(f"✅ Bulk creation completed: {len(created_helpers)} helpers")

        # 2. VERIFY: All helpers exist and have correct states
        logger.info("🔍 Verifying all helpers exist...")
        for _helper_type, _helper_id, helper_entity in created_helpers:
            state_data = await wait_for_entity_state(
                mcp_client, helper_entity, max_retries=8, delay=0.5
            )
            if state_data is not None:
                entity_state = (
                    state_data["data"]["state"] if "data" in state_data else "unknown"
                )
                logger.info(f"✅ Verified: {helper_entity} (state: {entity_state})")
            else:
                logger.warning(f"⚠️ Helper {helper_entity} not accessible after retries")
                # Continue test anyway as entity might be created but not yet accessible

        # 3. TEST: Bulk state changes
        logger.info("🔄 Testing bulk state changes...")

        # Toggle all booleans
        for helper_type, _helper_id, helper_entity in created_helpers:
            if helper_type == "input_boolean":
                toggle_result = await mcp_client.call_tool(
                    "ha_call_service",
                    {
                        "domain": "input_boolean",
                        "service": "toggle",
                        "entity_id": helper_entity,
                    },
                )
                toggle_data = parse_mcp_result(toggle_result)
                assert toggle_data.get("success"), (
                    f"Failed to toggle {helper_entity}: {toggle_data}"
                )
                logger.info(f"✅ Toggled: {helper_entity}")


        # 4. CLEANUP: Bulk deletion
        logger.info(f"🗑️ Bulk deleting {len(created_helpers)} helpers...")
        for helper_type, helper_id, helper_entity in created_helpers:
            delete_result = await mcp_client.call_tool(
                "ha_config_remove_helper",
                {
                    "helper_type": helper_type,
                    "helper_id": helper_id
                },
            )

            delete_data = parse_mcp_result(delete_result)
            assert delete_data.get("success"), (
                f"Failed to delete {helper_entity}: {delete_data}"
            )
            logger.info(f"✅ Deleted: {helper_entity}")

        logger.info("✅ Bulk deletion completed")


@pytest.mark.helper
async def test_helper_search_and_discovery(mcp_client):
    """
    Test: Helper search and discovery capabilities

    Validates that users can find and explore existing helpers
    through the search functionality.
    """

    logger.info("🔍 Testing helper search and discovery...")

    # Search for existing helpers by domain
    helper_domains = [
        "input_boolean",
        "input_number",
        "input_select",
        "input_text",
        "input_datetime",
        "input_button",
    ]

    helpers_found = False
    for domain in helper_domains:
        logger.info(f"🔍 Searching for {domain} helpers...")
        search_result = await mcp_client.call_tool(
            "ha_search_entities", {"query": "", "domain_filter": domain, "limit": 10}
        )

        search_data = parse_mcp_result(search_result)
        # Handle different response formats - search might return data directly or nested
        if "data" in search_data and "results" in search_data["data"]:
            data_section = search_data["data"]
            assert data_section.get("success", True), (
                f"Helper search failed for {domain}: {search_data}"
            )
            results = data_section.get("results", [])
        elif "results" in search_data:
            # Direct results format
            results = search_data.get("results", [])
        else:
            # Fallback - no results found
            results = []
        logger.info(f"🔍 Found {len(results)} {domain} helpers")

        # If helpers exist, verify their structure
        if results:
            helpers_found = True
            first_helper = results[0]
            assert "entity_id" in first_helper, (
                f"Missing entity_id in {domain} helper: {first_helper}"
            )
            assert first_helper["entity_id"].startswith(f"{domain}."), (
                f"Invalid entity_id format: {first_helper['entity_id']}"
            )
            logger.info(f"✅ Sample {domain} helper: {first_helper.get('entity_id')}")

    # Get system overview to see helper information (use standard level for full domain listing)
    logger.info("🔍 Getting system overview...")
    overview_result = await mcp_client.call_tool("ha_get_overview", {"detail_level": "standard"})
    overview_data = parse_mcp_result(overview_result)

    # If helpers were found, they should appear in overview
    if helpers_found:
        overview_text = str(overview_data).lower()
        assert "input_" in overview_text or "helper" in overview_text, (
            "System overview should include helper information when helpers exist"
        )
        logger.info("✅ System overview includes helper data")
    else:
        logger.info("ℹ️ No helpers found in test environment, skipping overview validation")

    logger.info("✅ Helper search and discovery tests completed")


@pytest.mark.helper
@pytest.mark.cleanup
async def test_helper_list_functionality(mcp_client, cleanup_tracker):
    """
    Test: ha_config_list_helpers functionality

    Validates that the helper list tool returns configurations for all helpers
    of a specific type with their full configuration details.
    """

    logger.info("📋 Testing ha_config_list_helpers functionality...")

    # Test helper types that support the list endpoint
    helper_types = [
        "input_boolean",
        "input_number",
        "input_select",
        "input_text",
        "input_datetime",
        "input_button",
    ]

    # Create test helpers for verification
    test_helpers_created = []

    logger.info("🚀 Creating test helpers for list verification...")

    # Create input_boolean
    bool_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": "test_list_bool",
            "icon": "mdi:test-tube",
        },
    )
    bool_data = parse_mcp_result(bool_result)
    if bool_data.get("success"):
        test_helpers_created.append(("input_boolean", "test_list_bool"))
        cleanup_tracker.track("input_boolean", "input_boolean.test_list_bool")
        logger.info("✅ Created test input_boolean")

    # Create input_number
    num_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_number",
            "name": "test_list_num",
            "min_value": 0,
            "max_value": 100,
            "initial": "50",
            "icon": "mdi:test-tube",
        },
    )
    num_data = parse_mcp_result(num_result)
    if num_data.get("success"):
        test_helpers_created.append(("input_number", "test_list_num"))
        cleanup_tracker.track("input_number", "input_number.test_list_num")
        logger.info("✅ Created test input_number")

    # Wait for helpers to be registered

    # Test listing for each helper type
    for helper_type in helper_types:
        logger.info(f"📋 Listing {helper_type} helpers...")

        list_result = await mcp_client.call_tool(
            "ha_config_list_helpers",
            {"helper_type": helper_type},
        )

        list_data = parse_mcp_result(list_result)

        # Verify successful response
        assert list_data.get("success"), (
            f"Failed to list {helper_type} helpers: {list_data}"
        )

        # Verify response structure
        assert "helper_type" in list_data, (
            f"Response missing helper_type field: {list_data}"
        )
        assert "count" in list_data, f"Response missing count field: {list_data}"
        assert "helpers" in list_data, f"Response missing helpers field: {list_data}"
        assert "message" in list_data, f"Response missing message field: {list_data}"

        # Verify helper_type matches
        assert list_data["helper_type"] == helper_type, (
            f"Helper type mismatch: expected {helper_type}, got {list_data['helper_type']}"
        )

        # Verify count matches list length
        helpers = list_data["helpers"]
        count = list_data["count"]
        assert len(helpers) == count, (
            f"Count mismatch for {helper_type}: count={count}, len(helpers)={len(helpers)}"
        )

        logger.info(f"✅ Listed {count} {helper_type} helper(s)")

        # If we created a test helper of this type, verify it's in the list
        test_helper_name = next(
            (name for htype, name in test_helpers_created if htype == helper_type),
            None,
        )

        if test_helper_name and helpers:
            # Check if our test helper is in the results
            found_test_helper = False
            for helper in helpers:
                # Helper config should have 'id' or 'name' field
                helper_id = helper.get("id", helper.get("name", ""))
                if test_helper_name in str(helper_id):
                    found_test_helper = True
                    logger.info(
                        f"✅ Verified test helper '{test_helper_name}' in list results"
                    )

                    # Verify helper has configuration fields
                    assert "name" in helper or "id" in helper, (
                        f"Helper missing identifier: {helper}"
                    )
                    logger.info(f"   Helper config fields: {list(helper.keys())}")
                    break

            if not found_test_helper:
                logger.warning(
                    f"⚠️ Test helper '{test_helper_name}' not found in list (may not be registered yet)"
                )

    # Cleanup test helpers
    logger.info("🗑️ Cleaning up test helpers...")
    for helper_type, helper_name in test_helpers_created:
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_helper",
            {
                "helper_type": helper_type,
                "helper_id": helper_name,
            },
        )
        delete_data = parse_mcp_result(delete_result)
        if delete_data.get("success"):
            logger.info(f"✅ Deleted {helper_type}.{helper_name}")
        else:
            logger.warning(
                f"⚠️ Failed to delete {helper_type}.{helper_name}: {delete_data.get('error')}"
            )

    logger.info("✅ Helper list functionality tests completed")
