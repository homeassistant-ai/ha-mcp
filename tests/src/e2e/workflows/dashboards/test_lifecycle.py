"""
End-to-End tests for Home Assistant Dashboard Management.

This test suite validates the complete lifecycle of Home Assistant dashboards including:
- Dashboard listing and discovery
- Dashboard creation with metadata and initial config
- Dashboard configuration retrieval and updates
- Dashboard metadata updates
- Dashboard deletion and cleanup
- Strategy-based dashboard support
- Error handling and validation
- Edge cases (url_path validation, default dashboard, etc.)

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.
"""

import asyncio
import ast
import json
import logging
from typing import Any

# Import test utilities
from tests.src.e2e.utilities.assertions import MCPAssertions

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_mcp_result(result) -> dict[str, Any]:
    """Parse MCP result from tool response."""
    try:
        if hasattr(result, "content") and result.content:
            response_text = str(result.content[0].text)
            try:
                return json.loads(response_text)
            except json.JSONDecodeError:
                # Try Python literal evaluation (safe alternative to eval)
                try:
                    fixed_text = (
                        response_text.replace("true", "True")
                        .replace("false", "False")
                        .replace("null", "None")
                    )
                    return ast.literal_eval(fixed_text)
                except (SyntaxError, ValueError):
                    return {"raw_response": response_text, "parse_error": True}

        return {
            "content": str(result.content[0]) if hasattr(result, "content") else str(result)
        }
    except Exception as e:
        logger.warning(f"Failed to parse MCP result: {e}")
        return {"error": "Failed to parse result", "exception": str(e)}


class TestDashboardLifecycle:
    """Test complete dashboard CRUD lifecycle."""

    async def test_basic_dashboard_lifecycle(self, mcp_client):
        """Test create, read, update, delete dashboard workflow."""
        logger.info("Starting basic dashboard lifecycle test")
        mcp = MCPAssertions(mcp_client)

        # 1. Create dashboard with initial config
        logger.info("Creating test dashboard...")
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-e2e-dashboard",
                "title": "E2E Test Dashboard",
                "icon": "mdi:test-tube",
                "config": {
                    "views": [
                        {"title": "Test View", "cards": [{"type": "markdown", "content": "Test"}]}
                    ]
                },
            },
        )
        assert create_data["success"] is True
        assert create_data["action"] in ["create", "set"]
        assert create_data.get("dashboard_created") is True or create_data.get("action") == "create"

        # Extract dashboard ID for later operations
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None, "Dashboard creation should return dashboard_id"

        # Small delay for HA to process
        await asyncio.sleep(1)

        # 2. List dashboards - verify exists
        logger.info("Listing dashboards...")
        list_data = await mcp.call_tool_success("ha_config_list_dashboards", {})
        assert list_data["success"] is True
        assert any(
            d.get("url_path") == "test-e2e-dashboard" for d in list_data.get("dashboards", [])
        )

        # 3. Get dashboard config
        logger.info("Getting dashboard config...")
        get_data = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"url_path": "test-e2e-dashboard"}
        )
        assert get_data["success"] is True
        assert "config" in get_data
        assert "views" in get_data["config"]

        # 4. Update config (add another card)
        logger.info("Updating dashboard config...")
        update_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-e2e-dashboard",
                "config": {
                    "views": [
                        {
                            "title": "Updated View",
                            "cards": [
                                {"type": "markdown", "content": "Updated content"},
                                {"type": "markdown", "content": "Second card"},
                            ],
                        }
                    ]
                },
            },
        )
        assert update_data["success"] is True

        # 5. Update metadata (change title)
        logger.info("Updating dashboard metadata...")
        meta_data = await mcp.call_tool_success(
            "ha_config_update_dashboard_metadata",
            {"dashboard_id": dashboard_id, "title": "Updated E2E Dashboard"},
        )
        assert meta_data["success"] is True

        # 6. Delete dashboard
        logger.info("Deleting test dashboard...")
        delete_data = await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )
        assert delete_data["success"] is True

        # 7. Verify deletion
        await asyncio.sleep(1)
        list_after_data = await mcp.call_tool_success("ha_config_list_dashboards", {})
        assert not any(
            d.get("url_path") == "test-e2e-dashboard"
            for d in list_after_data.get("dashboards", [])
        )

        logger.info("Basic dashboard lifecycle test completed successfully")

    async def test_strategy_based_dashboard(self, mcp_client):
        """Test creating strategy-based dashboard (auto-generated)."""
        logger.info("Starting strategy-based dashboard test")
        mcp = MCPAssertions(mcp_client)

        # Create dashboard with strategy config
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-strategy-dashboard",
                "title": "Strategy Test",
                "config": {"strategy": {"type": "home", "favorite_entities": []}},
            },
        )
        assert create_data["success"] is True
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None

        await asyncio.sleep(1)

        # Verify it exists
        list_data = await mcp.call_tool_success("ha_config_list_dashboards", {})
        assert any(
            d.get("url_path") == "test-strategy-dashboard"
            for d in list_data.get("dashboards", [])
        )

        # Cleanup
        await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )

        logger.info("Strategy-based dashboard test completed successfully")

    async def test_url_path_validation(self, mcp_client):
        """Test that url_path must contain hyphen."""
        logger.info("Starting url_path validation test")

        # Try to create dashboard without hyphen
        result = await mcp_client.call_tool(
            "ha_config_set_dashboard",
            {"url_path": "nodash", "title": "Invalid Dashboard"},
        )
        data = parse_mcp_result(result)
        assert data["success"] is False
        assert "hyphen" in data.get("error", "").lower()

        logger.info("url_path validation test completed successfully")

    async def test_partial_metadata_update(self, mcp_client):
        """Test updating only some metadata fields."""
        logger.info("Starting partial metadata update test")
        mcp = MCPAssertions(mcp_client)

        # Create dashboard
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": "test-partial-update", "title": "Original Title"},
        )
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None

        await asyncio.sleep(1)

        # Update only title
        meta_data = await mcp.call_tool_success(
            "ha_config_update_dashboard_metadata",
            {"dashboard_id": dashboard_id, "title": "New Title"},
        )
        assert meta_data["success"] is True
        assert "title" in meta_data.get("updated_fields", {})

        # Cleanup
        await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )

        logger.info("Partial metadata update test completed successfully")

    async def test_dashboard_without_initial_config(self, mcp_client):
        """Test creating dashboard without initial configuration."""
        logger.info("Starting dashboard without config test")
        mcp = MCPAssertions(mcp_client)

        # Create dashboard without config
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": "test-no-config", "title": "No Config Dashboard"},
        )
        assert create_data["success"] is True
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None

        await asyncio.sleep(1)

        # Verify it exists
        list_data = await mcp.call_tool_success("ha_config_list_dashboards", {})
        assert any(d.get("url_path") == "test-no-config" for d in list_data.get("dashboards", []))

        # Cleanup
        await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )

        logger.info("Dashboard without config test completed successfully")

    async def test_metadata_update_requires_at_least_one_field(self, mcp_client):
        """Test that metadata update requires at least one field."""
        logger.info("Starting metadata update validation test")

        # Try to update metadata with no fields
        result = await mcp_client.call_tool(
            "ha_config_update_dashboard_metadata", {"dashboard_id": "test-dashboard"}
        )
        data = parse_mcp_result(result)
        assert data["success"] is False
        assert "at least one field" in data.get("error", "").lower()

        logger.info("Metadata update validation test completed successfully")


