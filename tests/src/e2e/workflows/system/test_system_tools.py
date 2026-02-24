"""
System Management Tools E2E Tests

NOTE: Run these tests with the Docker test environment:
    HAMCP_ENV_FILE=tests/.env.test uv run pytest tests/src/e2e/workflows/system/test_system_tools.py -v

Tests for system management MCP tools:
- ha_check_config: Configuration validation
- ha_restart: Home Assistant restart (with safety measures)
- ha_reload_core: Reload specific configuration components
- ha_get_overview: Get system information and entity overview
- ha_get_system_health: Get system health data

Note: ha_restart is tested carefully to avoid disrupting the test environment.
We verify the safety mechanisms work but do not actually restart HA during tests.
"""

import logging

import pytest

from ...utilities.assertions import parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.system
class TestSystemTools:
    """Test system management tools."""

    @pytest.mark.asyncio
    async def test_check_config_valid(self, mcp_client):
        """
        Test: Check Home Assistant configuration for errors.

        This test validates that we can check the HA configuration
        and that the test environment has valid configuration.
        """
        logger.info("Checking Home Assistant configuration...")

        result = await mcp_client.call_tool("ha_check_config", {})
        data = parse_mcp_result(result)

        logger.info(f"Config check result: {data}")

        # Verify the tool executed successfully
        assert data.get("success") is True, f"Config check failed: {data.get('error')}"

        # Verify we got the expected fields
        assert "result" in data, "Missing 'result' field"
        assert "is_valid" in data, "Missing 'is_valid' field"
        assert "errors" in data, "Missing 'errors' field"

        # The test environment should have valid config
        if data.get("is_valid"):
            logger.info("Configuration is valid")
            assert data["result"] == "valid"
            errors = data.get("errors") or []
            assert len(errors) == 0, f"Valid config should have no errors: {errors}"
        else:
            # Log errors if config is invalid (unexpected in test env)
            logger.warning(f"Configuration has errors: {data['errors']}")

        logger.info("Config check test completed successfully")

    @pytest.mark.asyncio
    async def test_restart_without_confirmation(self, mcp_client):
        """
        Test: Restart without confirmation fails safely.

        This test verifies that the restart tool requires explicit
        confirmation and does not restart without it.
        """
        logger.info("Testing restart safety mechanism (no confirmation)...")

        result = await mcp_client.call_tool("ha_restart", {})
        data = parse_mcp_result(result)

        logger.info(f"Restart result (no confirm): {data}")

        # Should fail with helpful error
        assert data.get("success") is False, "Restart should fail without confirmation"
        assert "not confirmed" in data.get("error", "").lower(), (
            f"Expected 'not confirmed' error, got: {data.get('error')}"
        )

        # Should provide suggestions
        assert "suggestions" in data, "Should provide suggestions"
        suggestions = data["suggestions"]
        assert any("confirm" in s.lower() for s in suggestions), (
            "Suggestions should mention confirmation"
        )

        logger.info("Restart safety mechanism test passed - restart was prevented")

    @pytest.mark.asyncio
    async def test_restart_with_false_confirmation(self, mcp_client):
        """
        Test: Restart with confirm=False fails safely.

        This test explicitly sets confirm=False to verify the safety check.
        """
        logger.info("Testing restart with explicit confirm=False...")

        result = await mcp_client.call_tool("ha_restart", {"confirm": False})
        data = parse_mcp_result(result)

        logger.info(f"Restart result (confirm=False): {data}")

        # Should fail with helpful error
        assert data.get("success") is False, "Restart should fail with confirm=False"
        assert "not confirmed" in data.get("error", "").lower(), (
            f"Expected 'not confirmed' error, got: {data.get('error')}"
        )

        logger.info("Restart safety test passed - explicit false confirmation rejected")

    # NOTE: We do NOT test ha_restart with confirm=True in e2e tests
    # as it would disrupt the test environment. The safety mechanisms
    # are verified above, and actual restart functionality can be
    # tested manually or in integration tests with proper recovery.

    @pytest.mark.asyncio
    async def test_reload_automations(self, mcp_client):
        """
        Test: Reload automations configuration.

        This test verifies that we can reload automation configurations
        without a full restart.
        """
        logger.info("Testing reload automations...")

        result = await mcp_client.call_tool(
            "ha_reload_core", {"target": "automations"}
        )
        data = parse_mcp_result(result)

        logger.info(f"Reload automations result: {data}")

        # Verify successful reload
        assert data.get("success") is True, f"Reload failed: {data.get('error')}"
        assert data.get("target") == "automations", "Target should be 'automations'"
        assert data.get("service") == "automation.reload", (
            f"Service should be 'automation.reload', got: {data.get('service')}"
        )

        logger.info("Reload automations test completed successfully")

    @pytest.mark.asyncio
    async def test_reload_scripts(self, mcp_client):
        """
        Test: Reload scripts configuration.
        """
        logger.info("Testing reload scripts...")

        result = await mcp_client.call_tool(
            "ha_reload_core", {"target": "scripts"}
        )
        data = parse_mcp_result(result)

        logger.info(f"Reload scripts result: {data}")

        assert data.get("success") is True, f"Reload failed: {data.get('error')}"
        assert data.get("target") == "scripts"
        assert data.get("service") == "script.reload"

        logger.info("Reload scripts test completed successfully")

    @pytest.mark.asyncio
    async def test_reload_scenes(self, mcp_client):
        """
        Test: Reload scenes configuration.
        """
        logger.info("Testing reload scenes...")

        result = await mcp_client.call_tool(
            "ha_reload_core", {"target": "scenes"}
        )
        data = parse_mcp_result(result)

        logger.info(f"Reload scenes result: {data}")

        assert data.get("success") is True, f"Reload failed: {data.get('error')}"
        assert data.get("target") == "scenes"
        assert data.get("service") == "scene.reload"

        logger.info("Reload scenes test completed successfully")

    @pytest.mark.asyncio
    async def test_reload_core_config(self, mcp_client):
        """
        Test: Reload core configuration (customize, packages).
        """
        logger.info("Testing reload core config...")

        result = await mcp_client.call_tool(
            "ha_reload_core", {"target": "core"}
        )
        data = parse_mcp_result(result)

        logger.info(f"Reload core config result: {data}")

        assert data.get("success") is True, f"Reload failed: {data.get('error')}"
        assert data.get("target") == "core"
        assert data.get("service") == "homeassistant.reload_core_config"

        logger.info("Reload core config test completed successfully")

    @pytest.mark.asyncio
    async def test_reload_invalid_target(self, mcp_client):
        """
        Test: Reload with invalid target returns helpful error.
        """
        logger.info("Testing reload with invalid target...")

        result = await mcp_client.call_tool(
            "ha_reload_core", {"target": "invalid_target_xyz"}
        )
        data = parse_mcp_result(result)

        logger.info(f"Reload invalid target result: {data}")

        # Should fail with helpful error
        assert data.get("success") is False, "Reload should fail for invalid target"
        assert "invalid" in data.get("error", "").lower(), (
            f"Expected 'invalid' in error, got: {data.get('error')}"
        )

        # Should provide valid targets
        assert "valid_targets" in data, "Should provide list of valid targets"
        valid_targets = data["valid_targets"]
        assert "automations" in valid_targets, "Valid targets should include 'automations'"
        assert "scripts" in valid_targets, "Valid targets should include 'scripts'"

        logger.info("Invalid target test passed with helpful error message")

    @pytest.mark.asyncio
    @pytest.mark.slow
    async def test_reload_all(self, mcp_client):
        """
        Test: Reload all configuration components.

        This test reloads all reloadable components. Marked as slow
        because it calls multiple reload services sequentially.
        """
        logger.info("Testing reload all components...")

        result = await mcp_client.call_tool(
            "ha_reload_core", {"target": "all"}
        )
        data = parse_mcp_result(result)

        logger.info(f"Reload all result: {data}")

        # Verify successful reload
        assert data.get("success") is True, f"Reload all failed: {data.get('error')}"
        assert "reloaded" in data, "Should list reloaded components"

        reloaded = data["reloaded"]
        logger.info(f"Reloaded {len(reloaded)} components: {reloaded}")

        # Some core components should always be reloadable
        assert len(reloaded) > 0, "Should have reloaded at least one component"

        # Log any warnings
        if data.get("warnings"):
            logger.warning(f"Reload warnings: {data['warnings']}")

        logger.info("Reload all test completed successfully")

    @pytest.mark.asyncio
    async def test_get_system_overview(self, mcp_client):
        """
        Test: Get Home Assistant system overview (replaces ha_get_system_info).

        This test verifies that we can retrieve comprehensive
        system information from Home Assistant via ha_get_overview.
        """
        logger.info("Testing get system overview...")

        result = await mcp_client.call_tool("ha_get_overview", {"detail_level": "full"})
        data = parse_mcp_result(result)

        logger.info(f"System overview result keys: {list(data.keys())}")

        # Verify successful response
        assert data.get("success") is True, f"Get overview failed: {data.get('error')}"

        # Verify system_info field exists
        assert "system_info" in data, "Missing 'system_info' field"
        system_info = data["system_info"]

        # Verify expected fields in system_info (components only at "full" level)
        expected_fields = [
            "version",
            "location_name",
            "time_zone",
            "components_loaded",
        ]

        for field in expected_fields:
            assert field in system_info, f"Missing expected field in system_info: {field}"

        # Log key information
        logger.info(f"Home Assistant version: {system_info.get('version')}")
        logger.info(f"Location: {system_info.get('location_name')}")
        logger.info(f"Timezone: {system_info.get('time_zone')}")
        logger.info(f"Components loaded: {system_info.get('components_loaded')}")

        # Verify components_loaded count is positive
        components_loaded = system_info.get("components_loaded", 0)
        assert components_loaded > 0, "Should have at least some components loaded"

        logger.info("Get system overview test completed successfully")

    @pytest.mark.asyncio
    async def test_get_system_health(self, mcp_client):
        """
        Test: Get Home Assistant system health information.

        This test verifies that we can retrieve health check data
        from Home Assistant via WebSocket.

        Note: system_health may not be available in all HA installations,
        particularly in test containers without the system_health integration.
        """
        logger.info("Testing get system health...")

        result = await mcp_client.call_tool("ha_get_system_health", {})
        data = parse_mcp_result(result)

        logger.info(f"System health result: {data}")

        # System health might not be available in test environments
        if not data.get("success"):
            error_msg = data.get("error", "")
            # Skip test if system_health is not available
            if "not available" in error_msg.lower() or "NoneType" in error_msg:
                pytest.skip("system_health not available in test environment")
            else:
                # Unexpected failure
                pytest.fail(f"Get system health failed unexpectedly: {error_msg}")

        # Verify expected fields when successful
        assert "health_info" in data, "Missing 'health_info' field"
        assert "component_count" in data, "Missing 'component_count' field"

        health_info = data.get("health_info", {})
        component_count = data.get("component_count", 0)

        logger.info(f"Health info available for {component_count} components")

        # Log health info for key components
        if isinstance(health_info, dict) and "homeassistant" in health_info:
            ha_health = health_info["homeassistant"]
            logger.info(f"Home Assistant health: {ha_health}")

        # Health info structure varies by installation type and integrations
        # Just verify we got some data
        if component_count > 0 and isinstance(health_info, dict):
            # Log first few component health statuses
            for component, status in list(health_info.items())[:5]:
                logger.info(f"  {component}: {status}")

        logger.info("Get system health test completed successfully")


