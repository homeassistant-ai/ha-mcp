"""
Automation Lifecycle E2E Tests

Tests the complete automation workflow: Create → Trigger → Update → Delete
This represents the most critical user journey for Home Assistant automation management.

Note: Tests are designed to work with both Docker test environment (localhost:8124)
and production environments. Entity references are dynamically discovered.
"""

import logging

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
    safe_call_tool,
    wait_for_automation,
)
from ...utilities.wait_helpers import (
    wait_for_entity_state,
    wait_for_logbook_entry,
    wait_for_tool_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.automation
@pytest.mark.cleanup
class TestAutomationLifecycle:
    """Test complete automation management workflows."""

    async def _find_test_light_entity(self, mcp_client) -> str:
        """
        Find a suitable light entity for testing.

        Prefers demo entities, falls back to any available light.
        Returns entity_id of a suitable light for testing.
        """
        # Search for light entities
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "light", "domain_filter": "light", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)

        # Handle nested data structure
        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if not results:
            pytest.skip("No light entities available for testing")

        # Prefer demo entities
        for entity in results:
            entity_id = entity.get("entity_id", "")
            if "demo" in entity_id.lower() or "test" in entity_id.lower():
                logger.info(f"🔍 Using demo/test light: {entity_id}")
                return entity_id

        # Fall back to first available light
        entity_id = results[0].get("entity_id", "")
        if not entity_id:
            pytest.skip("No valid light entity found for testing")

        logger.info(f"🔍 Using first available light: {entity_id}")
        return entity_id

    async def _find_test_binary_sensors(self, mcp_client) -> list[str]:
        """
        Find suitable binary sensor entities for testing.

        Returns list of binary sensor entity_ids suitable for testing.
        """
        # Search for binary sensor entities
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "binary_sensor", "domain_filter": "binary_sensor", "limit": 20},
        )

        search_data = parse_mcp_result(search_result)

        # Handle nested data structure
        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if not results:
            # If no binary sensors, use a light entity as fallback
            logger.warning("No binary_sensor entities found, using light as fallback")
            test_light = await self._find_test_light_entity(mcp_client)
            return [
                test_light,
                test_light,
            ]  # Return same entity twice for compatibility

        # Prefer demo entities
        demo_sensors = []
        all_sensors = []

        for entity in results:
            entity_id = entity.get("entity_id", "")
            if entity_id:
                all_sensors.append(entity_id)
                if "demo" in entity_id.lower() or "test" in entity_id.lower():
                    demo_sensors.append(entity_id)

        # Return at least 2 entities (duplicate if needed)
        if demo_sensors:
            result = demo_sensors[:2]
            if len(result) == 1:
                result.append(result[0])  # Duplicate if only one found
            logger.info(f"🔍 Using demo binary sensors: {result}")
            return result

        if all_sensors:
            result = all_sensors[:2]
            if len(result) == 1:
                result.append(result[0])  # Duplicate if only one found
            logger.info(f"🔍 Using available binary sensors: {result}")
            return result

        # Ultimate fallback - use light entities
        logger.warning(
            "No suitable binary sensors found, using light entities as fallback"
        )
        test_light = await self._find_test_light_entity(mcp_client)
        return [test_light, test_light]

    async def test_basic_automation_lifecycle(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Create basic automation → Trigger → Verify → Delete

        This test validates the fundamental automation workflow that most
        users will follow when setting up Home Assistant automations.
        """

        # 1. DISCOVER: Find available test entities
        test_light = await self._find_test_light_entity(mcp_client)
        logger.info(f"🔍 Using test light entity: {test_light}")

        # 2. CREATE: Basic time-based automation
        automation_name = "Morning Light E2E"
        create_config = test_data_factory.automation_config(
            automation_name,
            trigger=[{"platform": "time", "at": "07:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )

        logger.info(f"📝 Creating automation: {automation_name}")
        # Use safe_call_tool to handle ToolError exceptions gracefully
        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {"config": create_config},
        )
        assert create_data.get("success"), f"automation creation failed: {create_data}"

        # Extract automation entity ID with robust error handling
        automation_entity = create_data.get("entity_id")
        if not automation_entity:
            # Fallback to construct entity ID from alias
            alias = create_config["alias"]
            automation_entity = (
                f"automation.{alias.lower().replace(' ', '_').replace('_e2e', '_e2e')}"
            )
            logger.warning(
                f"No entity_id in response, using constructed ID: {automation_entity}"
            )

        # Validate entity ID format
        if not automation_entity.startswith("automation."):
            raise AssertionError(
                f"Invalid automation entity ID format: {automation_entity}"
            )

        cleanup_tracker.track("automation", automation_entity)
        logger.info(f"✅ Created automation: {automation_entity}")

        # 3. VERIFY: Automation exists and is configured correctly
        # Wait for Home Assistant to register the new automation
        logger.info("🔍 Verifying automation configuration...")
        config = await wait_for_automation(mcp_client, automation_entity, timeout=10)
        if not config:
            raise AssertionError(
                f"Automation {automation_entity} not found after creation"
            )

        # Check essential fields
        assert config.get("alias") == create_config["alias"], (
            f"Alias mismatch: {config.get('alias')} != {create_config['alias']}"
        )
        assert "trigger" in config or "triggers" in config, (
            "No triggers found in automation config"
        )
        assert "action" in config or "actions" in config, (
            "No actions found in automation config"
        )

        logger.info("✅ Automation configuration verified")

        # 4. TRIGGER: Manually trigger the automation
        logger.info("🚀 Triggering automation...")
        trigger_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "trigger",
                "entity_id": automation_entity,
            },
        )

        trigger_data = assert_mcp_success(trigger_result, "automation trigger")
        logger.info("✅ Automation triggered successfully")

        # 5. VERIFY: Check that automation ran (via logbook)
        logger.info("📋 Checking automation execution in logbook...")
        try:
            automation_logged = await wait_for_logbook_entry(
                mcp_client, automation_name, timeout=10, poll_interval=1.0
            )
            if automation_logged:
                logger.info("📋 Automation execution verified in logbook")
            else:
                logger.info(
                    "📋 Logbook verification timeout - automation trigger was successful"
                )
        except Exception as e:
            logger.warning(f"Logbook verification failed: {e} - continuing with test")

        # 6. UPDATE: Modify automation to add delay and different time
        logger.info("📝 Updating automation configuration...")
        update_config = test_data_factory.automation_config(
            f"{automation_name} Updated",
            trigger=[{"platform": "time", "at": "07:30:00"}],  # Different time
            action=[
                {"service": "light.turn_on", "target": {"entity_id": test_light}},
                {"delay": {"seconds": 2}},
                {"service": "light.turn_off", "target": {"entity_id": test_light}},
            ],
        )

        update_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {
                "identifier": automation_entity,
                "config": update_config},
        )

        update_data = assert_mcp_success(update_result, "automation update")
        logger.info("✅ Automation updated successfully")

        # 7. VERIFY: Update was applied
        logger.info("🔍 Verifying automation update...")
        config = await wait_for_automation(mcp_client, automation_entity, timeout=10)
        if not config:
            raise AssertionError(
                f"No configuration returned after update for automation {automation_entity}"
            )

        # Verify updated fields with better error messages
        assert config.get("alias") == update_config["alias"], (
            f"Alias not updated: {config.get('alias')} != {update_config['alias']}"
        )

        # Check actions (Home Assistant may return 'action' or 'actions')
        actions = config.get("actions") or config.get("action", [])
        assert len(actions) == 3, (
            f"Expected 3 actions after update, got {len(actions)}: {actions}"
        )

        # Check trigger time (Home Assistant may return 'trigger' or 'triggers')
        triggers = config.get("triggers") or config.get("trigger", [])
        if triggers:
            trigger_time = triggers[0].get("at")
            assert trigger_time == "07:30:00", (
                f"Trigger time not updated: {trigger_time} != 07:30:00"
            )

        logger.info("✅ Automation update verified")

        # 8. DELETE: Clean up test automation
        logger.info("🗑️ Deleting automation...")
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_automation",
            { "identifier": automation_entity},
        )

        delete_data = assert_mcp_success(delete_result, "automation deletion")
        logger.info("✅ Automation deleted successfully")

        # 9. VERIFY: Automation is gone
        logger.info("🔍 Verifying automation deletion...")
        # Poll to ensure deletion propagated (wait_for_automation returns None if not found)
        config = await wait_for_automation(mcp_client, automation_entity, timeout=5)

        # If still found, that's a problem
        if config is not None:
            raise AssertionError(
                f"Automation {automation_entity} still exists after deletion: {config}"
            )

        # Double-check with direct call for error message verification
        # Use safe_call_tool since we expect this to fail (automation deleted)
        final_data = await safe_call_tool(
            mcp_client,
            "ha_config_get_automation",
            {"identifier": automation_entity},
        )
        # Automation should not exist anymore - this should fail
        assert not final_data.get("success"), (
            f"Automation should be deleted but still exists: {final_data}"
        )

        # Check for expected error indicators
        expected_errors = ["not found", "does not exist", "404"]
        error_msg = str(final_data.get("error", "")).lower()
        has_expected_error = any(err in error_msg for err in expected_errors)

        if final_data.get("success") or not has_expected_error:
            logger.warning(f"Unexpected deletion verification result: {final_data}")

        logger.info("✅ Automation deletion verified")

    async def test_automation_enable_disable_lifecycle(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Automation enabling and disabling functionality

        This test validates automation state management which is critical
        for users who want to temporarily disable automations.
        """
        # Find test entity
        test_light = await self._find_test_light_entity(mcp_client)

        # Create automation in disabled state
        automation_name = "Toggle Test E2E"
        config = test_data_factory.automation_config(
            automation_name,
            trigger=[{"platform": "time", "at": "09:00:00"}],
            action=[{"service": "light.toggle", "target": {"entity_id": test_light}}],
            initial_state=False,  # Start disabled
        )

        logger.info(f"📝 Creating disabled automation: {automation_name}")
        create_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            { "config": config}
        )

        create_data = assert_mcp_success(create_result, "disabled automation creation")
        automation_entity = (
            create_data.get("entity_id")
            or f"automation.{automation_name.lower().replace(' ', '_')}"
        )
        cleanup_tracker.track("automation", automation_entity)

        # Wait for automation to be registered and verify it starts in disabled state
        state_reached = await wait_for_entity_state(
            mcp_client, automation_entity, "off", timeout=20
        )
        assert state_reached, (
            f"Automation {automation_entity} did not reach initial state 'off' within timeout"
        )
        logger.info("✅ Automation correctly starts in disabled state")

        # Enable the automation
        logger.info("🔄 Enabling automation...")
        enable_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "turn_on",
                "entity_id": automation_entity,
            },
        )

        enable_data = assert_mcp_success(enable_result, "automation enable")

        # Verify automation is now enabled
        state_reached = await wait_for_entity_state(
            mcp_client, automation_entity, "on", timeout=20
        )
        assert state_reached, (
            f"Automation {automation_entity} did not reach enabled state 'on' within timeout"
        )
        logger.info("✅ Automation successfully enabled")

        # Disable the automation
        logger.info("🔄 Disabling automation...")
        disable_result = await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "automation",
                "service": "turn_off",
                "entity_id": automation_entity,
            },
        )

        disable_data = assert_mcp_success(disable_result, "automation disable")

        # Verify automation is now disabled
        state_reached = await wait_for_entity_state(
            mcp_client, automation_entity, "off", timeout=20
        )
        assert state_reached, (
            f"Automation {automation_entity} did not reach disabled state 'off' within timeout"
        )
        logger.info("✅ Automation successfully disabled")

        # Clean up
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_automation",
            { "identifier": automation_entity},
        )
        assert_mcp_success(delete_result, "automation cleanup")
        logger.info("🗑️ Automation cleaned up")

    async def test_automation_yaml_validation(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Automation YAML configuration validation

        This test validates that automation configurations are properly validated
        and that invalid configurations are rejected appropriately.
        """
        test_light = await self._find_test_light_entity(mcp_client)

        # Test valid configuration
        logger.info("🧪 Testing valid automation configuration...")
        valid_config = test_data_factory.automation_config(
            "Valid Config E2E",
            trigger=[
                {"platform": "time", "at": "10:00:00"},
                {"platform": "state", "entity_id": test_light, "to": "on"},
            ],
            condition=[
                {"condition": "time", "after": "09:00:00", "before": "17:00:00"}
            ],
            action=[
                {"service": "light.turn_off", "target": {"entity_id": test_light}},
                {"delay": {"seconds": 5}},
                {
                    "service": "persistent_notification.create",
                    "data": {"message": "Valid automation executed", "title": "Test"},
                },
            ],
            mode="single",
        )

        create_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            { "config": valid_config}
        )

        create_data = assert_mcp_success(create_result, "valid configuration creation")
        automation_entity = (
            create_data.get("entity_id")
            or f"automation.{valid_config['alias'].lower().replace(' ', '_')}"
        )
        cleanup_tracker.track("automation", automation_entity)
        logger.info("✅ Valid configuration accepted")

        # Verify configuration structure
        get_result = await mcp_client.call_tool(
            "ha_config_get_automation",
            { "identifier": automation_entity}
        )

        get_data = assert_mcp_success(get_result, "configuration retrieval")
        config = get_data.get("config", {})

        # Validate all expected sections are present
        assert config.get("alias"), "Configuration missing alias"

        # Check triggers (Home Assistant may use 'trigger' or 'triggers')
        triggers = config.get("triggers") or config.get("trigger", [])
        assert len(triggers) == 2, f"Expected 2 triggers, got {len(triggers)}"

        # Check conditions
        conditions = config.get("conditions") or config.get("condition", [])
        assert len(conditions) == 1, f"Expected 1 condition, got {len(conditions)}"

        # Check actions
        actions = config.get("actions") or config.get("action", [])
        assert len(actions) == 3, f"Expected 3 actions, got {len(actions)}"

        # Check mode
        assert config.get("mode") == "single", (
            f"Expected mode 'single', got {config.get('mode')}"
        )

        logger.info("✅ Configuration structure validated")

        # Test invalid configuration (should fail gracefully)
        logger.info("🧪 Testing invalid automation configuration...")
        invalid_config = {
            "alias": "Invalid Config E2E",
            "trigger": [
                {"platform": "invalid_platform"}
            ],  # Invalid: platform doesn't exist
            "action": [{"service": "nonexistent.service"}],  # Invalid service
        }

        try:
            invalid_result = await mcp_client.call_tool(
                "ha_config_set_automation",
                { "config": invalid_config}
            )

            invalid_data = parse_mcp_result(invalid_result)

            # Invalid config should fail
            if invalid_data.get("success"):
                logger.warning("Invalid configuration was unexpectedly accepted")
                # If it was accepted, clean it up
                if invalid_data.get("entity_id"):
                    cleanup_tracker.track("automation", invalid_data["entity_id"])
            else:
                logger.info("✅ Invalid configuration properly rejected")

        except Exception as e:
            logger.info(
                f"✅ Invalid configuration properly rejected with exception: {e}"
            )

        # Clean up valid automation
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_automation",
            { "identifier": automation_entity},
        )
        assert_mcp_success(delete_result, "valid automation cleanup")
        logger.info("🗑️ Test automations cleaned up")

    @pytest.mark.slow
    async def test_complex_automation_with_conditions(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Complex automation with multiple triggers, conditions, and templates

        This test validates advanced automation features that power users rely on.
        """

        automation_name = "Complex Security E2E"

        # Discover test entities for complex automation
        test_light = await self._find_test_light_entity(mcp_client)
        test_binary_sensors = await self._find_test_binary_sensors(mcp_client)

        logger.info(
            f"🔍 Using test entities - Light: {test_light}, Binary sensors: {test_binary_sensors}"
        )

        # Create complex automation with conditions and templates
        complex_config = test_data_factory.automation_config(
            automation_name,
            trigger=[
                {"platform": "state", "entity_id": test_binary_sensors[0], "to": "on"},
                {
                    "platform": "state",
                    "entity_id": (
                        test_binary_sensors[1]
                        if len(test_binary_sensors) > 1
                        else test_binary_sensors[0]
                    ),
                    "to": "on",
                },
            ],
            condition=[
                {"condition": "time", "after": "22:00:00", "before": "06:00:00"},
                {"condition": "state", "entity_id": test_light, "state": "off"},
            ],
            action=[
                {
                    "service": "light.turn_on",
                    "target": {"entity_id": test_light},
                    "data": {"brightness_pct": 25},
                },
                {
                    "service": "persistent_notification.create",
                    "data": {
                        "title": "Security Alert",
                        "message": "Activity detected at {{ now().strftime('%H:%M:%S') }}",
                    },
                },
            ],
            mode="single",
        )

        logger.info(f"📝 Creating complex automation: {automation_name}")
        create_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            { "config": complex_config}
        )

        create_data = assert_mcp_success(create_result, "complex automation creation")

        automation_entity = (
            create_data.get("entity_id")
            or f"automation.{automation_name.lower().replace(' ', '_')}"
        )
        if not automation_entity.startswith("automation."):
            raise AssertionError(
                f"Invalid complex automation entity ID format: {automation_entity}"
            )

        cleanup_tracker.track("automation", automation_entity)
        logger.info(f"✅ Complex automation created: {automation_entity}")

        # Test template evaluation used in the automation
        logger.info("🧪 Testing template evaluation...")
        template_result = await mcp_client.call_tool(
            "ha_eval_template", {"template": "{{ now().strftime('%H:%M:%S') }}"}
        )

        template_data = assert_mcp_success(template_result, "template evaluation")

        result = template_data.get("result", "")
        assert ":" in result, (
            f"Template should return time string with colon, got: {result}"
        )
        assert len(result) >= 8, f"Template result too short for time format: {result}"

        logger.info(f"✅ Template evaluation works: {result}")

        # Verify complex configuration
        logger.info("🔍 Verifying complex automation configuration...")
        get_result = await mcp_client.call_tool(
            "ha_config_get_automation",
            { "identifier": automation_entity}
        )

        get_data = assert_mcp_success(get_result, "complex automation retrieval")

        config = get_data.get("config", {})
        if not config:
            raise AssertionError(
                f"No configuration returned for complex automation {automation_entity}"
            )

        # Home Assistant API returns plural forms
        triggers = config.get("triggers") or config.get("trigger", [])
        conditions = config.get("conditions") or config.get("condition", [])
        actions = config.get("actions") or config.get("action", [])

        # Validate configuration structure
        assert len(triggers) == 2, (
            f"Expected 2 triggers, got {len(triggers)}: {triggers}"
        )
        assert len(conditions) == 2, (
            f"Expected 2 conditions, got {len(conditions)}: {conditions}"
        )
        assert len(actions) == 2, f"Expected 2 actions, got {len(actions)}: {actions}"
        assert config.get("mode") == "single", (
            f"Expected mode 'single', got: {config.get('mode')}"
        )

        logger.info("✅ Complex automation configuration verified")

        # Cleanup
        logger.info("🗑️ Cleaning up complex automation...")
        delete_result = await mcp_client.call_tool(
            "ha_config_remove_automation",
            { "identifier": automation_entity},
        )

        delete_data = assert_mcp_success(delete_result, "complex automation deletion")
        logger.info("✅ Complex automation cleaned up")

    async def test_automation_mode_behaviors(
        self, mcp_client, cleanup_tracker, test_data_factory
    ):
        """
        Test: Different automation execution modes (single, restart, queued, parallel)

        This test validates automation execution behavior modes that affect how
        automations handle multiple triggers.
        """

        # Test different mode configurations
        modes_to_test = ["single", "restart", "queued", "parallel"]

        for mode in modes_to_test:
            automation_name = f"Mode Test {mode.title()} E2E"
            logger.info(f"🧪 Testing automation mode: {mode}")

            # Use dynamic test entity
            test_light = await self._find_test_light_entity(mcp_client)

            mode_config = test_data_factory.automation_config(
                automation_name,
                trigger=[{"platform": "time", "at": "08:00:00"}],
                action=[
                    {"delay": {"seconds": 1}},
                    {"service": "light.toggle", "target": {"entity_id": test_light}},
                ],
                mode=mode,
                max=3 if mode in ["queued", "parallel"] else None,
            )

            # Remove None values
            if mode_config.get("max") is None:
                mode_config.pop("max", None)

            create_result = await mcp_client.call_tool(
                "ha_config_set_automation",
                { "config": mode_config}
            )

            create_data = assert_mcp_success(
                create_result, f"{mode} mode automation creation"
            )

            automation_entity = (
                create_data.get("entity_id")
                or f"automation.{automation_name.lower().replace(' ', '_')}"
            )
            if not automation_entity.startswith("automation."):
                raise AssertionError(
                    f"Invalid {mode} automation entity ID format: {automation_entity}"
                )

            cleanup_tracker.track("automation", automation_entity)

            # Verify mode is set correctly
            get_result = await mcp_client.call_tool(
                "ha_config_get_automation",
                { "identifier": automation_entity},
            )

            get_data = assert_mcp_success(
                get_result, f"{mode} mode automation retrieval"
            )

            config = get_data.get("config", {})
            if not config:
                raise AssertionError(
                    f"No configuration returned for {mode} automation {automation_entity}"
                )

            assert config.get("mode") == mode, (
                f"Mode not set correctly for {mode}: expected '{mode}', got '{config.get('mode')}'"
            )

            if mode in ["queued", "parallel"]:
                max_value = config.get("max")
                assert max_value == 3, (
                    f"Max not set correctly for {mode}: expected 3, got {max_value}"
                )

            logger.info(f"✅ Mode {mode} automation created and verified")

            # Cleanup immediately to avoid entity ID conflicts
            delete_result = await mcp_client.call_tool(
                "ha_config_remove_automation",
                { "identifier": automation_entity},
            )

            delete_data = assert_mcp_success(
                delete_result, f"{mode} mode automation deletion"
            )
            logger.info(f"🗑️ Mode {mode} automation cleaned up")


