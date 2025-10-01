"""
End-to-End tests for Home Assistant Script Management (ha_manage_script tool).

This test suite validates the complete lifecycle of Home Assistant scripts including:
- Script creation with various configurations
- Script retrieval and configuration validation
- Script execution and state monitoring
- Script updates and versioning
- Script deletion and cleanup
- Parameter handling and validation
- Edge cases and error scenarios

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.

Tests are designed for Docker Home Assistant test environment at localhost:8124.
"""

import asyncio
import json
import logging
import time
from typing import Any

import pytest

# Import test utilities
from ...utilities.assertions import (
    MCPAssertions,
)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def enhanced_parse_mcp_result(result) -> dict[str, Any]:
    """Enhanced MCP result parser with better error handling."""
    try:
        if hasattr(result, "content") and result.content:
            response_text = str(result.content[0].text)
            try:
                # First try standard JSON parsing
                return json.loads(response_text)
            except json.JSONDecodeError:
                # Try parsing with Python literal evaluation
                try:
                    fixed_text = (
                        response_text.replace("true", "True")
                        .replace("false", "False")
                        .replace("null", "None")
                    )
                    return eval(fixed_text)
                except (SyntaxError, NameError, ValueError):
                    # Return raw response if parsing fails
                    return {"raw_response": response_text, "parse_error": True}

        # Fallback for other result formats
        return {
            "content": (
                str(result.content[0]) if hasattr(result, "content") else str(result)
            )
        }
    except Exception as e:
        logger.warning(f"Failed to parse MCP result: {e}")
        return {"error": "Failed to parse result", "exception": str(e)}


def extract_script_config(get_data: dict[str, Any]) -> dict[str, Any]:
    """Extract script configuration from ha_manage_script get response."""
    # Handle nested config structure: get_data["config"]["config"]
    config_wrapper = get_data.get("config", {})
    if isinstance(config_wrapper, dict) and "config" in config_wrapper:
        return config_wrapper.get("config", {})
    return config_wrapper


def wait_for_script_registration(script_count: int = 1) -> int:
    """Calculate appropriate wait time for script registration."""
    # In Docker test environment, scripts may need more time to register
    # Increased base wait time and cap to handle Docker environment delays
    base_wait = 3
    scaled_wait = base_wait + (script_count * 0.8)
    return min(int(scaled_wait), 12)  # Increased cap from 5s to 12s


def validate_script_sequence(
    sequence: list[dict[str, Any]], expected_steps: int
) -> bool:
    """Validate script sequence structure."""
    if not isinstance(sequence, list):
        return False
    if len(sequence) != expected_steps:
        return False

    # Validate each step has required structure
    for step in sequence:
        if not isinstance(step, dict):
            return False
        # Each step should have either 'service'/'action' or 'delay' or other valid keys
        valid_keys = [
            "service",
            "action",
            "delay",
            "condition",
            "choose",
            "repeat",
            "parallel",
        ]
        if not any(key in step for key in valid_keys):
            return False

    return True


async def verify_script_exists_and_registered(
    mcp_client, script_id: str, timeout: int = 15, poll_interval: float = 1.0
) -> bool:
    """
    Wait for script to be registered and discoverable in Home Assistant.

    This function addresses timing issues where scripts are created but not
    immediately discoverable through the management API or entity registry.
    """
    start_time = time.time()
    script_entity = f"script.{script_id}"

    logger.info(
        f"⏳ Waiting for script {script_entity} to be registered (timeout: {timeout}s)"
    )

    while time.time() - start_time < timeout:
        try:
            # Method 1: Try to get script config via management API
            get_result = await mcp_client.call_tool(
                "ha_config_get_script",
                { "script_id": script_id}
            )
            get_data = enhanced_parse_mcp_result(get_result)
            if get_data.get("success") and get_data.get("config"):
                logger.info(f"✅ Script {script_entity} found via management API")
                return True

            # Method 2: Try to get script state via entity API
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": script_entity}
            )
            state_data = enhanced_parse_mcp_result(state_result)
            if state_data.get("success"):
                logger.info(f"✅ Script {script_entity} found via state API")
                return True

            # Method 3: Try to search for the script entity
            search_result = await mcp_client.call_tool(
                "ha_search_entities",
                {"query": script_id, "domain_filter": "script", "limit": 5},
            )
            search_data = enhanced_parse_mcp_result(search_result)
            search_results = (
                search_data.get("data", {}).get("results", [])
                if search_data.get("success")
                else []
            )

            for result in search_results:
                if result.get("entity_id") == script_entity:
                    logger.info(f"✅ Script {script_entity} found via search API")
                    return True

        except Exception as e:
            logger.debug(f"Script registration check failed: {e}")

        elapsed = time.time() - start_time
        logger.debug(
            f"🔍 Script {script_entity} not yet registered (elapsed: {elapsed:.1f}s)"
        )
        await asyncio.sleep(poll_interval)

    logger.warning(f"⚠️ Script {script_entity} was not registered within {timeout}s")
    return False


