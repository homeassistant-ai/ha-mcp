"""
End-to-End tests for Home Assistant Update Management tools.

This test suite validates the update management tools including:
- ha_list_updates: List all available updates
- ha_get_release_notes: Get release notes for an update
- ha_get_system_version: Get system version and info

Tests are designed for Docker Home Assistant test environment.
"""

import logging

import pytest

# Import test utilities
from ...utilities.assertions import MCPAssertions

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@pytest.mark.updates
class TestUpdateManagement:
    """Test suite for update management tools."""

    async def test_list_updates_basic(self, mcp_client):
        """
        Test: Basic listing of available updates.

        Validates that ha_list_updates returns the expected structure
        even when no updates are available (test environment typically up-to-date).
        """
        logger.info("Testing basic update listing...")

        async with MCPAssertions(mcp_client) as mcp:
            # Call ha_list_updates
            result = await mcp.call_tool_success(
                "ha_list_updates",
                {"include_skipped": False},
            )

            # Verify response structure
            assert "success" in result, f"Missing 'success' field in result: {result}"
            assert result.get("success") is True, f"Expected success=True: {result}"
            assert (
                "updates_available" in result
            ), f"Missing 'updates_available' field: {result}"
            assert "updates" in result, f"Missing 'updates' field: {result}"
            assert "categories" in result, f"Missing 'categories' field: {result}"

            # updates_available should be a non-negative integer
            updates_count = result.get("updates_available", -1)
            assert (
                isinstance(updates_count, int) and updates_count >= 0
            ), f"Invalid updates_available value: {updates_count}"

            logger.info(f"Found {updates_count} available updates")

            # Verify updates list structure if any updates exist
            updates_list = result.get("updates", [])
            assert isinstance(
                updates_list, list
            ), f"Updates should be a list: {type(updates_list)}"

            if updates_list:
                # Check first update has expected fields
                first_update = updates_list[0]
                expected_fields = [
                    "entity_id",
                    "title",
                    "installed_version",
                    "latest_version",
                    "category",
                ]
                for field in expected_fields:
                    assert (
                        field in first_update
                    ), f"Update missing field '{field}': {first_update}"
                logger.info(f"First update: {first_update.get('title')}")

            # Verify categories structure
            categories = result.get("categories", {})
            assert isinstance(
                categories, dict
            ), f"Categories should be a dict: {type(categories)}"

            # Valid category keys
            valid_categories = {
                "core",
                "os",
                "supervisor",
                "addons",
                "hacs",
                "devices",
                "other",
            }
            for cat_key in categories.keys():
                assert (
                    cat_key in valid_categories
                ), f"Unknown category '{cat_key}': {categories.keys()}"

            logger.info("Basic update listing test passed")

    async def test_list_updates_with_skipped(self, mcp_client):
        """
        Test: Listing updates with skipped updates included.

        Validates that include_skipped parameter works correctly.
        """
        logger.info("Testing update listing with skipped updates...")

        async with MCPAssertions(mcp_client) as mcp:
            # Call with include_skipped=True
            result_with_skipped = await mcp.call_tool_success(
                "ha_list_updates",
                {"include_skipped": True},
            )

            # Call without skipped (default)
            result_without_skipped = await mcp.call_tool_success(
                "ha_list_updates",
                {"include_skipped": False},
            )

            # Verify both succeeded
            assert result_with_skipped.get("success") is True
            assert result_without_skipped.get("success") is True

            # include_skipped flag should be reflected in response
            assert result_with_skipped.get("include_skipped") is True
            assert result_without_skipped.get("include_skipped") is False

            # skipped_count should be present
            assert "skipped_count" in result_with_skipped
            assert "skipped_count" in result_without_skipped

            logger.info(
                f"With skipped: {len(result_with_skipped.get('updates', []))} updates, "
                f"Without skipped: {len(result_without_skipped.get('updates', []))} updates"
            )
            logger.info("Skipped updates test passed")

    async def test_get_system_version(self, mcp_client):
        """
        Test: Get system version and configuration info.

        Validates that ha_get_system_version returns expected system information.
        """
        logger.info("Testing system version retrieval...")

        async with MCPAssertions(mcp_client) as mcp:
            # Call ha_get_system_version
            result = await mcp.call_tool_success(
                "ha_get_system_version",
                {},
            )

            # Verify response structure
            assert result.get("success") is True, f"Expected success=True: {result}"

            # Required fields
            assert "version" in result, f"Missing 'version' field: {result}"
            version = result.get("version")
            assert version is not None, "Version should not be None"
            assert isinstance(
                version, str
            ), f"Version should be string: {type(version)}"

            # Version format validation (should be like "2025.1.0")
            version_parts = version.split(".")
            assert (
                len(version_parts) >= 2
            ), f"Version should have at least 2 parts: {version}"

            logger.info(f"Home Assistant version: {version}")

            # Other expected fields
            optional_fields = [
                "location_name",
                "timezone",
                "config_dir",
                "components_loaded",
            ]
            for field in optional_fields:
                if field in result:
                    logger.info(f"  {field}: {result[field]}")

            # components_loaded should be a positive integer
            components = result.get("components_loaded")
            if components is not None:
                assert (
                    isinstance(components, int) and components > 0
                ), f"Invalid components_loaded: {components}"

            logger.info("System version test passed")

    async def test_get_release_notes_invalid_entity(self, mcp_client):
        """
        Test: Get release notes with invalid entity ID.

        Validates that ha_get_release_notes handles invalid entity IDs gracefully.
        """
        logger.info("Testing release notes with invalid entity...")

        async with MCPAssertions(mcp_client) as mcp:
            # Test with non-existent entity
            result = await mcp.call_tool_failure(
                "ha_get_release_notes",
                {"entity_id": "update.nonexistent_entity_xyz"},
                expected_error="not found",
            )
            logger.info("Non-existent entity test passed")

            # Test with invalid format (not starting with "update.")
            result = await mcp.call_tool_failure(
                "ha_get_release_notes",
                {"entity_id": "light.invalid_entity"},
                expected_error="Invalid entity_id format",
            )
            logger.info("Invalid format test passed")

    async def test_get_release_notes_for_update(self, mcp_client):
        """
        Test: Get release notes for an actual update entity.

        This test first lists updates to find available update entities,
        then attempts to get release notes for them.
        """
        logger.info("Testing release notes for actual update entities...")

        async with MCPAssertions(mcp_client) as mcp:
            # First, list all update entities
            list_result = await mcp.call_tool_success(
                "ha_list_updates",
                {"include_skipped": True},
            )

            updates = list_result.get("updates", [])

            if not updates:
                # No updates available, search for update entities directly
                search_result = await mcp.call_tool_success(
                    "ha_search_entities",
                    {"query": "update", "domain_filter": "update", "limit": 5},
                )

                search_data = search_result.get("data", search_result)
                results = search_data.get("results", [])

                if not results:
                    logger.info(
                        "No update entities found in test environment, skipping"
                    )
                    return

                # Use first found update entity
                entity_id = results[0].get("entity_id", "")
            else:
                # Use first available update
                entity_id = updates[0].get("entity_id", "")

            if not entity_id:
                logger.info("Could not find update entity to test, skipping")
                return

            logger.info(f"Testing release notes for: {entity_id}")

            # Get release notes for this entity
            result = await mcp.call_tool_success(
                "ha_get_release_notes",
                {"entity_id": entity_id},
            )

            # Verify response structure
            assert result.get("success") is True, f"Expected success=True: {result}"
            assert (
                result.get("entity_id") == entity_id
            ), f"Entity ID mismatch: {result.get('entity_id')} != {entity_id}"
            assert "version" in result, f"Missing 'version' field: {result}"

            # release_notes may be None if not available
            release_notes = result.get("release_notes")
            source = result.get("source")

            if release_notes:
                logger.info(f"Got release notes from source: {source}")
                logger.info(
                    f"Release notes preview: {release_notes[:200]}..."
                    if len(release_notes) > 200
                    else f"Release notes: {release_notes}"
                )
            else:
                logger.info(
                    f"No release notes available for {entity_id}: "
                    f"{result.get('message', 'No message')}"
                )

            logger.info("Release notes test passed")