@pytest.mark.automation
async def test_automation_search_and_discovery(mcp_client):
    """
    Test: Automation search and discovery capabilities

    Validates that users can find and explore existing automations
    through the search functionality.
    """

    logger.info("🔍 Testing automation search and discovery...")

    # Search for existing automations
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "automation", "domain_filter": "automation", "limit": 10},
    )

    search_data = parse_mcp_result(search_result)

    # Handle different response formats
    if "data" in search_data:
        # Success is nested in data
        data_section = search_data.get("data", {})
        assert data_section.get("success"), f"Automation search failed: {search_data}"
        results = data_section.get("results", [])
    else:
        # Success is at top level
        assert search_data.get("success"), f"Automation search failed: {search_data}"
        results = search_data.get("results", [])

    logger.info(f"🔍 Found {len(results)} automations")

    # Get system overview to see automation status
    overview_result = await mcp_client.call_tool("ha_get_overview")
    overview_data = parse_mcp_result(overview_result)

    # Should have automation information in overview
    overview_text = str(overview_data).lower()
    assert "automation" in overview_text, (
        "System overview should include automation information"
    )
    logger.info("✅ System overview includes automation data")

    # Test entity search with different patterns
    search_patterns = ["morning", "light", "security"]
    for pattern in search_patterns:
        pattern_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": pattern, "domain_filter": "automation", "limit": 5},
        )

        pattern_data = parse_mcp_result(pattern_result)

        # Handle nested data structure if present
        if "data" in pattern_data:
            results = pattern_data.get("data", {}).get("results", [])
        else:
            results = pattern_data.get("results", [])

        logger.info(f"🔍 Pattern '{pattern}' search: {len(results)} results")

    logger.info("✅ Automation search and discovery tests completed")



