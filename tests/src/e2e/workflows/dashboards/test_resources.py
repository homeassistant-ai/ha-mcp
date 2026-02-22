"""
End-to-End tests for Home Assistant Dashboard Resource Management.

This test suite validates the complete lifecycle of dashboard resources including:
- Resource listing
- Resource creation (module, js, css types)
- Resource updates (URL and type changes)
- Resource deletion
- Error handling and validation
- Type validation

Each test uses real Home Assistant API calls via the MCP server to ensure
production-level functionality and compatibility.
"""

import logging

# Import test utilities
from tests.src.e2e.utilities.assertions import MCPAssertions, parse_mcp_result

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TestDashboardResourceLifecycle:
    """Test complete dashboard resource CRUD lifecycle."""

    async def test_basic_resource_lifecycle(self, mcp_client):
        """Test create, read, update, delete resource workflow."""
        logger.info("Starting basic resource lifecycle test")
        mcp = MCPAssertions(mcp_client)

        # 1. List initial resources to establish baseline
        logger.info("Listing initial resources...")
        initial_list = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        assert initial_list["success"] is True
        assert "resources" in initial_list
        assert "count" in initial_list
        initial_count = initial_list["count"]
        logger.info(f"Initial resource count: {initial_count}")

        # 2. Add a new resource (module type) using set (upsert without resource_id = create)
        logger.info("Adding test resource...")
        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "url": "/local/test-e2e-card.js",
                "resource_type": "module",
            },
        )
        assert add_data["success"] is True
        assert add_data["action"] == "created"
        assert add_data["url"] == "/local/test-e2e-card.js"
        assert add_data["resource_type"] == "module"
        resource_id = add_data.get("resource_id")
        assert resource_id is not None, "Resource creation should return resource_id"
        logger.info(f"Created resource with ID: {resource_id}")

        # Small delay for HA to process

        # 3. List resources - verify new resource exists
        logger.info("Verifying resource was added...")
        list_data = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        assert list_data["success"] is True
        assert list_data["count"] == initial_count + 1
        assert any(
            r.get("url") == "/local/test-e2e-card.js"
            for r in list_data.get("resources", [])
        )

        # 4. Update the resource URL using set with resource_id
        logger.info("Updating resource URL...")
        update_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "url": "/local/test-e2e-card-v2.js",
                "resource_type": "module",
                "resource_id": resource_id,
            },
        )
        assert update_data["success"] is True
        assert update_data["action"] == "updated"


        # 5. Verify update was applied
        logger.info("Verifying resource update...")
        list_after_update = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        updated_resource = next(
            (
                r
                for r in list_after_update.get("resources", [])
                if r.get("id") == resource_id
            ),
            None,
        )
        assert updated_resource is not None, "Updated resource should still exist"
        assert updated_resource.get("url") == "/local/test-e2e-card-v2.js"

        # 6. Delete the resource
        logger.info("Deleting test resource...")
        delete_data = await mcp.call_tool_success(
            "ha_config_delete_dashboard_resource",
            {"resource_id": resource_id},
        )
        assert delete_data["success"] is True
        assert delete_data["action"] == "delete"


        # 7. Verify deletion
        logger.info("Verifying resource deletion...")
        list_after_delete = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )
        assert list_after_delete["count"] == initial_count
        assert not any(
            r.get("id") == resource_id for r in list_after_delete.get("resources", [])
        )

        logger.info("Basic resource lifecycle test completed successfully")

    async def test_resource_types(self, mcp_client):
        """Test creating resources of different types (module, js, css)."""
        logger.info("Starting resource types test")
        mcp = MCPAssertions(mcp_client)

        created_ids = []

        try:
            # Test module type
            logger.info("Testing module type resource...")
            module_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-module.js", "resource_type": "module"},
            )
            assert module_data["success"] is True
            assert module_data["resource_type"] == "module"
            created_ids.append(module_data.get("resource_id"))

            # Test js type
            logger.info("Testing js type resource...")
            js_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-legacy.js", "resource_type": "js"},
            )
            assert js_data["success"] is True
            assert js_data["resource_type"] == "js"
            created_ids.append(js_data.get("resource_id"))

            # Test css type
            logger.info("Testing css type resource...")
            css_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-theme.css", "resource_type": "css"},
            )
            assert css_data["success"] is True
            assert css_data["resource_type"] == "css"
            created_ids.append(css_data.get("resource_id"))


            # Verify by_type categorization
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )
            assert "by_type" in list_data
            logger.info(f"Resources by type: {list_data['by_type']}")

        finally:
            # Cleanup created resources
            for resource_id in created_ids:
                if resource_id:
                    await mcp_client.call_tool(
                        "ha_config_delete_dashboard_resource",
                        {"resource_id": resource_id},
                    )

        logger.info("Resource types test completed successfully")

    async def test_update_resource_type(self, mcp_client):
        """Test updating resource type."""
        logger.info("Starting update resource type test")
        mcp = MCPAssertions(mcp_client)

        resource_id = None
        try:
            # Create resource with js type
            add_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test-changetype.js", "resource_type": "js"},
            )
            resource_id = add_data.get("resource_id")
            assert resource_id is not None


            # Update to module type
            update_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "url": "/local/test-changetype.js",
                    "resource_type": "module",
                    "resource_id": resource_id,
                },
            )
            assert update_data["success"] is True
            assert update_data["action"] == "updated"


            # Verify type was changed
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )
            updated_resource = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("id") == resource_id
                ),
                None,
            )
            assert updated_resource is not None
            assert updated_resource.get("type") == "module"

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Update resource type test completed successfully")