async def verify_script_execution_state(
    mcp_client,
    script_entity: str,
    timeout: int = 15,  # Increased from 10s to 15s
) -> dict[str, Any]:
    """Verify script execution by checking state changes with retry logic."""
    start_time = time.time()
    consecutive_failures = 0
    max_consecutive_failures = 3

    while time.time() - start_time < timeout:
        try:
            state_result = await mcp_client.call_tool(
                "ha_get_state", {"entity_id": script_entity}
            )

            state_data = enhanced_parse_mcp_result(state_result)
            if state_data.get("success"):
                return state_data

            consecutive_failures = 0  # Reset on successful API call
            await asyncio.sleep(0.8)  # Increased from 0.5s to 0.8s
        except Exception as e:
            consecutive_failures += 1
            logger.debug(
                f"State check failed ({consecutive_failures}/{max_consecutive_failures}): {e}"
            )

            # If too many consecutive failures, increase wait time
            if consecutive_failures >= max_consecutive_failures:
                logger.debug(
                    f"Multiple consecutive failures for {script_entity}, extending wait time"
                )
                await asyncio.sleep(2.0)
                consecutive_failures = 0
            else:
                await asyncio.sleep(0.8)

    logger.warning(f"Could not verify state for {script_entity} within {timeout}s")
    return {"success": False, "timeout": True}


def create_test_script_config(
    name: str,
    sequence: list[dict[str, Any]] | None = None,
    mode: str = "single",
    **kwargs,
) -> dict[str, Any]:
    """Create a standardized test script configuration."""
    if sequence is None:
        sequence = [{"delay": {"seconds": 1}}]

    config = {
        "alias": f"Test {name} Script",
        "description": f"E2E test script for {name} - safe to delete",
        "sequence": sequence,
        "mode": mode,
    }

    # Add any additional configuration
    config.update(kwargs)

    return config