@pytest.mark.automation
async def test_automation_with_choose_block(mcp_client):
    """
    Test automation with choose blocks to verify conditions (plural) is preserved.

    This test ensures that the normalization bug is fixed where 'conditions'
    was incorrectly being converted to 'condition' inside choose blocks,
    causing API validation failures.
    """
    logger.info("🧪 Testing automation with choose block...")

    # Find a test light entity (poll — entities may not be loaded yet on slow runners)
    def _has_light(data: dict) -> bool:
        results = data.get("data", data).get("results", [])
        return len(results) > 0

    search_data = await wait_for_tool_result(
        mcp_client,
        tool_name="ha_search_entities",
        arguments={"query": "light", "domain_filter": "light", "limit": 5},
        predicate=_has_light,
        timeout=15,
        description="light entities available",
    )
    entities = search_data.get("data", search_data).get("results", [])
    light_entity = entities[0]["entity_id"]
    logger.info(f"🔦 Using test light: {light_entity}")

    automation_id = "test_choose_block_normalization"

    # Create automation with choose block that has conditions (plural)
    config = {
        "alias": "Test Choose Block Normalization",
        "description": "Test that choose block conditions (plural) are preserved",
        "triggers": [  # Using plural to test normalization
            {
                "platform": "state",
                "entity_id": light_entity,
                "to": "on",
                "id": "light_on",
            },
            {
                "platform": "state",
                "entity_id": light_entity,
                "to": "off",
                "id": "light_off",
            },
        ],
        "actions": [  # Using plural to test normalization
            {
                "choose": [
                    {
                        "conditions": [  # MUST remain plural in choose blocks
                            {
                                "condition": "trigger",
                                "id": "light_on",
                            }
                        ],
                        "sequences": [  # Test sequence normalization too
                            {
                                "service": "persistent_notification.create",
                                "data": {
                                    "title": "Choose Test",
                                    "message": "Light turned on",
                                },
                            }
                        ],
                    },
                    {
                        "conditions": [  # MUST remain plural
                            {
                                "condition": "trigger",
                                "id": "light_off",
                            }
                        ],
                        "sequence": [  # Test singular form too
                            {
                                "service": "persistent_notification.create",
                                "data": {
                                    "title": "Choose Test",
                                    "message": "Light turned off",
                                },
                            }
                        ],
                    },
                ],
                "default": [
                    {
                        "service": "persistent_notification.create",
                        "data": {
                            "title": "Choose Test",
                            "message": "Default action",
                        },
                    }
                ],
            }
        ],
    }

    # Create the automation - THIS IS THE KEY TEST
    # If normalization is broken, this will fail with:
    # "extra keys not allowed @ data['actions'][0]['choose'][0]['condition']"
    logger.info("📝 Creating automation with choose block...")
    create_result = await mcp_client.call_tool(
        "ha_config_set_automation",
        {
            "identifier": automation_id,
            "config": config,
        },
    )

    assert_mcp_success(create_result)
    logger.info("✅ Automation with choose block created successfully")

    # Wait for automation to be registered
    await wait_for_automation(mcp_client, automation_id)

    # Retrieve the automation to verify structure
    get_result = await mcp_client.call_tool(
        "ha_config_get_automation",
        {"identifier": automation_id},
    )

    automation_data = parse_mcp_result(get_result)
    logger.info("📥 Retrieved automation configuration")

    # Extract config from response
    config_data = automation_data.get("config", automation_data)

    # Verify the automation has the correct structure
    assert "trigger" in config_data or "triggers" in config_data, (
        "Automation should have triggers"
    )

    actions = config_data.get("action", config_data.get("actions", []))
    assert len(actions) > 0, "Automation should have actions"

    choose_action = actions[0]
    assert "choose" in choose_action, "First action should be a choose block"
    assert len(choose_action["choose"]) == 2, "Choose should have 2 options"

    # Verify that conditions are preserved in choose options
    for i, option in enumerate(choose_action["choose"]):
        # The key could be 'conditions' or 'condition' depending on HA version
        # But our normalization should have sent 'conditions' to the API
        has_conditions = "conditions" in option or "condition" in option
        assert has_conditions, (
            f"Choose option {i} should have conditions defined"
        )
        logger.info(f"✅ Choose option {i} has condition key: {list(option.keys())}")

    # The fact that we successfully created and retrieved the automation
    # with choose blocks proves the normalization fix works.
    # Execution testing would require more complex setup (triggering actual
    # entity state changes) which is beyond the scope of this normalization test.
    logger.info("✅ Choose block normalization verified - automation API accepted the config")

    # Clean up
    logger.info("🧹 Cleaning up test automation...")
    delete_result = await mcp_client.call_tool(
        "ha_config_remove_automation",
        {"identifier": automation_id},
    )
    assert_mcp_success(delete_result)

    logger.info("✅ Choose block normalization test completed successfully")