@pytest.mark.updates
class TestUpdateToolsEdgeCases:
    """Test edge cases and error handling for update tools."""

    async def test_list_updates_response_consistency(self, mcp_client):
        """
        Test: Verify update listing response is consistent across calls.

        Multiple calls should return consistent structure and similar data.
        """
        logger.info("Testing update listing consistency...")

        async with MCPAssertions(mcp_client) as mcp:
            # Make two calls
            result1 = await mcp.call_tool_success("ha_list_updates", {})
            result2 = await mcp.call_tool_success("ha_list_updates", {})

            # Both should have same structure
            assert result1.get("success") == result2.get("success")
            assert "updates_available" in result1 and "updates_available" in result2
            assert "categories" in result1 and "categories" in result2

            # Update counts should be same or very similar
            # (small differences possible if update status changes between calls)
            count1 = result1.get("updates_available", 0)
            count2 = result2.get("updates_available", 0)
            assert (
                abs(count1 - count2) <= 1
            ), f"Update counts differ significantly: {count1} vs {count2}"

            logger.info("Consistency test passed")

    async def test_system_version_fields_presence(self, mcp_client):
        """
        Test: Verify all expected system version fields are present.

        Validates comprehensive field coverage in system version response.
        """
        logger.info("Testing system version field presence...")

        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success("ha_get_system_version", {})

            # Core required fields
            required_fields = ["success", "version"]
            for field in required_fields:
                assert (
                    field in result
                ), f"Required field '{field}' missing from result: {result.keys()}"

            # Common optional fields that should usually be present
            common_fields = [
                "location_name",
                "timezone",
                "components_loaded",
            ]
            present_fields = [f for f in common_fields if f in result]
            logger.info(f"Present optional fields: {present_fields}")

            # At least some optional fields should be present
            assert (
                len(present_fields) >= 1
            ), f"Expected at least 1 optional field present: {common_fields}"

            logger.info("Field presence test passed")

    async def test_update_categorization(self, mcp_client):
        """
        Test: Verify update entities are correctly categorized.

        Tests the categorization logic for different update types.
        """
        logger.info("Testing update categorization logic...")

        async with MCPAssertions(mcp_client) as mcp:
            result = await mcp.call_tool_success(
                "ha_list_updates", {"include_skipped": True}
            )

            updates = result.get("updates", [])
            categories = result.get("categories", {})

            # Every update in the list should have a category
            for update in updates:
                assert (
                    "category" in update
                ), f"Update missing category: {update.get('entity_id')}"
                category = update.get("category")
                assert category in {
                    "core",
                    "os",
                    "supervisor",
                    "addons",
                    "hacs",
                    "devices",
                    "other",
                }, f"Invalid category '{category}' for {update.get('entity_id')}"

            # Categories dict should only contain updates that match
            for cat_name, cat_updates in categories.items():
                for update in cat_updates:
                    assert update.get("category") == cat_name, (
                        f"Update {update.get('entity_id')} in category {cat_name} "
                        f"but has category {update.get('category')}"
                    )

            logger.info(
                f"Verified categorization for {len(updates)} updates "
                f"across {len(categories)} categories"
            )
            logger.info("Categorization test passed")


async def test_update_tools_discovery(mcp_client):
    """
    Test: Verify update tools are discoverable and registered.

    Validates that all three update tools are available in the MCP server.
    """
    logger.info("Testing update tools discovery...")

    # Get available tools - FastMCP returns a list directly
    tools = await mcp_client.list_tools()

    # Handle both list and object response types
    if hasattr(tools, "tools"):
        tool_list = tools.tools
    else:
        tool_list = tools

    # Convert to list of tool names
    tool_names = [tool.name for tool in tool_list]

    # Check that update tools are registered
    expected_tools = [
        "ha_list_updates",
        "ha_get_release_notes",
        "ha_get_system_version",
    ]

    for tool_name in expected_tools:
        assert tool_name in tool_names, (
            f"Tool '{tool_name}' not found in registered tools. "
            f"Available tools: {sorted(tool_names)}"
        )
        logger.info(f"Found tool: {tool_name}")

    logger.info("Update tools discovery test passed")