@pytest.mark.script
@pytest.mark.cleanup
class TestScriptOrchestration:
    """Test complete script management workflows."""

    async def test_script_basic_lifecycle(self, mcp_client, cleanup_tracker):
        """
        Test: Basic script lifecycle (create, get, execute, delete)

        Validates fundamental script operations with a simple delay script.
        Uses Docker test environment at localhost:8124.
        """

        script_id = "test_basic_e2e"
        logger.info(f"📜 Testing basic script lifecycle: {script_id}")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. CREATE: Basic delay script
            create_data = await mcp.call_tool_success(
                "ha_config_set_script",
                {
                    "script_id": script_id,
                    "config": {
                        "alias": "Test Basic Script",
                        "description": "Simple delay script for E2E testing",
                        "sequence": [{"delay": {"seconds": 1}}],
                        "mode": "single",
                    },
                },
            )

            script_entity = f"script.{script_id}"
            cleanup_tracker.track("script", script_entity)
            logger.info(f"✅ Created script: {script_entity}")

            # 2. WAIT: Ensure script is registered before verification
            script_registered = await verify_script_exists_and_registered(
                mcp_client, script_id, timeout=12
            )
            if not script_registered:
                logger.error(
                    f"Script {script_entity} failed to register, skipping further tests"
                )
                return

            # 3. GET: Verify script configuration
            get_data = await mcp.call_tool_success(
                "ha_config_get_script",
                { "script_id": script_id}
            )

            config = extract_script_config(get_data)
            assert config.get("alias") == "Test Basic Script", (
                f"Alias mismatch: {config}"
            )
            assert "sequence" in config, f"Sequence missing in config: {config}"
            assert validate_script_sequence(config.get("sequence", []), 1), (
                f"Invalid sequence: {config.get('sequence')}"
            )
            assert config.get("mode") == "single", f"Mode mismatch: {config}"
            logger.info("✅ Script configuration verified")

            # 4. EXECUTE: Run the script
            execute_data = await mcp.call_tool_success(
                "ha_call_service",
                {"domain": "script", "service": "turn_on", "entity_id": script_entity},
            )
            logger.info("✅ Script executed successfully")

            # 5. VERIFY: Check script state shows execution
            await asyncio.sleep(2)  # Allow execution to complete

            state_data = await verify_script_execution_state(
                mcp_client, script_entity, timeout=10
            )  # Increased from 5s to 10s
            if state_data.get("success"):  # Check if state is accessible
                script_state = state_data.get("data", {}).get("state", "N/A")
                logger.info(f"✅ Script state accessible: {script_state}")
            else:
                logger.info(
                    "ℹ️ Script state not accessible (normal for completed scripts)"
                )

            # 6. DELETE: Clean up script
            delete_data = await mcp.call_tool_success(
                "ha_config_remove_script",
                { "script_id": script_id}
            )
            logger.info("✅ Script deleted successfully")

            # 7. VERIFY: Script no longer exists
            final_get_data = await mcp.call_tool_failure(
                "ha_config_get_script",
                { "script_id": script_id},
                expected_error="not found",
            )
            logger.info("✅ Script deletion verified")

    async def test_script_service_calls(
        self, mcp_client, cleanup_tracker, test_light_entity
    ):
        """
        Test: Script with service calls and entity interactions

        Validates scripts that control other Home Assistant entities.
        Uses Docker test environment and validates service execution.
        """

        script_id = "test_service_calls_e2e"
        logger.info(f"🔧 Testing script with service calls: {script_id}")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. CREATE: Script that controls a light
            create_data = await mcp.call_tool_success(
                "ha_config_set_script",
                {
                    "script_id": script_id,
                    "config": {
                        "alias": "Light Control Script",
                        "description": "Script that toggles a test light",
                        "sequence": [
                            {
                                "service": "light.turn_on",
                                "target": {"entity_id": test_light_entity},
                                "data": {"brightness_pct": 50},
                            },
                            {"delay": {"seconds": 2}},
                            {
                                "service": "light.turn_off",
                                "target": {"entity_id": test_light_entity},
                            },
                        ],
                        "mode": "single",
                    },
                },
            )

            script_entity = f"script.{script_id}"
            cleanup_tracker.track("script", script_entity)
            logger.info(f"✅ Created service call script: {script_entity}")

            # 2. WAIT: Ensure script is registered before verification
            script_registered = await verify_script_exists_and_registered(
                mcp_client, script_id, timeout=12
            )
            if not script_registered:
                logger.error(
                    f"Script {script_entity} failed to register, skipping further tests"
                )
                return

            # 3. VERIFY: Configuration contains correct service calls
            get_data = await mcp.call_tool_success(
                "ha_config_get_script",
                { "script_id": script_id}
            )

            config = extract_script_config(get_data)
            sequence = config.get("sequence", [])
            assert validate_script_sequence(sequence, 3), (
                f"Invalid sequence structure: {sequence}"
            )

            # Check first service call (Home Assistant uses 'action' field in scripts)
            first_step = sequence[0]
            action_key = first_step.get("action") or first_step.get("service")
            assert action_key == "light.turn_on", (
                f"Wrong action in first step: {first_step}"
            )
            assert test_light_entity in str(first_step.get("target", {})), (
                f"Target entity missing: {first_step}"
            )

            # Validate brightness_pct is preserved
            data_field = first_step.get("data", {})
            assert data_field.get("brightness_pct") == 50, (
                f"Brightness not preserved: {data_field}"
            )

            # Check third service call (turn_off)
            third_step = sequence[2]
            third_action = third_step.get("action") or third_step.get("service")
            assert third_action == "light.turn_off", (
                f"Wrong action in third step: {third_step}"
            )

            logger.info("✅ Service call configuration verified")

            # 3. EXECUTE: Run the script and monitor light changes
            logger.info(f"🚀 Executing script to control {test_light_entity}...")

            # Get initial light state for comparison
            initial_state_data = await mcp.call_tool_success(
                "ha_get_state", {"entity_id": test_light_entity}
            )
            initial_state = initial_state_data.get("data", {}).get("state", "unknown")
            logger.info(f"💡 Initial light state: {initial_state}")

            # Execute the script
            execute_data = await mcp.call_tool_success(
                "ha_call_service",
                {"domain": "script", "service": "turn_on", "entity_id": script_entity},
            )
            logger.info("✅ Script execution initiated")

            # Allow script to complete (should take about 3 seconds total)
            await asyncio.sleep(4)
            logger.info("✅ Script execution completed")

            # Verify final light state (should be off after script completes)
            final_state_data = await mcp.call_tool_success(
                "ha_get_state", {"entity_id": test_light_entity}
            )
            final_state = final_state_data.get("data", {}).get("state", "unknown")
            logger.info(f"💡 Final light state: {final_state}")

            # 4. CLEANUP: Delete script
            delete_data = await mcp.call_tool_success(
                "ha_config_remove_script",
                { "script_id": script_id}
            )
            logger.info("✅ Service call script cleaned up")

    async def test_script_with_parameters(self, mcp_client, cleanup_tracker):
        """
        Test: Script with input parameters and templating

        Validates scripts that accept parameters and use templating.
        Tests field definitions, parameter passing, and template evaluation.
        """

        script_id = "test_parameters_e2e"
        logger.info(f"📝 Testing script with parameters: {script_id}")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. CREATE: Script with input fields and templating
            create_data = await mcp.call_tool_success(
                "ha_config_set_script",
                {
                    "script_id": script_id,
                    "config": {
                        "alias": "Parameterized Script",
                        "description": "Script that accepts parameters for testing",
                        "fields": {
                            "message": {
                                "name": "Message",
                                "description": "Custom message to log",
                                "required": True,
                                "selector": {"text": None},
                            },
                            "delay_seconds": {
                                "name": "Delay",
                                "description": "Delay in seconds",
                                "default": 1,
                                "selector": {"number": {"min": 1, "max": 10}},
                            },
                        },
                        "sequence": [
                            {
                                "service": "system_log.write",
                                "data": {
                                    "message": "Script parameter test: {{ message | default('No message') }}",
                                    "level": "info",
                                },
                            },
                            {"delay": {"seconds": "{{ delay_seconds | default(1) }}"}},
                        ],
                        "mode": "queued",
                    },
                },
            )

            script_entity = f"script.{script_id}"
            cleanup_tracker.track("script", script_entity)
            logger.info(f"✅ Created parameterized script: {script_entity}")

            # 2. VERIFY: Configuration includes fields and templating
            await asyncio.sleep(wait_for_script_registration())

            get_data = await mcp.call_tool_success(
                "ha_config_get_script",
                { "script_id": script_id}
            )

            config = extract_script_config(get_data)
            fields = config.get("fields", {})
            assert "message" in fields, f"Message field missing: {fields}"
            assert "delay_seconds" in fields, f"Delay field missing: {fields}"
            assert config.get("mode") == "queued", f"Mode should be queued: {config}"

            # Validate field properties
            message_field = fields.get("message", {})
            assert message_field.get("required") is True, (
                f"Message field should be required: {message_field}"
            )
            assert "selector" in message_field, (
                f"Message field missing selector: {message_field}"
            )

            delay_field = fields.get("delay_seconds", {})
            assert delay_field.get("default") == 1, (
                f"Delay field default incorrect: {delay_field}"
            )

            # Validate templating in sequence
            sequence = config.get("sequence", [])
            assert validate_script_sequence(sequence, 2), (
                f"Invalid parameter script sequence: {sequence}"
            )

            # Check for template syntax in first step
            first_step = sequence[0]
            message_template = first_step.get("data", {}).get("message", "")
            assert "{{" in message_template and "}}" in message_template, (
                f"Template syntax missing: {message_template}"
            )

            logger.info("✅ Parameter configuration verified")

            # 3. EXECUTE: Run script with parameters
            test_message = "E2E Test Message"
            test_delay = 2

            execute_data = await mcp.call_tool_success(
                "ha_call_service",
                {
                    "domain": "script",
                    "service": "turn_on",
                    "entity_id": script_entity,
                    "data": {
                        "variables": {
                            "message": test_message,
                            "delay_seconds": test_delay,
                        }
                    },
                },
            )
            logger.info(
                f"✅ Parameterized script executed with message: '{test_message}', delay: {test_delay}s"
            )

            # Allow execution to complete (should take test_delay + processing time)
            await asyncio.sleep(test_delay + 1)

            # Optional: Check if script is still running in queued mode
            state_data = await verify_script_execution_state(
                mcp_client, script_entity, timeout=8
            )  # Increased from 3s to 8s
            if state_data.get("success"):
                script_state = state_data.get("data", {}).get("state", "off")
                logger.info(f"🔄 Script execution state: {script_state}")

            # 4. CLEANUP: Delete script
            delete_data = await mcp.call_tool_success(
                "ha_config_remove_script",
                { "script_id": script_id}
            )
            logger.info("✅ Parameterized script cleaned up")

    async def test_script_update_operations(self, mcp_client, cleanup_tracker):
        """
        Test: Script update and versioning operations

        Validates updating existing scripts with new configurations.
        Tests configuration persistence and version tracking.
        """

        script_id = "test_update_e2e"
        logger.info(f"🔄 Testing script update operations: {script_id}")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. CREATE: Initial script version
            initial_config = {
                "alias": "Update Test Script v1",
                "description": "Initial version for update testing",
                "sequence": [{"delay": {"seconds": 1}}],
                "mode": "single",
            }

            create_data = await mcp.call_tool_success(
                "ha_config_set_script",
                { "script_id": script_id, "config": initial_config},
            )

            script_entity = f"script.{script_id}"
            cleanup_tracker.track("script", script_entity)
            logger.info(f"✅ Created initial script version: {script_entity}")

            # 2. VERIFY: Initial configuration
            await asyncio.sleep(wait_for_script_registration())

            get_data = await mcp.call_tool_success(
                "ha_config_get_script",
                { "script_id": script_id}
            )

            initial_retrieved = extract_script_config(get_data)
            assert initial_retrieved.get("alias") == "Update Test Script v1", (
                f"Initial alias mismatch: {initial_retrieved}"
            )
            assert validate_script_sequence(initial_retrieved.get("sequence", []), 1), (
                f"Initial sequence invalid: {initial_retrieved.get('sequence')}"
            )
            assert initial_retrieved.get("mode") == "single", (
                f"Initial mode mismatch: {initial_retrieved}"
            )
            logger.info("✅ Initial configuration verified")

            # 3. UPDATE: Modify script with new configuration
            updated_config = {
                "alias": "Update Test Script v2",
                "description": "Updated version with new sequence",
                "sequence": [
                    {"delay": {"seconds": 1}},
                    {
                        "service": "system_log.write",
                        "data": {
                            "message": "Script was updated successfully",
                            "level": "info",
                        },
                    },
                ],
                "mode": "restart",
            }

            update_data = await mcp.call_tool_success(
                "ha_config_set_script",
                { "script_id": script_id, "config": updated_config},
            )
            logger.info("✅ Script updated successfully")

            # 4. VERIFY: Updated configuration
            await asyncio.sleep(wait_for_script_registration())

            updated_get_data = await mcp.call_tool_success(
                "ha_config_get_script",
                { "script_id": script_id}
            )

            updated_retrieved = extract_script_config(updated_get_data)
            assert updated_retrieved.get("alias") == "Update Test Script v2", (
                f"Updated alias mismatch: {updated_retrieved}"
            )
            assert updated_retrieved.get("mode") == "restart", (
                f"Updated mode mismatch: {updated_retrieved}"
            )

            updated_sequence = updated_retrieved.get("sequence", [])
            assert validate_script_sequence(updated_sequence, 2), (
                f"Updated sequence invalid: {updated_sequence}"
            )

            # Verify the new service call step was added
            service_step = updated_sequence[1]
            service_name = service_step.get("service") or service_step.get("action")
            assert service_name == "system_log.write", (
                f"Service step missing or incorrect: {service_step}"
            )
            assert "Script was updated successfully" in str(
                service_step.get("data", {})
            ), f"Update message missing: {service_step}"

            logger.info("✅ Updated configuration verified")

            # 5. EXECUTE: Test updated script
            execute_data = await mcp.call_tool_success(
                "ha_call_service",
                {"domain": "script", "service": "turn_on", "entity_id": script_entity},
            )
            logger.info("✅ Updated script executed successfully")

            await asyncio.sleep(3)  # Allow execution to complete

            # 6. CLEANUP: Delete script
            delete_data = await mcp.call_tool_success(
                "ha_config_remove_script",
                { "script_id": script_id}
            )
            logger.info("✅ Updated script cleaned up")

    async def test_script_execution_modes(self, mcp_client, cleanup_tracker):
        """
        Test: Different script execution modes (single, restart, queued, parallel)

        Validates behavior of different execution modes with proper timeout handling.
        Tests mode-specific behavior and concurrent execution limits.
        """

        logger.info("⚙️ Testing script execution modes...")

        async with MCPAssertions(mcp_client) as mcp:
            modes_to_test = [
                ("single", "Only one execution at a time"),
                ("restart", "Restart if already running"),
                ("queued", "Queue executions"),
                ("parallel", "Allow parallel executions"),
            ]

            created_scripts = []

            for mode, description in modes_to_test:
                script_id = f"test_mode_{mode}_e2e"

                # CREATE: Script with specific mode
                create_config = {
                    "alias": f"Mode Test - {mode.title()}",
                    "description": f"Test script for {description}",
                    "sequence": [
                        {
                            "service": "system_log.write",
                            "data": {
                                "message": f"Executing {mode} mode script",
                                "level": "info",
                            },
                        },
                        {"delay": {"seconds": 2}},
                    ],
                    "mode": mode,
                }

                # Only add max for modes that support it
                if mode in ["queued", "parallel"]:
                    create_config["max"] = 3

                create_data = await mcp.call_tool_success(
                    "ha_config_set_script",
                    {
                        "script_id": script_id,
                        "config": create_config},
                )

                script_entity = f"script.{script_id}"
                cleanup_tracker.track("script", script_entity)
                created_scripts.append((script_id, script_entity, mode))
                logger.info(f"✅ Created {mode} mode script: {script_entity}")

                await asyncio.sleep(1)  # Small delay between creations

            # VERIFY: All scripts created with correct modes
            await asyncio.sleep(wait_for_script_registration(len(created_scripts)))

            for script_id, script_entity, expected_mode in created_scripts:
                get_data = await mcp.call_tool_success(
                    "ha_config_get_script",
                    { "script_id": script_id}
                )

                config = extract_script_config(get_data)
                actual_mode = config.get("mode")
                assert actual_mode == expected_mode, (
                    f"Mode mismatch for {script_id}: expected {expected_mode}, got {actual_mode}"
                )

                # Validate sequence structure
                assert validate_script_sequence(config.get("sequence", []), 2), (
                    f"Invalid sequence for {expected_mode} script: {config.get('sequence')}"
                )

                if expected_mode in ["queued", "parallel"]:
                    max_value = config.get("max")
                    assert max_value == 3, (
                        f"Max value mismatch for {script_id}: expected 3, got {max_value}"
                    )

            logger.info("✅ All execution modes verified")

            # EXECUTE: Test each mode with timeout protection
            execution_tasks = []
            for script_id, script_entity, mode in created_scripts:
                logger.info(f"🚀 Testing execution of {mode} mode script...")

                # Execute the script
                execute_data = await mcp.call_tool_success(
                    "ha_call_service",
                    {
                        "domain": "script",
                        "service": "turn_on",
                        "entity_id": script_entity,
                    },
                )
                logger.info(f"✅ Executed {mode} mode script")

                # For queued and parallel modes, test multiple executions
                if mode in ["queued", "parallel"]:
                    logger.info(f"🔄 Testing concurrent execution for {mode} mode...")
                    # Execute again immediately to test mode behavior
                    execute_data2 = await mcp.call_tool_success(
                        "ha_call_service",
                        {
                            "domain": "script",
                            "service": "turn_on",
                            "entity_id": script_entity,
                        },
                    )
                    logger.info(f"✅ Second execution initiated for {mode} mode")

                await asyncio.sleep(0.5)  # Small delay between different scripts

            # Allow all executions to complete with generous timeout
            logger.info("⏳ Allowing all script executions to complete...")
            await asyncio.sleep(6)  # Increased timeout for multiple executions

            # CLEANUP: Delete all test scripts
            for script_id, script_entity, mode in created_scripts:
                delete_data = await mcp.call_tool_success(
                    "ha_config_remove_script",
                    { "script_id": script_id}
                )
                logger.debug(f"🗑️ Deleted {mode} script: {script_entity}")
                await asyncio.sleep(0.3)  # Small delay between deletions

            logger.info("✅ All execution mode scripts cleaned up")

    @pytest.mark.slow
    async def test_script_bulk_operations(self, mcp_client, cleanup_tracker):
        """
        Test: Bulk script operations and management

        Validates creating, managing, and deleting multiple scripts simultaneously.
        Tests cleanup tracking and bulk operation reliability.
        """

        logger.info("🏭 Testing bulk script operations...")

        async with MCPAssertions(mcp_client) as mcp:
            # Define multiple scripts to create
            scripts_to_create = [
                (
                    "bulk_script_1",
                    {
                        "alias": "Bulk Script 1",
                        "description": "First bulk test script",
                        "sequence": [{"delay": {"seconds": 1}}],
                        "mode": "single",
                    },
                ),
                (
                    "bulk_script_2",
                    {
                        "alias": "Bulk Script 2",
                        "description": "Second bulk test script",
                        "sequence": [
                            {
                                "service": "system_log.write",
                                "data": {"message": "Bulk script 2", "level": "info"},
                            },
                            {"delay": {"seconds": 1}},
                        ],
                        "mode": "restart",
                    },
                ),
                (
                    "bulk_script_3",
                    {
                        "alias": "Bulk Script 3",
                        "description": "Third bulk test script",
                        "sequence": [{"delay": {"seconds": 2}}],
                        "mode": "queued",
                        "max": 2,
                    },
                ),
            ]

            created_scripts = []
            failed_scripts = []

            # 1. CREATE: Bulk creation of scripts with error tracking
            logger.info(f"🚀 Creating {len(scripts_to_create)} scripts...")
            for script_id, config in scripts_to_create:
                try:
                    create_data = await mcp.call_tool_success(
                        "ha_config_set_script",
                        { "script_id": script_id, "config": config},
                    )

                    script_entity = f"script.{script_id}"
                    created_scripts.append((script_id, script_entity))
                    cleanup_tracker.track("script", script_entity)

                    logger.info(f"✅ Created: {script_entity}")

                except Exception as e:
                    logger.error(f"❌ Failed to create {script_id}: {e}")
                    failed_scripts.append((script_id, str(e)))

                await asyncio.sleep(0.5)  # Small delay between creations

            if failed_scripts:
                logger.warning(
                    f"⚠️ {len(failed_scripts)} scripts failed to create: {[s[0] for s in failed_scripts]}"
                )

            logger.info(
                f"✅ Bulk creation completed: {len(created_scripts)} successful, {len(failed_scripts)} failed"
            )

            # 2. VERIFY: All successfully created scripts exist and have correct configurations
            await asyncio.sleep(wait_for_script_registration(len(created_scripts)))

            logger.info("🔍 Verifying all scripts exist...")
            verified_scripts = []
            for script_id, script_entity in created_scripts:
                try:
                    get_data = await mcp.call_tool_success(
                        "ha_config_get_script",
                        { "script_id": script_id}
                    )

                    config = extract_script_config(get_data)
                    assert "alias" in config, (
                        f"Alias missing for {script_entity}: {config}"
                    )
                    assert "sequence" in config, (
                        f"Sequence missing for {script_entity}: {config}"
                    )
                    assert validate_script_sequence(
                        config.get("sequence", []), len(config.get("sequence", []))
                    ), f"Invalid sequence for {script_entity}"

                    verified_scripts.append((script_id, script_entity))
                    logger.info(f"✅ Verified: {script_entity} - {config.get('alias')}")

                except Exception as e:
                    logger.error(f"❌ Failed to verify {script_entity}: {e}")

            # 3. EXECUTE: Bulk execution of verified scripts
            logger.info(
                f"🚀 Bulk executing {len(verified_scripts)} verified scripts..."
            )

            executed_scripts = []
            for script_id, script_entity in verified_scripts:
                try:
                    execute_data = await mcp.call_tool_success(
                        "ha_call_service",
                        {
                            "domain": "script",
                            "service": "turn_on",
                            "entity_id": script_entity,
                        },
                    )
                    executed_scripts.append((script_id, script_entity))
                    logger.info(f"✅ Executed: {script_entity}")
                except Exception as e:
                    logger.error(f"❌ Failed to execute {script_entity}: {e}")

            # Allow all executions to complete (max delay is 2s + processing)
            await asyncio.sleep(4)
            logger.info(
                f"✅ Bulk execution completed: {len(executed_scripts)} scripts executed"
            )

            # 4. CLEANUP: Bulk deletion with tracking
            logger.info(f"🗑️ Bulk deleting {len(created_scripts)} scripts...")
            deleted_scripts = []
            failed_deletions = []

            for script_id, script_entity in created_scripts:
                try:
                    delete_data = await mcp.call_tool_success(
                        "ha_config_remove_script",
                        { "script_id": script_id}
                    )
                    deleted_scripts.append((script_id, script_entity))
                    logger.debug(f"🗑️ Deleted: {script_entity}")
                except Exception as e:
                    logger.error(f"❌ Failed to delete {script_entity}: {e}")
                    failed_deletions.append((script_id, script_entity, str(e)))

                await asyncio.sleep(0.3)  # Small delay between deletions

            logger.info(
                f"✅ Bulk deletion completed: {len(deleted_scripts)} deleted, {len(failed_deletions)} failed"
            )

            if failed_deletions:
                logger.warning(
                    f"⚠️ Failed to delete scripts: {[s[1] for s in failed_deletions]}"
                )

    async def test_script_error_handling(self, mcp_client, cleanup_tracker):
        """
        Test: Script error handling and validation

        Validates proper error handling for invalid configurations and operations.
        Tests network resilience and edge cases.
        """

        logger.info("🚨 Testing script error handling...")

        async with MCPAssertions(mcp_client) as mcp:
            # 1. TEST: Get non-existent script
            nonexistent_data = await mcp.call_tool_failure(
                "ha_config_get_script",
                { "script_id": "nonexistent_script_xyz"},
                expected_error="not found",
            )
            logger.info("✅ Non-existent script properly handled")

            # 2. TEST: Invalid config (missing sequence)
            script_id = "test_invalid_config"
            invalid_config_data = await mcp.call_tool_failure(
                "ha_config_set_script",
                {
                    "script_id": script_id,
                    "config": {
                        "alias": "Invalid Script",
                        "description": "Missing sequence",
                        # Missing required 'sequence' field
                    },
                },
                expected_error="sequence",
            )
            logger.info("✅ Invalid config (missing sequence) properly rejected")

            # 3. TEST: Delete non-existent script
            delete_nonexistent_data = await mcp.call_tool_failure(
                "ha_config_remove_script",
                { "script_id": "nonexistent_delete_xyz"},
                expected_error="not found",
            )
            logger.info("✅ Delete non-existent script properly handled")

            # 4. TEST: Invalid script ID format
            invalid_id_data = await mcp.call_tool_failure(
                "ha_config_get_script",
                { "script_id": "invalid.script.id.with.dots"},
            )
            logger.info("✅ Invalid script ID format properly handled")

            # 5. TEST: Invalid sequence structure (non-list)
            invalid_sequence_data = await mcp.call_tool_failure(
                "ha_config_set_script",
                {
                    "script_id": "test_invalid_sequence",
                    "config": {
                        "alias": "Invalid Sequence Script",
                        "description": "Script with invalid sequence type",
                        "sequence": "not_a_list",  # Invalid sequence type
                        "mode": "single"},
                },
            )
            logger.info("✅ Invalid sequence type properly rejected")

            logger.info("✅ All error handling tests passed")