@pytest.mark.automation
async def test_duplicate_automation_prevention(mcp_client, cleanup_tracker):
    """
    Test: Creating automation with existing 'id' in config but no identifier is rejected.

    Validates fix for issue #698 — when an agent retrieves an automation config
    (which contains an 'id' field) and passes it back to ha_config_set_automation
    without an identifier, the tool should reject the request instead of silently
    creating a duplicate.
    """
    logger.info("Testing duplicate automation prevention...")

    # First create a real automation so we have a valid config with an 'id' field
    create_result = await safe_call_tool(
        mcp_client,
        "ha_config_set_automation",
        {
            "config": {
                "alias": "Duplicate Prevention Test E2E",
                "description": "E2E test - safe to delete",
                "trigger": [{"platform": "time", "at": "06:00:00"}],
                "action": [
                    {
                        "service": "persistent_notification.create",
                        "data": {"message": "test"},
                    }
                ],
            }
        },
    )
    assert create_result.get("success"), f"Initial creation failed: {create_result}"
    automation_entity = create_result.get("entity_id")
    unique_id = create_result.get("unique_id")
    assert automation_entity, "No entity_id returned"
    assert unique_id, "No unique_id returned"
    cleanup_tracker.track("automation", automation_entity)
    logger.info(f"Created test automation: {automation_entity} (id={unique_id})")

    # Now retrieve the automation config — it will contain the 'id' field
    config = await wait_for_automation(mcp_client, automation_entity, timeout=10)
    assert config is not None, "Could not retrieve created automation"
    assert "id" in config, f"Retrieved config should contain 'id' field: {config.keys()}"

    # Attempt to create a new automation using this config WITHOUT passing identifier.
    # This should be rejected because the config contains an existing 'id'.
    logger.info("Attempting to create automation with existing 'id' in config (no identifier)...")
    duplicate_result = await safe_call_tool(
        mcp_client,
        "ha_config_set_automation",
        {"config": config},
    )

    assert not duplicate_result.get("success"), (
        f"Should have rejected config with existing 'id' but got success: {duplicate_result}"
    )

    # Verify the error mentions the 'id' field and provides guidance
    error = duplicate_result.get("error", {})
    error_msg = error.get("message", "") if isinstance(error, dict) else str(error)
    assert "id" in error_msg.lower(), (
        f"Error should mention 'id' field: {error_msg}"
    )
    logger.info(f"Correctly rejected with error: {error_msg}")

    # Clean up
    delete_result = await mcp_client.call_tool(
        "ha_config_remove_automation",
        {"identifier": automation_entity},
    )
    assert_mcp_success(delete_result, "duplicate prevention test cleanup")
    logger.info("Duplicate automation prevention test passed")


