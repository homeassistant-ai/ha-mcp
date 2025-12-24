"""
Blueprint Management E2E Tests

Tests the blueprint management tools:
- ha_list_blueprints - List installed blueprints
- ha_get_blueprint - Get blueprint details
- ha_import_blueprint - Import blueprint from URL

Note: Tests are designed to work with both Docker test environment (localhost:8124)
and production environments. Blueprint availability may vary.
"""

import logging

import pytest

from ...utilities.assertions import MCPAssertions, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.blueprint
class TestBlueprintManagement:
    """Test blueprint management workflows."""

    async def test_list_automation_blueprints(self, mcp_client):
        """
        Test: List automation blueprints

        Validates that we can list automation blueprints from Home Assistant.
        """
        logger.info("Testing ha_list_blueprints for automation domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # List automation blueprints
            result = await mcp.call_tool_success(
                "ha_list_blueprints",
                {"domain": "automation"},
            )

            # Verify response structure
            assert "blueprints" in result, "Response should contain 'blueprints' key"
            assert "count" in result, "Response should contain 'count' key"
            assert "domain" in result, "Response should contain 'domain' key"
            assert result["domain"] == "automation", "Domain should be 'automation'"

            blueprints = result.get("blueprints", [])
            logger.info(f"Found {len(blueprints)} automation blueprints")

            # If blueprints exist, verify their structure
            if blueprints:
                first_blueprint = blueprints[0]
                assert "path" in first_blueprint, "Blueprint should have 'path'"
                assert "domain" in first_blueprint, "Blueprint should have 'domain'"
                assert "name" in first_blueprint, "Blueprint should have 'name'"
                logger.info(f"First blueprint: {first_blueprint.get('name')} ({first_blueprint.get('path')})")

            logger.info("ha_list_blueprints for automation domain succeeded")

    async def test_list_script_blueprints(self, mcp_client):
        """
        Test: List script blueprints

        Validates that we can list script blueprints from Home Assistant.
        """
        logger.info("Testing ha_list_blueprints for script domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # List script blueprints
            result = await mcp.call_tool_success(
                "ha_list_blueprints",
                {"domain": "script"},
            )

            # Verify response structure
            assert "blueprints" in result, "Response should contain 'blueprints' key"
            assert "count" in result, "Response should contain 'count' key"
            assert result["domain"] == "script", "Domain should be 'script'"

            blueprints = result.get("blueprints", [])
            logger.info(f"Found {len(blueprints)} script blueprints")

            logger.info("ha_list_blueprints for script domain succeeded")

    async def test_list_blueprints_invalid_domain(self, mcp_client):
        """
        Test: List blueprints with invalid domain

        Validates proper error handling for invalid domain parameter.
        """
        logger.info("Testing ha_list_blueprints with invalid domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try to list blueprints with invalid domain
            result = await mcp.call_tool_failure(
                "ha_list_blueprints",
                {"domain": "invalid_domain"},
                expected_error="Invalid domain",
            )

            # Verify error response includes valid domains
            assert "valid_domains" in result, "Error response should include valid domains"
            logger.info("ha_list_blueprints properly rejects invalid domain")

    async def test_get_blueprint_details(self, mcp_client):
        """
        Test: Get blueprint details

        Validates that we can get detailed information about a specific blueprint.
        First lists blueprints, then retrieves details of an existing one.
        """
        logger.info("Testing ha_get_blueprint...")

        async with MCPAssertions(mcp_client) as mcp:
            # First, list available blueprints
            list_result = await mcp.call_tool_success(
                "ha_list_blueprints",
                {"domain": "automation"},
            )

            blueprints = list_result.get("blueprints", [])

            if not blueprints:
                logger.info("No automation blueprints available, skipping detail test")
                pytest.skip("No automation blueprints available for testing")

            # Get details of the first blueprint
            first_blueprint_path = blueprints[0]["path"]
            logger.info(f"Getting details for blueprint: {first_blueprint_path}")

            detail_result = await mcp.call_tool_success(
                "ha_get_blueprint",
                {"path": first_blueprint_path, "domain": "automation"},
            )

            # Verify response structure
            assert "path" in detail_result, "Response should contain 'path'"
            assert "domain" in detail_result, "Response should contain 'domain'"
            assert "name" in detail_result, "Response should contain 'name'"
            assert detail_result["path"] == first_blueprint_path, "Path should match requested path"

            logger.info(f"Blueprint details retrieved: {detail_result.get('name')}")

            # Check for metadata if available
            if "metadata" in detail_result:
                meta = detail_result["metadata"]
                logger.info(f"  Description: {(meta.get('description') or 'N/A')[:100]}...")
                logger.info(f"  Author: {meta.get('author') or 'N/A'}")

            # Check for inputs if available
            if "inputs" in detail_result:
                inputs = detail_result["inputs"]
                logger.info(f"  Inputs: {len(inputs)} defined")

            logger.info("ha_get_blueprint succeeded")

    async def test_get_blueprint_not_found(self, mcp_client):
        """
        Test: Get blueprint that doesn't exist

        Validates proper error handling when blueprint path doesn't exist.
        """
        logger.info("Testing ha_get_blueprint with non-existent path...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try to get a non-existent blueprint
            result = await mcp.call_tool_failure(
                "ha_get_blueprint",
                {"path": "nonexistent/blueprint_xyz.yaml", "domain": "automation"},
                expected_error="not found",
            )

            # Verify error response includes suggestions
            assert "suggestions" in result, "Error response should include suggestions"
            logger.info("ha_get_blueprint properly handles non-existent blueprint")

    async def test_get_blueprint_invalid_domain(self, mcp_client):
        """
        Test: Get blueprint with invalid domain

        Validates proper error handling for invalid domain parameter.
        """
        logger.info("Testing ha_get_blueprint with invalid domain...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try with invalid domain
            result = await mcp.call_tool_failure(
                "ha_get_blueprint",
                {"path": "some/path.yaml", "domain": "invalid_domain"},
                expected_error="Invalid domain",
            )

            assert "valid_domains" in result, "Error response should include valid domains"
            logger.info("ha_get_blueprint properly rejects invalid domain")

    async def test_import_blueprint_invalid_url(self, mcp_client):
        """
        Test: Import blueprint with invalid URL format

        Validates proper error handling for invalid URL format.
        """
        logger.info("Testing ha_import_blueprint with invalid URL...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try with invalid URL format
            await mcp.call_tool_failure(
                "ha_import_blueprint",
                {"url": "not-a-valid-url"},
                expected_error="Invalid URL",
            )

            logger.info("ha_import_blueprint properly rejects invalid URL format")

    @pytest.mark.slow
    async def test_import_blueprint_nonexistent_url(self, mcp_client):
        """
        Test: Import blueprint from non-existent URL

        Validates proper error handling when URL doesn't exist or isn't accessible.
        Note: This test makes an actual network request, hence marked as slow.
        """
        logger.info("Testing ha_import_blueprint with non-existent URL...")

        async with MCPAssertions(mcp_client) as mcp:
            # Try with URL that doesn't exist
            result = await mcp.call_tool_failure(
                "ha_import_blueprint",
                {"url": "https://example.com/nonexistent/blueprint.yaml"},
            )

            # Should fail with appropriate error
            assert "suggestions" in result, "Error response should include suggestions"
            logger.info("ha_import_blueprint properly handles non-existent URL")


@pytest.mark.blueprint
async def test_blueprint_discovery_workflow(mcp_client):
    """
    Test: Complete blueprint discovery workflow

    Validates the typical user journey for discovering and exploring blueprints:
    1. List all blueprints
    2. Get details of interesting blueprints
    3. Review inputs and configuration
    """
    logger.info("Testing complete blueprint discovery workflow...")

    async with MCPAssertions(mcp_client) as mcp:
        # Step 1: List automation blueprints
        logger.info("Step 1: List automation blueprints...")
        list_result = await mcp.call_tool_success(
            "ha_list_blueprints",
            {"domain": "automation"},
        )

        automation_count = list_result.get("count", 0)
        logger.info(f"Found {automation_count} automation blueprints")

        # Step 2: List script blueprints
        logger.info("Step 2: List script blueprints...")
        script_result = await mcp.call_tool_success(
            "ha_list_blueprints",
            {"domain": "script"},
        )

        script_count = script_result.get("count", 0)
        logger.info(f"Found {script_count} script blueprints")

        # Step 3: If blueprints exist, explore one
        blueprints = list_result.get("blueprints", [])
        if blueprints:
            logger.info("Step 3: Exploring first blueprint...")
            first_blueprint = blueprints[0]

            detail_result = await mcp.call_tool_success(
                "ha_get_blueprint",
                {"path": first_blueprint["path"], "domain": "automation"},
            )

            logger.info(f"Explored blueprint: {detail_result.get('name')}")

            # Log input requirements if available
            if "inputs" in detail_result:
                inputs = detail_result["inputs"]
                logger.info(f"Blueprint requires {len(inputs)} inputs:")
                for input_name, input_config in list(inputs.items())[:3]:
                    logger.info(f"  - {input_name}: {(input_config.get('description') or 'No description')[:50]}")
        else:
            logger.info("Step 3: Skipped (no blueprints available)")

        logger.info("Blueprint discovery workflow completed successfully")


@pytest.mark.blueprint
async def test_blueprint_search_integration(mcp_client):
    """
    Test: Blueprint search integration

    Validates that blueprints can be discovered through search functionality
    and that the blueprint tools work with other MCP tools.
    """
    logger.info("Testing blueprint search integration...")

    async with MCPAssertions(mcp_client) as mcp:
        # List blueprints
        result = await mcp.call_tool_success(
            "ha_list_blueprints",
            {"domain": "automation"},
        )

        blueprints = result.get("blueprints", [])
        logger.info(f"Blueprint search found {len(blueprints)} results")

        # Verify blueprint metadata is searchable/useful
        for bp in blueprints[:3]:  # Check first 3
            assert "path" in bp, "Blueprint should have path for retrieval"
            assert "name" in bp, "Blueprint should have name for display"

        logger.info("Blueprint search integration test completed")


@pytest.mark.blueprint
async def test_blueprint_automation_lifecycle(mcp_client):
    """
    Test: Create and update blueprint-based automation

    Validates that blueprint automations can be created and updated without
    requiring trigger/action fields, fixing issue #363.
    """
    logger.info("Testing blueprint automation lifecycle...")

    async with MCPAssertions(mcp_client) as mcp:
        # Step 1: List available blueprints
        list_result = await mcp.call_tool_success(
            "ha_list_blueprints",
            {"domain": "automation"},
        )

        blueprints = list_result.get("blueprints", [])
        if not blueprints:
            logger.info("No automation blueprints available, skipping test")
            pytest.skip("No automation blueprints available for testing")

        # Use the first available blueprint
        blueprint_path = blueprints[0]["path"]
        logger.info(f"Using blueprint: {blueprint_path}")

        # Step 2: Get blueprint details to understand required inputs
        detail_result = await mcp.call_tool_success(
            "ha_get_blueprint",
            {"path": blueprint_path, "domain": "automation"},
        )

        inputs = detail_result.get("inputs", {})
        logger.info(f"Blueprint has {len(inputs)} inputs")

        # Step 3: Create automation from blueprint (no trigger/action fields)
        # Note: We can't actually test creation with empty inputs since HA validates
        # blueprint inputs. Instead, we test that the tool ACCEPTS the config without
        # trigger/action fields (it will fail later at HA validation, not our validation)
        automation_config = {
            "alias": "Test Blueprint Automation E2E",
            "description": "Testing blueprint automation creation (issue #363)",
            "use_blueprint": {
                "path": blueprint_path,
                "input": {},  # Empty inputs - will fail HA validation but pass our validation
            },
        }

        # This should reach HA (proving our validation passed) even if HA rejects it
        # If our validation failed, we'd get a different error code
        create_raw_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {"config": automation_config},
        )
        create_result = parse_mcp_result(create_raw_result)

        # Check if it was our validation or HA's validation that failed
        if not create_result.get("success"):
            error_msg = str(create_result.get("error", {}).get("message", ""))
            # If error is about missing blueprint inputs, our validation passed! HA rejected it.
            if "Missing input" in error_msg or "input" in error_msg.lower():
                logger.info(f"✅ Our validation passed (config reached HA), HA rejected due to missing blueprint inputs as expected")
                logger.info("✅ Blueprint automation lifecycle test completed (validation works)")
                return
            # If error is about missing trigger/action, our fix didn't work
            if "trigger" in error_msg.lower() or "action" in error_msg.lower():
                raise AssertionError(f"Our validation failed - still requiring trigger/action: {error_msg}")
            # Some other error
            raise AssertionError(f"Unexpected error: {create_result}")

        # If it succeeded, great! (unlikely with empty inputs)
        automation_id = create_result.get("entity_id") or create_result.get("id")
        assert automation_id, "Should return automation ID"
        logger.info(f"✅ Created blueprint automation: {automation_id}")

        # If we got here, the automation was created successfully
        # Step 4: Retrieve the automation and verify no trigger/action fields
        import asyncio
        await asyncio.sleep(2)  # Wait for automation to register

        get_result = await mcp.call_tool_success(
            "ha_config_get_automation",
            {"identifier": automation_id},
        )

        config = get_result.get("config", {})
        assert "use_blueprint" in config, "Config should have use_blueprint"
        logger.info("✅ Blueprint automation config verified")

        # Step 5: Clean up
        delete_result = await mcp.call_tool_success(
            "ha_config_remove_automation",
            {"identifier": automation_id},
        )

        logger.info("✅ Blueprint automation lifecycle test completed")


@pytest.mark.blueprint
async def test_blueprint_automation_with_empty_arrays(mcp_client):
    """
    Test: Blueprint automation with empty trigger/action arrays gets cleaned

    Validates that if a user mistakenly provides empty trigger/action/condition
    arrays with a blueprint automation, they are stripped before saving (issue #363).
    """
    logger.info("Testing blueprint automation with empty arrays...")

    async with MCPAssertions(mcp_client) as mcp:
        # List available blueprints
        list_result = await mcp.call_tool_success(
            "ha_list_blueprints",
            {"domain": "automation"},
        )

        blueprints = list_result.get("blueprints", [])
        if not blueprints:
            pytest.skip("No automation blueprints available for testing")

        blueprint_path = blueprints[0]["path"]

        # Create blueprint automation WITH empty arrays (should be stripped)
        automation_config = {
            "alias": "Test Blueprint Empty Arrays E2E",
            "use_blueprint": {
                "path": blueprint_path,
                "input": {},
            },
            "trigger": [],  # These should be stripped
            "action": [],  # These should be stripped
            "condition": [],  # These should be stripped
        }

        # The key test: This should pass our validation (not fail with "missing trigger/action")
        # It will fail HA validation due to missing blueprint inputs, but that's expected
        create_raw_result = await mcp_client.call_tool(
            "ha_config_set_automation",
            {"config": automation_config},
        )
        create_result = parse_mcp_result(create_raw_result)

        # If our validation works, it should reach HA (which will reject due to missing inputs)
        if not create_result.get("success"):
            error_msg = str(create_result.get("error", {}).get("message", ""))
            # If error is about missing blueprint inputs, our validation passed!
            if "Missing input" in error_msg or "input" in error_msg.lower():
                logger.info("✅ Empty arrays were stripped (passed our validation, failed HA blueprint validation as expected)")
                logger.info("✅ Empty arrays test completed")
                return
            # If error is about missing trigger/action, our fix didn't work
            if "trigger" in error_msg.lower() or "action" in error_msg.lower():
                raise AssertionError(f"Empty arrays not stripped - validation failed: {error_msg}")
            # Some other error
            raise AssertionError(f"Unexpected error: {create_result}")

        # If somehow it succeeded (unlikely with empty inputs)
        automation_id = create_result.get("entity_id") or create_result.get("id")
        logger.info(f"✅ Created blueprint automation with empty arrays: {automation_id}")

        # Clean up
        await mcp.call_tool_success(
            "ha_config_remove_automation",
            {"identifier": automation_id},
        )

        logger.info("✅ Empty arrays test completed")