class TestDashboardResourceValidation:
    """Test validation and error handling for dashboard resources."""

    async def test_invalid_resource_type(self, mcp_client):
        """Test that invalid resource type is rejected at schema level."""
        logger.info("Starting invalid resource type test")
        import pytest
        from fastmcp.exceptions import ToolError

        # FastMCP validates Literal types at schema level, raising ToolError
        with pytest.raises(ToolError) as exc_info:
            await mcp_client.call_tool(
                "ha_config_set_dashboard_resource",
                {"url": "/local/test.js", "resource_type": "invalid"},
            )

        # Verify the error message mentions the valid options
        error_msg = str(exc_info.value).lower()
        assert "module" in error_msg or "js" in error_msg or "css" in error_msg

        logger.info("Invalid resource type test completed successfully")

    async def test_delete_nonexistent_resource(self, mcp_client):
        """Test that deleting nonexistent resource is idempotent (succeeds)."""
        logger.info("Starting delete nonexistent resource test")
        mcp = MCPAssertions(mcp_client)

        # Deleting a resource that doesn't exist should succeed (idempotent)
        delete_data = await mcp.call_tool_success(
            "ha_config_delete_dashboard_resource",
            {"resource_id": "nonexistent-resource-id-12345"},
        )
        assert delete_data["success"] is True

        logger.info("Delete nonexistent resource test completed successfully")


class TestDashboardResourceList:
    """Test resource listing functionality."""

    async def test_list_resources_structure(self, mcp_client):
        """Test that list resources returns proper structure."""
        logger.info("Starting list resources structure test")
        mcp = MCPAssertions(mcp_client)

        list_data = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {}
        )

        assert list_data["success"] is True
        assert list_data["action"] == "list"
        assert "resources" in list_data
        assert "count" in list_data
        assert "by_type" in list_data

        # Verify by_type structure
        by_type = list_data["by_type"]
        assert "module" in by_type
        assert "js" in by_type
        assert "css" in by_type

        # All by_type values should be integers
        assert all(isinstance(v, int) for v in by_type.values())

        logger.info("List resources structure test completed successfully")

    async def test_list_resources_returns_resource_ids(self, mcp_client):
        """Test that listed resources have IDs for CRUD operations."""
        logger.info("Starting list resources returns IDs test")
        mcp = MCPAssertions(mcp_client)

        # Create a resource first
        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/local/test-id-check.js", "resource_type": "module"},
        )
        resource_id = add_data.get("resource_id")

        try:

            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )

            # Find our resource
            our_resource = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("url") == "/local/test-id-check.js"
                ),
                None,
            )
            assert our_resource is not None, "Created resource should appear in list"
            assert "id" in our_resource, "Resource should have an ID"
            assert "url" in our_resource, "Resource should have a URL"
            assert "type" in our_resource, "Resource should have a type"

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("List resources returns IDs test completed successfully")

    async def test_list_resources_include_content(self, mcp_client):
        """Test that include_content flag works."""
        logger.info("Starting list resources include_content test")
        mcp = MCPAssertions(mcp_client)

        # Just verify the tool accepts the parameter and doesn't error
        list_data = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {"include_content": False}
        )
        assert list_data["success"] is True

        list_data_with_content = await mcp.call_tool_success(
            "ha_config_list_dashboard_resources", {"include_content": True}
        )
        assert list_data_with_content["success"] is True

        logger.info("List resources include_content test completed successfully")