@pytest.mark.automation
async def test_automation_creation_returns_verified_entity(
    mcp_client, cleanup_tracker, test_data_factory
):
    """
    Test: Successful automation creation returns a verified entity_id.

    Validates fix for issue #610 — after creating an automation, the tool should
    return a real entity_id that was confirmed via state polling, not a predicted one.
    The entity_id must be queryable immediately after creation returns.
    """
    logger.info("Testing automation creation returns verified entity...")

    config = test_data_factory.automation_config(
        "Verified Entity",
        trigger=[{"platform": "time", "at": "06:00:00"}],
        action=[
            {
                "service": "persistent_notification.create",
                "data": {"message": "verified entity test"},
            }
        ],
    )

    create_result = await safe_call_tool(
        mcp_client,
        "ha_config_set_automation",
        {"config": config},
    )
    assert create_result.get("success"), f"Automation creation failed: {create_result}"

    entity_id = create_result.get("entity_id")
    assert entity_id, "No entity_id returned from creation"
    assert entity_id.startswith("automation."), f"Invalid entity_id format: {entity_id}"
    cleanup_tracker.track("automation", entity_id)

    # The returned entity_id should be immediately queryable since it was verified
    logger.info(f"Verifying returned entity_id {entity_id} is queryable...")
    state_result = await safe_call_tool(
        mcp_client,
        "ha_get_state",
        {"entity_id": entity_id},
    )
    # ha_get_state nests entity data under 'data' key
    state_data = state_result.get("data", state_result)
    assert state_data.get("entity_id") == entity_id, (
        f"Returned entity_id {entity_id} is not queryable: {state_result}"
    )
    logger.info(f"Entity {entity_id} is queryable - verified, not predicted")

    # Clean up
    delete_result = await mcp_client.call_tool(
        "ha_config_remove_automation",
        {"identifier": entity_id},
    )
    assert_mcp_success(delete_result, "verified entity test cleanup")
    logger.info("Automation creation verified entity test passed")



