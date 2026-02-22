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

import ast
import json
import logging
import sys
from typing import Any

import pytest

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
            "content": str(result.content[0])
            if hasattr(result, "content")
            else str(result)
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
                        {
                            "title": "Test View",
                            "cards": [{"type": "markdown", "content": "Test"}],
                        }
                    ]
                },
            },
        )
        assert create_data["success"] is True
        assert create_data["action"] in ["create", "set"]
        assert (
            create_data.get("dashboard_created") is True
            or create_data.get("action") == "create"
        )

        # Extract dashboard ID for later operations
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None, "Dashboard creation should return dashboard_id"

        # Small delay for HA to process

        # 2. List dashboards - verify exists
        logger.info("Listing dashboards...")
        list_data = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"list_only": True}
        )
        assert list_data["success"] is True
        assert any(
            d.get("url_path") == "test-e2e-dashboard"
            for d in list_data.get("dashboards", [])
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

        # 5. Update metadata (change title) via ha_config_set_dashboard
        logger.info("Updating dashboard metadata...")
        meta_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": "test-e2e-dashboard", "title": "Updated E2E Dashboard"},
        )
        assert meta_data["success"] is True
        assert meta_data.get("metadata_updated") is True

        # 6. Delete dashboard
        logger.info("Deleting test dashboard...")
        delete_data = await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )
        assert delete_data["success"] is True

        # 7. Verify deletion
        list_after_data = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"list_only": True}
        )
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

        # Verify it exists
        list_data = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"list_only": True}
        )
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
        """Test that 'lovelace' and 'default' are not rejected by hyphen validation (#591)."""
        logger.info("Starting default dashboard hyphen validation test")

        # "lovelace" should NOT be rejected by the hyphen validation
        # (it may fail for other reasons on fresh HA, but not the hyphen check)
        result = await mcp_client.call_tool(
            "ha_config_set_dashboard",
            {"url_path": "lovelace", "title": "Default Dashboard"},
        )
        data = parse_mcp_result(result)
        # The key assertion: error must NOT be about hyphens
        if not data.get("success", False):
            assert "hyphen" not in data.get("error", "").lower(), (
                f"'lovelace' should not be rejected by hyphen validation, got: {data['error']}"
            )

        # "default" alias should also not be rejected by hyphen validation
        result = await mcp_client.call_tool(
            "ha_config_set_dashboard",
            {"url_path": "default", "title": "Default Dashboard"},
        )
        data = parse_mcp_result(result)
        if not data.get("success", False):
            assert "hyphen" not in data.get("error", "").lower(), (
                f"'default' should not be rejected by hyphen validation, got: {data['error']}"
            )

        # "nodash" (non-existent, no hyphen) SHOULD still be rejected
        result = await mcp_client.call_tool(
            "ha_config_set_dashboard",
            {"url_path": "nodash", "title": "Invalid Dashboard"},
        )
        data = parse_mcp_result(result)
        assert data["success"] is False
        assert "hyphen" in data.get("error", "").lower()

        logger.info("Default dashboard hyphen validation test completed successfully")

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

        # Update only title via ha_config_set_dashboard
        meta_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": "test-partial-update", "title": "New Title"},
        )
        assert meta_data["success"] is True
        assert meta_data.get("metadata_updated") is True

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

        # Verify it exists
        list_data = await mcp.call_tool_success(
            "ha_config_get_dashboard", {"list_only": True}
        )
        assert any(
            d.get("url_path") == "test-no-config"
            for d in list_data.get("dashboards", [])
        )

        # Cleanup
        await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )

        logger.info("Dashboard without config test completed successfully")

    async def test_metadata_update_via_set_dashboard(self, mcp_client):
        """Test updating dashboard metadata via ha_config_set_dashboard."""
        logger.info("Starting metadata update via set_dashboard test")
        mcp = MCPAssertions(mcp_client)

        # Create dashboard
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": "test-meta-via-set", "title": "Original Title"},
        )
        dashboard_id = create_data.get("dashboard_id")
        assert dashboard_id is not None

        # Update title without changing config
        meta_data = await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {"url_path": "test-meta-via-set", "title": "Updated Title"},
        )
        assert meta_data["success"] is True
        assert meta_data.get("metadata_updated") is True

        # Cleanup
        await mcp.call_tool_success(
            "ha_config_delete_dashboard", {"dashboard_id": dashboard_id}
        )

        logger.info("Metadata update via set_dashboard test completed successfully")


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
            "ha_config_delete_dashboard",
            {"dashboard_id": "nonexistent-dashboard-67890"},
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
        assert "url_path must contain hyphen" in guide_content.lower()
        assert "Dashboard Structure" in guide_content
        assert "Card Categories" in guide_content

        logger.info("ha_get_dashboard_guide test passed")

    async def test_get_card_types(self, mcp_client):
        """Test ha_get_card_documentation returns all card types when called without card_type."""
        logger.info("Testing ha_get_card_documentation (list mode)")
        mcp = MCPAssertions(mcp_client)

        data = await mcp.call_tool_success("ha_get_card_documentation", {})

        assert data["success"] is True
        assert data["action"] == "get_card_types"
        assert "card_types" in data
        assert "total_count" in data
        assert data["total_count"] == 41

        # Verify some common card types are present
        card_types = data["card_types"]
        assert "light" in card_types
        assert "entity" in card_types

        logger.info("ha_get_card_documentation (list mode) test passed")

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