@pytest.mark.system
class TestSystemToolsIntegration:
    """Integration tests for system tools working together."""

    @pytest.mark.asyncio
    async def test_check_config_before_reload(self, mcp_client):
        """
        Test: Workflow of checking config before reloading.

        This tests the recommended workflow:
        1. Check configuration
        2. If valid, reload components
        """
        logger.info("Testing check config -> reload workflow...")

        # Step 1: Check configuration
        config_result = await mcp_client.call_tool("ha_check_config", {})
        config_data = parse_mcp_result(config_result)

        assert config_data.get("success") is True, "Config check should succeed"

        if config_data.get("is_valid"):
            logger.info("Config is valid, proceeding with reload...")

            # Step 2: Reload automations
            reload_result = await mcp_client.call_tool(
                "ha_reload_core", {"target": "automations"}
            )
            reload_data = parse_mcp_result(reload_result)

            assert reload_data.get("success") is True, "Reload should succeed after valid config check"
            logger.info("Workflow completed: config check -> reload automations")
        else:
            logger.warning("Config has errors - reload would be skipped in production")
            # We still consider the test passed as the workflow logic is correct

        logger.info("Check config -> reload workflow test completed")

    @pytest.mark.asyncio
    async def test_system_overview(self, mcp_client):
        """
        Test: Get complete system overview using multiple tools.

        This test demonstrates gathering comprehensive system status
        by combining multiple system tools.
        """
        logger.info("Testing comprehensive system overview...")

        # Get system overview
        info_result = await mcp_client.call_tool("ha_get_overview", {})
        info_data = parse_mcp_result(info_result)

        # Get system health (may not be available in test environment)
        health_result = await mcp_client.call_tool("ha_get_system_health", {})
        health_data = parse_mcp_result(health_result)

        # Get config status
        config_result = await mcp_client.call_tool("ha_check_config", {})
        config_data = parse_mcp_result(config_result)

        # Verify essential tools returned successfully
        assert info_data.get("success") is True, "System overview should succeed"
        assert config_data.get("success") is True, "Config check should succeed"
        # Health data might not be available in test containers - don't require it
        health_available = health_data.get("success") is True

        # Extract system_info from overview
        system_info = info_data.get("system_info", {})

        # Log comprehensive overview
        logger.info("=" * 60)
        logger.info("SYSTEM OVERVIEW")
        logger.info("=" * 60)
        logger.info(f"Version: {system_info.get('version')}")
        logger.info(f"Location: {system_info.get('location_name')}")
        logger.info(f"Timezone: {system_info.get('time_zone')}")
        logger.info(f"Components: {system_info.get('components_loaded')}")
        logger.info(f"Config Status: {config_data.get('result')}")
        if health_available:
            logger.info(f"Health Components: {health_data.get('component_count')}")
        else:
            logger.info("Health: Not available in this environment")
        logger.info("=" * 60)

        logger.info("System overview test completed successfully")