async def _find_test_light_entity(mcp_client) -> str:
    """Find a suitable light entity for testing.

    Prefers demo/test entities, falls back to first available light.
    Shared helper used by multiple test classes in this module.
    """
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "domain_filter": "light", "limit": 20},
    )
    search_data = parse_mcp_result(search_result)
    if "data" in search_data:
        results = search_data.get("data", {}).get("results", [])
    else:
        results = search_data.get("results", [])
    if not results:
        pytest.skip("No light entities available for testing")
    for entity in results:
        entity_id = entity.get("entity_id", "")
        if "demo" in entity_id.lower() or "test" in entity_id.lower():
            return entity_id
    entity_id = results[0].get("entity_id", "")
    if not entity_id:
        pytest.skip("No valid light entity found for testing")
    return entity_id


@pytest.mark.automation
@pytest.mark.cleanup
class TestConfigHashMismatch:
    """Tests for ha_config_set_automation optimistic-locking guard (Guard 1).

    Guard: tools_config_automations.py _fetch_and_verify_hash —
    raises SERVICE_CALL_FAILED when the supplied config_hash does not match
    the hash of the automation's current stored configuration.

    All tests require a live automation (create → get → set → cleanup).
    Entity references are dynamically discovered (consistent with file docstring).
    """

    async def test_config_hash_mismatch_rejected(
        self, mcp_client, cleanup_tracker, test_data_factory
    ) -> None:
        """Rejects an update when the caller supplies a stale config_hash.

        Guard: _fetch_and_verify_hash raises SERVICE_CALL_FAILED when
        config_hash does not match the current hash. This prevents silent
        overwrites of automations that were modified since last read.
        """
        # 1. DISCOVER: find a valid light entity for the automation action
        test_light = await _find_test_light_entity(mcp_client)
        logger.info(f"Using light entity: {test_light}")

        # 2. CREATE a throwaway automation
        config = test_data_factory.automation_config(
            "A6 Hash Mismatch Test",
            trigger=[{"platform": "time", "at": "03:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )
        create_data = await safe_call_tool(
            mcp_client, "ha_config_set_automation", {"config": config}
        )
        assert create_data.get("success"), f"create failed: {create_data}"
        automation_entity = create_data.get("entity_id")
        assert automation_entity, f"no entity_id in response: {create_data}"
        cleanup_tracker.track("automation", automation_entity)
        logger.info(f"Created automation: {automation_entity}")

        # 2. GET the automation — extract the real config_hash
        await wait_for_automation(mcp_client, automation_entity)
        get_result = await mcp_client.call_tool(
            "ha_config_get_automation", {"identifier": automation_entity}
        )
        get_data = parse_mcp_result(get_result)
        real_hash = get_data.get("config_hash")
        assert real_hash, f"no config_hash in get response: {get_data}"
        logger.info(f"Real config_hash: {real_hash[:12]}...")

        # 3. Construct a stale hash — guarantee it differs from the real one
        stale_hash = real_hash[:-4] + ("0000" if not real_hash.endswith("0000") else "ffff")
        assert stale_hash != real_hash, "stale_hash must differ from real_hash"

        # 4. Attempt update with the stale hash — guard must reject it
        update_config = test_data_factory.automation_config(
            "A6 Hash Mismatch Test Updated",
            trigger=[{"platform": "time", "at": "04:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "identifier": automation_entity,
                "config": update_config,
                "config_hash": stale_hash,
            },
        )
        assert result["success"] is False, f"expected failure with stale hash: {result}"
        assert result["error"]["code"] == "SERVICE_CALL_FAILED", (
            f"expected SERVICE_CALL_FAILED, got: {result['error']['code']}"
        )
        logger.info("Stale hash correctly rejected")

    async def test_update_with_correct_hash_succeeds(
        self, mcp_client, cleanup_tracker, test_data_factory
    ) -> None:
        """Accepts an update when the caller supplies the current config_hash.

        Complementary to test_config_hash_mismatch_rejected: verifies the
        guard fires only on actual mismatches, not on every update.
        """
        # 1. CREATE
        test_light = await _find_test_light_entity(mcp_client)
        config = test_data_factory.automation_config(
            "A6 Hash Valid Test",
            trigger=[{"platform": "time", "at": "05:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )
        create_data = await safe_call_tool(
            mcp_client, "ha_config_set_automation", {"config": config}
        )
        assert create_data.get("success"), f"create failed: {create_data}"
        automation_entity = create_data.get("entity_id")
        assert automation_entity, f"no entity_id: {create_data}"
        cleanup_tracker.track("automation", automation_entity)

        # 2. GET — real hash
        await wait_for_automation(mcp_client, automation_entity)
        get_result = await mcp_client.call_tool(
            "ha_config_get_automation", {"identifier": automation_entity}
        )
        get_data = parse_mcp_result(get_result)
        real_hash = get_data.get("config_hash")
        assert real_hash, f"no config_hash: {get_data}"

        # 3. UPDATE with correct hash — must succeed
        update_config = test_data_factory.automation_config(
            "A6 Hash Valid Test Updated",
            trigger=[{"platform": "time", "at": "06:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "identifier": automation_entity,
                "config": update_config,
                "config_hash": real_hash,
            },
        )
        assert result.get("success") is True, f"expected success with correct hash: {result}"
        logger.info("Correct hash accepted — update succeeded")

    async def test_update_without_hash_succeeds(
        self, mcp_client, cleanup_tracker, test_data_factory
    ) -> None:
        """Update without config_hash is allowed (hash check is opt-in).

        Guard code: `if identifier and config_hash:` — omitting config_hash
        skips the optimistic-lock check entirely, enabling unconditional overwrites.
        """
        # 1. CREATE
        test_light = await _find_test_light_entity(mcp_client)
        config = test_data_factory.automation_config(
            "A6 No Hash Test",
            trigger=[{"platform": "time", "at": "07:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )
        create_data = await safe_call_tool(
            mcp_client, "ha_config_set_automation", {"config": config}
        )
        assert create_data.get("success"), f"create failed: {create_data}"
        automation_entity = create_data.get("entity_id")
        assert automation_entity, f"no entity_id: {create_data}"
        cleanup_tracker.track("automation", automation_entity)

        # 2. UPDATE without config_hash — guard skipped, must succeed
        await wait_for_automation(mcp_client, automation_entity)
        update_config = test_data_factory.automation_config(
            "A6 No Hash Test Updated",
            trigger=[{"platform": "time", "at": "08:00:00"}],
            action=[{"service": "light.turn_on", "target": {"entity_id": test_light}}],
        )
        result = await safe_call_tool(
            mcp_client,
            "ha_config_set_automation",
            {
                "identifier": automation_entity,
                "config": update_config,
                # config_hash intentionally omitted — guard must not fire
            },
        )
        assert result.get("success") is True, (
            f"update without config_hash should succeed: {result}"
        )
        logger.info("Update without config_hash succeeded — guard correctly skipped")