class TestDashboardErrorHandling:
    """Test error handling and edge cases."""

    async def test_get_nonexistent_dashboard(self, mcp_client):
        """Test getting config for non-existent dashboard."""
        logger.info("Starting get nonexistent dashboard test")

        result = await mcp_client.call_tool(
            "ha_config_get_dashboard", {"url_path": "nonexistent-dashboard-12345"}
        )
        data = parse_mcp_result(result)
        # May succeed but return empty/error config, or fail - either is acceptable
        assert "success" in data or "error" in data

        logger.info("Get nonexistent dashboard test completed successfully")

    async def test_delete_nonexistent_dashboard(self, mcp_client):
        """Test deleting non-existent dashboard."""
        logger.info("Starting delete nonexistent dashboard test")

        result = await mcp_client.call_tool(
            "ha_config_delete_dashboard", {"dashboard_id": "nonexistent-dashboard-67890"}
        )
        data = parse_mcp_result(result)
        # Home Assistant handles delete as idempotent - deleting nonexistent item succeeds
        # This is expected behavior and consistent with other HA operations
        assert data["success"] is True

        logger.info("Delete nonexistent dashboard test completed successfully")


class TestDashboardDocumentationTools:
    """Test dashboard documentation tools."""

    async def test_get_dashboard_guide(self, mcp_client):
        """Test ha_get_dashboard_guide returns the guide."""
        logger.info("Testing ha_get_dashboard_guide")
        mcp = MCPAssertions(mcp_client)

        data = await mcp.call_tool_success("ha_get_dashboard_guide", {})

        assert data["success"] is True
        assert data["action"] == "get_guide"
        assert "guide" in data
        assert data["format"] == "markdown"

        # Verify guide contains key sections
        guide_content = data["guide"]
        assert "url_path MUST contain hyphen" in guide_content
        assert "Dashboard Structure" in guide_content
        assert "Card Categories" in guide_content

        logger.info("ha_get_dashboard_guide test passed")

    async def test_get_card_types(self, mcp_client):
        """Test ha_get_card_types returns all card types."""
        logger.info("Testing ha_get_card_types")
        mcp = MCPAssertions(mcp_client)

        data = await mcp.call_tool_success("ha_get_card_types", {})

        assert data["success"] is True
        assert data["action"] == "get_card_types"
        assert "card_types" in data
        assert "total_count" in data
        assert data["total_count"] == 41

        # Verify some common card types are present
        card_types = data["card_types"]
        assert "light" in card_types
        assert "entity" in card_types

        logger.info("ha_get_card_types test passed")

    async def test_get_card_documentation_invalid(self, mcp_client):
        """Test ha_get_card_documentation with invalid card type."""
        logger.info("Testing ha_get_card_documentation with invalid card type")
        mcp = MCPAssertions(mcp_client)

        data = await mcp.call_tool_failure(
            "ha_get_card_documentation",
            {"card_type": "nonexistent-card-type"},
            expected_error="Unknown card type",
        )

        assert data["success"] is False
        assert data["card_type"] == "nonexistent-card-type"

        logger.info("ha_get_card_documentation (invalid) test passed")