class TestDashboardResourceUrlPatterns:
    """Test various URL patterns for resources."""

    async def test_local_url_pattern(self, mcp_client):
        """Test /local/ URL pattern (www directory)."""
        logger.info("Starting local URL pattern test")
        mcp = MCPAssertions(mcp_client)

        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/local/custom-cards/my-card.js", "resource_type": "module"},
        )
        resource_id = add_data.get("resource_id")

        try:
            assert add_data["success"] is True
            assert add_data["url"] == "/local/custom-cards/my-card.js"
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Local URL pattern test completed successfully")

    async def test_external_url_pattern(self, mcp_client):
        """Test external HTTPS URL pattern."""
        logger.info("Starting external URL pattern test")
        mcp = MCPAssertions(mcp_client)

        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {
                "url": "https://cdn.jsdelivr.net/npm/test-card@1.0.0/dist/card.js",
                "resource_type": "module",
            },
        )
        resource_id = add_data.get("resource_id")

        try:
            assert add_data["success"] is True
            assert "jsdelivr" in add_data["url"]
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("External URL pattern test completed successfully")

    async def test_hacsfiles_url_pattern(self, mcp_client):
        """Test /hacsfiles/ URL pattern (HACS resources)."""
        logger.info("Starting hacsfiles URL pattern test")
        mcp = MCPAssertions(mcp_client)

        add_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"url": "/hacsfiles/button-card/button-card.js", "resource_type": "module"},
        )
        resource_id = add_data.get("resource_id")

        try:
            assert add_data["success"] is True
            assert add_data["url"] == "/hacsfiles/button-card/button-card.js"
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Hacsfiles URL pattern test completed successfully")


class TestInlineDashboardResource:
    """Test inline dashboard resource creation (code to URL)."""

    async def test_create_inline_module(self, mcp_client):
        """Test creating an inline module resource."""
        logger.info("Starting inline module creation test")
        mcp = MCPAssertions(mcp_client)

        # Create inline resource
        content = "class TestCard extends HTMLElement { connectedCallback() { this.innerHTML = 'Test'; } } customElements.define('test-card', TestCard);"
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content, "resource_type": "module"},
        )

        resource_id = create_data.get("resource_id")
        try:
            assert create_data["success"] is True
            assert create_data["action"] == "created"
            assert create_data["resource_type"] == "module"
            assert create_data["size"] == len(content.encode("utf-8"))
            assert resource_id is not None

            # Verify it appears in list with inline marker
            list_data = await mcp.call_tool_success(
                "ha_config_list_dashboard_resources", {}
            )

            # Find our inline resource
            our_resource = next(
                (
                    r
                    for r in list_data.get("resources", [])
                    if r.get("id") == resource_id
                ),
                None,
            )
            assert our_resource is not None
            assert our_resource.get("_inline") is True
            assert our_resource.get("url") == "[inline]"
            assert "_preview" in our_resource or "_size" in our_resource

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Inline module creation test completed successfully")

    async def test_create_inline_css(self, mcp_client):
        """Test creating an inline CSS resource."""
        logger.info("Starting inline CSS creation test")
        mcp = MCPAssertions(mcp_client)

        content = ".my-card { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); border-radius: 16px; }"
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content, "resource_type": "css"},
        )

        resource_id = create_data.get("resource_id")
        try:
            assert create_data["success"] is True
            assert create_data["resource_type"] == "css"
        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Inline CSS creation test completed successfully")

    async def test_inline_empty_content_error(self, mcp_client):
        """Test that empty content returns error."""
        logger.info("Starting inline empty content error test")

        result = await mcp_client.call_tool(
            "ha_config_set_dashboard_resource",
            {"content": ""},
        )
        data = parse_mcp_result(result)
        assert data["success"] is False
        assert "empty" in data.get("error", "").lower()

        logger.info("Inline empty content error test completed successfully")

    async def test_inline_update_existing(self, mcp_client):
        """Test updating an existing inline resource."""
        logger.info("Starting inline update test")
        mcp = MCPAssertions(mcp_client)

        # Create initial resource
        content_v1 = "const VERSION = 1;"
        create_data = await mcp.call_tool_success(
            "ha_config_set_dashboard_resource",
            {"content": content_v1, "resource_type": "module"},
        )
        resource_id = create_data.get("resource_id")

        try:
            assert create_data["action"] == "created"


            # Update with new content
            content_v2 = "const VERSION = 2; // Updated"
            update_data = await mcp.call_tool_success(
                "ha_config_set_dashboard_resource",
                {
                    "content": content_v2,
                    "resource_type": "module",
                    "resource_id": resource_id,
                },
            )
            assert update_data["success"] is True
            assert update_data["action"] == "updated"
            assert update_data["size"] == len(content_v2.encode("utf-8"))

        finally:
            if resource_id:
                await mcp_client.call_tool(
                    "ha_config_delete_dashboard_resource",
                    {"resource_id": resource_id},
                )

        logger.info("Inline update test completed successfully")