@pytest.mark.skipif(
    sys.platform == "win32", reason="jq library not available on Windows"
)
class TestJqTransformAndFindCard:
    """E2E tests for jq_transform and ha_dashboard_find_card."""

    async def test_jq_transform_update_field(self, mcp_client):
        """Test updating a card field using jq_transform."""
        logger.info("Starting jq_transform update field test")
        mcp = MCPAssertions(mcp_client)

        # Setup: Create test dashboard
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-jq-update",
                "title": "JQ Update Test",
                "config": {
                    "views": [
                        {
                            "title": "Test View",
                            "cards": [
                                {
                                    "type": "tile",
                                    "entity": "sensor.temperature",
                                    "icon": "mdi:old",
                                },
                            ],
                        }
                    ]
                },
            },
        )

        try:
            # Get dashboard and config_hash
            get_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-jq-update"},
            )
            config_hash = get_result["config_hash"]

            # Update icon using jq_transform
            update_result = await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-jq-update",
                    "config_hash": config_hash,
                    "jq_transform": '.views[0].cards[0].icon = "mdi:thermometer"',
                },
            )
            assert update_result["success"] is True
            assert update_result["action"] == "jq_transform"
            assert "config_hash" in update_result

            # Verify the change
            verify_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-jq-update"},
            )
            assert (
                verify_result["config"]["views"][0]["cards"][0]["icon"]
                == "mdi:thermometer"
            )

            logger.info("jq_transform update field test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-jq-update"},
            )

    async def test_jq_transform_add_delete_card(self, mcp_client):
        """Test adding and deleting cards using jq_transform."""
        logger.info("Starting jq_transform add/delete test")
        mcp = MCPAssertions(mcp_client)

        # Setup: Create test dashboard with one card
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-jq-ops",
                "title": "JQ Operations Test",
                "config": {
                    "views": [
                        {
                            "title": "Test View",
                            "cards": [
                                {"type": "tile", "entity": "sensor.temperature"},
                            ],
                        }
                    ]
                },
            },
        )

        try:
            # Add a card
            get_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-jq-ops"},
            )
            config_hash = get_result["config_hash"]

            add_result = await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-jq-ops",
                    "config_hash": config_hash,
                    "jq_transform": '.views[0].cards += [{"type": "button", "entity": "light.bedroom"}]',
                },
            )
            assert add_result["success"] is True

            # Verify 2 cards
            verify_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-jq-ops"},
            )
            assert len(verify_result["config"]["views"][0]["cards"]) == 2

            # Delete the first card
            config_hash = verify_result["config_hash"]
            delete_result = await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-jq-ops",
                    "config_hash": config_hash,
                    "jq_transform": "del(.views[0].cards[0])",
                },
            )
            assert delete_result["success"] is True

            # Verify 1 card remaining
            final_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-jq-ops"},
            )
            assert len(final_result["config"]["views"][0]["cards"]) == 1
            assert final_result["config"]["views"][0]["cards"][0]["type"] == "button"

            logger.info("jq_transform add/delete test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-jq-ops"},
            )

    async def test_jq_transform_config_hash_validation(self, mcp_client):
        """Test config_hash validation prevents conflicts."""
        logger.info("Starting config_hash validation test")
        mcp = MCPAssertions(mcp_client)

        # Setup
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-hash-validation",
                "title": "Hash Validation Test",
                "config": {
                    "views": [
                        {
                            "title": "Test View",
                            "cards": [{"type": "tile", "entity": "sensor.temperature"}],
                        }
                    ]
                },
            },
        )

        try:
            # Get config_hash
            get_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-hash-validation"},
            )
            old_hash = get_result["config_hash"]

            # Modify dashboard (simulate concurrent edit)
            await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-hash-validation",
                    "config": {
                        "views": [
                            {
                                "title": "Modified View",
                                "cards": [{"type": "markdown", "content": "Changed"}],
                            }
                        ]
                    },
                },
            )

            # Try to apply jq_transform with stale hash - should fail
            result = await mcp_client.call_tool(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-hash-validation",
                    "config_hash": old_hash,
                    "jq_transform": '.views[0].cards[0].icon = "mdi:new"',
                },
            )
            parsed = parse_mcp_result(result)
            assert parsed["success"] is False
            assert (
                "conflict" in parsed["error"].lower()
                or "modified" in parsed["error"].lower()
            )

            logger.info("config_hash validation test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-hash-validation"},
            )

    async def test_jq_transform_requires_hash(self, mcp_client):
        """Test that jq_transform requires config_hash."""
        logger.info("Starting jq_transform requires hash test")
        mcp = MCPAssertions(mcp_client)

        # Setup
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-requires-hash",
                "title": "Requires Hash Test",
                "config": {"views": [{"cards": []}]},
            },
        )

        try:
            # Try jq_transform without config_hash - should fail
            result = await mcp_client.call_tool(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-requires-hash",
                    "jq_transform": '.views[0].title = "New Title"',
                },
            )
            parsed = parse_mcp_result(result)
            assert parsed["success"] is False
            assert "config_hash is required" in parsed["error"]

            logger.info("jq_transform requires hash test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-requires-hash"},
            )

    async def test_find_card_by_entity(self, mcp_client):
        """Test finding cards by entity_id."""
        logger.info("Starting find_card by entity test")
        mcp = MCPAssertions(mcp_client)

        # Setup: Create dashboard with multiple cards
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-find-entity",
                "title": "Find Card Test",
                "config": {
                    "views": [
                        {
                            "title": "Test View",
                            "type": "sections",
                            "sections": [
                                {
                                    "title": "Section 1",
                                    "cards": [
                                        {
                                            "type": "tile",
                                            "entity": "sensor.temperature",
                                        },
                                        {"type": "tile", "entity": "sensor.humidity"},
                                    ],
                                }
                            ],
                        }
                    ]
                },
            },
        )

        try:
            # Find card by entity
            result = await mcp.call_tool_success(
                "ha_dashboard_find_card",
                {
                    "url_path": "test-find-entity",
                    "entity_id": "sensor.temperature",
                },
            )
            assert result["success"] is True
            assert result["match_count"] == 1
            assert len(result["matches"]) == 1

            match = result["matches"][0]
            assert match["view_index"] == 0
            assert match["section_index"] == 0
            assert match["card_index"] == 0
            assert "jq_path" in match
            assert match["jq_path"] == ".views[0].sections[0].cards[0]"

            logger.info("find_card by entity test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-find-entity"},
            )

    async def test_find_card_by_type(self, mcp_client):
        """Test finding cards by card type."""
        logger.info("Starting find_card by type test")
        mcp = MCPAssertions(mcp_client)

        # Setup
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-find-type",
                "title": "Find Type Test",
                "config": {
                    "views": [
                        {
                            "cards": [
                                {"type": "tile", "entity": "sensor.temperature"},
                                {"type": "markdown", "content": "Test"},
                                {"type": "tile", "entity": "sensor.humidity"},
                            ]
                        }
                    ]
                },
            },
        )

        try:
            # Find all tile cards
            result = await mcp.call_tool_success(
                "ha_dashboard_find_card",
                {
                    "url_path": "test-find-type",
                    "card_type": "tile",
                },
            )
            assert result["success"] is True
            assert result["match_count"] == 2
            assert len(result["matches"]) == 2
            assert all(m["card_index"] in [0, 2] for m in result["matches"])

            logger.info("find_card by type test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-find-type"},
            )

    async def test_find_card_with_jq_transform(self, mcp_client):
        """Test workflow: find card -> jq_transform."""
        logger.info("Starting find_card + jq_transform workflow test")
        mcp = MCPAssertions(mcp_client)

        # Setup
        await mcp.call_tool_success(
            "ha_config_set_dashboard",
            {
                "url_path": "test-workflow",
                "title": "Workflow Test",
                "config": {
                    "views": [
                        {
                            "cards": [
                                {
                                    "type": "tile",
                                    "entity": "light.living_room",
                                    "icon": "mdi:lamp",
                                },
                                {
                                    "type": "tile",
                                    "entity": "light.bedroom",
                                    "icon": "mdi:bed",
                                },
                            ]
                        }
                    ]
                },
            },
        )

        try:
            # Find card
            find_result = await mcp.call_tool_success(
                "ha_dashboard_find_card",
                {
                    "url_path": "test-workflow",
                    "entity_id": "light.bedroom",
                },
            )
            assert find_result["match_count"] == 1

            match = find_result["matches"][0]
            jq_path = match["jq_path"]
            config_hash = find_result["config_hash"]

            # Update using jq_path
            update_result = await mcp.call_tool_success(
                "ha_config_set_dashboard",
                {
                    "url_path": "test-workflow",
                    "config_hash": config_hash,
                    "jq_transform": f'{jq_path}.icon = "mdi:lightbulb"',
                },
            )
            assert update_result["success"] is True

            # Verify
            verify_result = await mcp.call_tool_success(
                "ha_config_get_dashboard",
                {"url_path": "test-workflow"},
            )
            assert (
                verify_result["config"]["views"][0]["cards"][1]["icon"]
                == "mdi:lightbulb"
            )

            logger.info("find_card + jq_transform workflow test passed")

        finally:
            await mcp.call_tool_success(
                "ha_config_delete_dashboard",
                {"dashboard_id": "test-workflow"},
            )
