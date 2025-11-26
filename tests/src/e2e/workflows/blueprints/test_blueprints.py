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

from ...utilities.assertions import MCPAssertions

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