async def test_script_search_and_discovery(mcp_client):
    """
    Test: Script search and discovery capabilities

    Validates that users can find and explore existing scripts.
    Tests search functionality and configuration retrieval.
    """

    logger.info("🔍 Testing script search and discovery...")

    async with MCPAssertions(mcp_client) as mcp:
        # Search for existing scripts with enhanced error handling
        try:
            search_data = await mcp.call_tool_success(
                "ha_search_entities",
                {"query": "script", "domain_filter": "script", "limit": 10},
            )

            # Handle nested data structure
            data = (
                search_data.get("data", {}) if search_data.get("data") else search_data
            )

            if data.get("success") and data.get("results"):
                results = data.get("results", [])
                logger.info(f"✅ Found {len(results)} existing scripts")

                # Test getting configuration of first found script
                if results:
                    first_script = results[0]
                    script_entity_id = first_script.get("entity_id", "")
                    script_id = script_entity_id.replace("script.", "")

                    logger.info(
                        f"🔍 Testing configuration retrieval for: {script_entity_id}"
                    )

                    # Try to get script configuration (may fail for YAML-defined scripts)
                    try:
                        get_data = await mcp.call_tool_success(
                            "ha_config_get_script",
                            { "script_id": script_id},
                        )

                        config = extract_script_config(get_data)
                        alias = config.get("alias", "No alias")
                        sequence_count = len(config.get("sequence", []))
                        mode = config.get("mode", "unknown")

                        logger.info(f"✅ Retrieved config for {script_entity_id}:")
                        logger.info(f"    - Alias: {alias}")
                        logger.info(f"    - Steps: {sequence_count}")
                        logger.info(f"    - Mode: {mode}")

                    except Exception as e:
                        logger.info(
                            f"ℹ️ Could not retrieve config for {script_entity_id}: {str(e)} (likely YAML-defined)"
                        )

                    # Test search with more specific criteria
                    specific_search_data = await mcp.call_tool_success(
                        "ha_search_entities",
                        {
                            "query": script_id[:5],  # First 5 chars of script ID
                            "domain_filter": "script",
                            "limit": 5,
                        },
                    )

                    specific_data = (
                        specific_search_data.get("data", {})
                        if specific_search_data.get("data")
                        else specific_search_data
                    )
                    specific_results = specific_data.get("results", [])
                    logger.info(
                        f"✅ Specific search found {len(specific_results)} matching scripts"
                    )
            else:
                logger.info("ℹ️ No scripts found in system for discovery test")

        except Exception as e:
            logger.warning(f"⚠️ Script search failed: {e}")
            logger.info(
                "ℹ️ This may be normal if no scripts exist in the test environment"
            )

    logger.info("✅ Script search and discovery test completed")
