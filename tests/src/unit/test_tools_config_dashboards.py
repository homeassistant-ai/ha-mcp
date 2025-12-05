"""Unit tests for dashboard resource tools module.

Tests validation logic and error handling for dashboard resource management tools:
- ha_config_list_dashboard_resources
- ha_config_add_dashboard_resource
- ha_config_update_dashboard_resource
- ha_config_delete_dashboard_resource
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_config_dashboards import register_config_dashboard_tools


class MockToolRegistry:
    """Helper class to capture tool registrations."""

    def __init__(self):
        self.registered_tools: dict[str, callable] = {}

    def tool(self, *args, **kwargs):
        """Decorator that captures registered tools."""
        def wrapper(func):
            self.registered_tools[func.__name__] = func
            return func
        return wrapper


class TestHaConfigAddDashboardResource:
    """Test ha_config_add_dashboard_resource tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    # =========================================================================
    # Resource Type Validation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_valid_resource_type_module(self, registered_tools, mock_client):
        """Module type should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "test-id-123", "url": "/local/card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/custom-card.js", res_type="module")

        assert result["success"] is True
        assert result["res_type"] == "module"

    @pytest.mark.asyncio
    async def test_valid_resource_type_js(self, registered_tools, mock_client):
        """JS type should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "test-id-123", "url": "/local/legacy.js", "type": "js"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/legacy-card.js", res_type="js")

        assert result["success"] is True
        assert result["res_type"] == "js"

    @pytest.mark.asyncio
    async def test_valid_resource_type_css(self, registered_tools, mock_client):
        """CSS type should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "test-id-123", "url": "/local/theme.css", "type": "css"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/custom-theme.css", res_type="css")

        assert result["success"] is True
        assert result["res_type"] == "css"

    @pytest.mark.asyncio
    async def test_invalid_resource_type(self, registered_tools, mock_client):
        """Invalid resource type should return error with suggestions."""
        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type="invalid")

        assert result["success"] is False
        assert "invalid" in result["error"].lower()
        assert "suggestions" in result
        assert any("module" in s for s in result["suggestions"])

    @pytest.mark.asyncio
    async def test_invalid_resource_type_typescript(self, registered_tools, mock_client):
        """TypeScript type should be rejected (not supported)."""
        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.ts", res_type="ts")

        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_invalid_resource_type_empty(self, registered_tools, mock_client):
        """Empty resource type should be rejected."""
        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type="")

        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    # =========================================================================
    # URL Pattern Validation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_valid_url_local_pattern(self, registered_tools, mock_client):
        """/local/ URL pattern should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "id-1", "url": "/local/my-card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/my-card.js", res_type="module")

        assert result["success"] is True
        assert result["url"] == "/local/my-card.js"

    @pytest.mark.asyncio
    async def test_valid_url_local_nested_path(self, registered_tools, mock_client):
        """/local/ URL with nested path should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "id-1", "url": "/local/custom-cards/my-card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/custom-cards/my-card.js", res_type="module")

        assert result["success"] is True
        assert "custom-cards" in result["url"]

    @pytest.mark.asyncio
    async def test_valid_url_hacsfiles_pattern(self, registered_tools, mock_client):
        """/hacsfiles/ URL pattern should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "id-2", "url": "/hacsfiles/button-card/button-card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/hacsfiles/button-card/button-card.js", res_type="module")

        assert result["success"] is True
        assert result["url"] == "/hacsfiles/button-card/button-card.js"

    @pytest.mark.asyncio
    async def test_valid_url_https_external(self, registered_tools, mock_client):
        """HTTPS external URL should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {
                "id": "id-3",
                "url": "https://cdn.jsdelivr.net/npm/card@1.0/card.js",
                "type": "module"
            }
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(
            url="https://cdn.jsdelivr.net/npm/card@1.0/card.js",
            res_type="module"
        )

        assert result["success"] is True
        assert "jsdelivr" in result["url"]

    @pytest.mark.asyncio
    async def test_valid_url_https_with_version(self, registered_tools, mock_client):
        """HTTPS URL with version numbers should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {
                "id": "id-4",
                "url": "https://unpkg.com/some-card@2.1.0/dist/some-card.js",
                "type": "module"
            }
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(
            url="https://unpkg.com/some-card@2.1.0/dist/some-card.js",
            res_type="module"
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_valid_url_css_file(self, registered_tools, mock_client):
        """CSS file URL should be accepted."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "id-5", "url": "/local/themes/dark.css", "type": "css"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/themes/dark.css", res_type="css")

        assert result["success"] is True
        assert result["url"].endswith(".css")

    # =========================================================================
    # API Error Handling Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_api_error_response(self, registered_tools, mock_client):
        """API error response should return failure with suggestions."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Permission denied"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type="module")

        assert result["success"] is False
        assert "Permission denied" in result["error"]
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_api_exception_handling(self, registered_tools, mock_client):
        """Exception during API call should be handled gracefully."""
        mock_client.send_websocket_message.side_effect = Exception("Connection failed")

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type="module")

        assert result["success"] is False
        assert "Connection failed" in result["error"]

    @pytest.mark.asyncio
    async def test_resource_id_returned(self, registered_tools, mock_client):
        """Resource ID should be returned on successful creation."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "unique-resource-id-123", "url": "/local/card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type="module")

        assert result["success"] is True
        assert result["resource_id"] == "unique-resource-id-123"


class TestHaConfigUpdateDashboardResource:
    """Test ha_config_update_dashboard_resource tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    # =========================================================================
    # Parameter Validation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_update_requires_at_least_one_field(self, registered_tools, mock_client):
        """Update with no fields should return error."""
        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="some-id")

        assert result["success"] is False
        assert "at least one" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_url_only(self, registered_tools, mock_client):
        """Update with only URL should succeed."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/card-v2.js", "type": "module"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", url="/local/card-v2.js")

        assert result["success"] is True
        assert "url" in result["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_res_type_only(self, registered_tools, mock_client):
        """Update with only res_type should succeed."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", res_type="module")

        assert result["success"] is True
        assert "res_type" in result["updated_fields"]

    @pytest.mark.asyncio
    async def test_update_both_url_and_type(self, registered_tools, mock_client):
        """Update with both URL and type should succeed."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/new-card.js", "type": "js"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(
            resource_id="res-id",
            url="/local/new-card.js",
            res_type="js"
        )

        assert result["success"] is True
        assert "url" in result["updated_fields"]
        assert "res_type" in result["updated_fields"]

    # =========================================================================
    # Resource Type Validation Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_update_invalid_res_type(self, registered_tools, mock_client):
        """Update with invalid res_type should fail."""
        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", res_type="invalid")

        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_valid_res_type_module(self, registered_tools, mock_client):
        """Update to module type should succeed."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", res_type="module")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_update_valid_res_type_js(self, registered_tools, mock_client):
        """Update to js type should succeed."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/card.js", "type": "js"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", res_type="js")

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_update_valid_res_type_css(self, registered_tools, mock_client):
        """Update to css type should succeed."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/theme.css", "type": "css"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", res_type="css")

        assert result["success"] is True

    # =========================================================================
    # API Error Handling Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_update_resource_not_found(self, registered_tools, mock_client):
        """Update nonexistent resource should return helpful error."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Resource not found"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="nonexistent-id", url="/local/card.js")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_api_exception(self, registered_tools, mock_client):
        """Exception during update should be handled gracefully."""
        mock_client.send_websocket_message.side_effect = Exception("WebSocket error")

        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", url="/local/card.js")

        assert result["success"] is False
        assert "WebSocket error" in result["error"]


class TestHaConfigDeleteDashboardResource:
    """Test ha_config_delete_dashboard_resource tool validation logic."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    # =========================================================================
    # Successful Deletion Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_delete_success(self, registered_tools, mock_client):
        """Successful deletion should return success."""
        mock_client.send_websocket_message.return_value = {"result": None}

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        result = await tool(resource_id="resource-to-delete")

        assert result["success"] is True
        assert result["action"] == "delete"
        assert result["resource_id"] == "resource-to-delete"

    @pytest.mark.asyncio
    async def test_delete_idempotent_not_found_response(self, registered_tools, mock_client):
        """Delete of nonexistent resource should succeed (idempotent)."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Resource not found"}
        }

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        result = await tool(resource_id="nonexistent-resource")

        # Should still be success (idempotent behavior)
        assert result["success"] is True
        assert "already deleted" in result["message"].lower() or "does not exist" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_delete_idempotent_unable_to_find(self, registered_tools, mock_client):
        """Delete with 'unable to find' error should succeed (idempotent)."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Unable to find resource"}
        }

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        result = await tool(resource_id="missing-resource")

        assert result["success"] is True

    # =========================================================================
    # Error Handling Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_delete_permission_error(self, registered_tools, mock_client):
        """Delete with permission error should return failure."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Permission denied"}
        }

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        result = await tool(resource_id="protected-resource")

        assert result["success"] is False
        assert "Permission denied" in result["error"]
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_delete_api_exception(self, registered_tools, mock_client):
        """Exception during delete should be handled gracefully."""
        mock_client.send_websocket_message.side_effect = Exception("Network error")

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        result = await tool(resource_id="resource-id")

        assert result["success"] is False
        assert "Network error" in result["error"]

    @pytest.mark.asyncio
    async def test_delete_exception_not_found_is_idempotent(self, registered_tools, mock_client):
        """Exception with 'not found' should still be idempotent success."""
        mock_client.send_websocket_message.side_effect = Exception("Resource not found in storage")

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        result = await tool(resource_id="missing-id")

        assert result["success"] is True


class TestHaConfigListDashboardResources:
    """Test ha_config_list_dashboard_resources tool."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    # =========================================================================
    # Successful List Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_list_empty_resources(self, registered_tools, mock_client):
        """List with no resources should return empty list."""
        mock_client.send_websocket_message.return_value = {"result": []}

        tool = registered_tools["ha_config_list_dashboard_resources"]
        result = await tool()

        assert result["success"] is True
        assert result["resources"] == []
        assert result["count"] == 0
        assert result["by_type"] == {"module": 0, "js": 0, "css": 0}

    @pytest.mark.asyncio
    async def test_list_with_resources(self, registered_tools, mock_client):
        """List should return resources with count and categorization."""
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "1", "url": "/local/card1.js", "type": "module"},
                {"id": "2", "url": "/local/card2.js", "type": "module"},
                {"id": "3", "url": "/local/theme.css", "type": "css"},
                {"id": "4", "url": "/local/legacy.js", "type": "js"},
            ]
        }

        tool = registered_tools["ha_config_list_dashboard_resources"]
        result = await tool()

        assert result["success"] is True
        assert result["count"] == 4
        assert result["by_type"]["module"] == 2
        assert result["by_type"]["css"] == 1
        assert result["by_type"]["js"] == 1

    @pytest.mark.asyncio
    async def test_list_returns_resource_structure(self, registered_tools, mock_client):
        """Listed resources should have expected structure."""
        mock_client.send_websocket_message.return_value = {
            "result": [
                {"id": "test-id", "url": "/local/card.js", "type": "module"}
            ]
        }

        tool = registered_tools["ha_config_list_dashboard_resources"]
        result = await tool()

        assert result["success"] is True
        assert len(result["resources"]) == 1
        resource = result["resources"][0]
        assert resource["id"] == "test-id"
        assert resource["url"] == "/local/card.js"
        assert resource["type"] == "module"

    @pytest.mark.asyncio
    async def test_list_handles_list_response(self, registered_tools, mock_client):
        """List should handle direct list response format."""
        mock_client.send_websocket_message.return_value = [
            {"id": "1", "url": "/local/card.js", "type": "module"}
        ]

        tool = registered_tools["ha_config_list_dashboard_resources"]
        result = await tool()

        assert result["success"] is True
        assert result["count"] == 1

    # =========================================================================
    # Error Handling Tests
    # =========================================================================

    @pytest.mark.asyncio
    async def test_list_api_error(self, registered_tools, mock_client):
        """List API error should return failure with suggestions."""
        mock_client.send_websocket_message.side_effect = Exception("Connection refused")

        tool = registered_tools["ha_config_list_dashboard_resources"]
        result = await tool()

        assert result["success"] is False
        assert "Connection refused" in result["error"]
        assert "suggestions" in result


class TestResourceTypeEdgeCases:
    """Test edge cases for resource type handling."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_add_case_sensitive_type(self, registered_tools, mock_client):
        """Resource type should be case-sensitive (MODULE != module)."""
        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type="MODULE")

        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_add_whitespace_type(self, registered_tools, mock_client):
        """Resource type with whitespace should be rejected."""
        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js", res_type=" module ")

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_update_case_sensitive_type(self, registered_tools, mock_client):
        """Update resource type should be case-sensitive."""
        tool = registered_tools["ha_config_update_dashboard_resource"]
        result = await tool(resource_id="res-id", res_type="CSS")

        assert result["success"] is False
        assert "invalid" in result["error"].lower()


class TestURLPatternEdgeCases:
    """Test edge cases for URL pattern handling."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_url_with_query_params(self, registered_tools, mock_client):
        """URL with query parameters should be passed through."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "1", "url": "/local/card.js?v=1.0", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url="/local/card.js?v=1.0", res_type="module")

        assert result["success"] is True
        # Verify the URL was passed to the API
        call_args = mock_client.send_websocket_message.call_args
        assert call_args[0][0]["url"] == "/local/card.js?v=1.0"

    @pytest.mark.asyncio
    async def test_url_hacsfiles_deep_path(self, registered_tools, mock_client):
        """HACS URL with deep path should work."""
        mock_client.send_websocket_message.return_value = {
            "result": {
                "id": "1",
                "url": "/hacsfiles/lovelace-mushroom/mushroom.js",
                "type": "module"
            }
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(
            url="/hacsfiles/lovelace-mushroom/mushroom.js",
            res_type="module"
        )

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_https_url_complex(self, registered_tools, mock_client):
        """Complex HTTPS URL with version and path should work."""
        complex_url = "https://cdn.jsdelivr.net/gh/user/repo@v1.2.3/dist/card-bundle.min.js"
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "1", "url": complex_url, "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        result = await tool(url=complex_url, res_type="module")

        assert result["success"] is True


class TestWebSocketMessageFormat:
    """Test that WebSocket messages are formatted correctly."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures tool registrations."""
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register tools and return the registry."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_add_sends_correct_message(self, registered_tools, mock_client):
        """Add resource should send correctly formatted WebSocket message."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "new-id", "url": "/local/card.js", "type": "module"}
        }

        tool = registered_tools["ha_config_add_dashboard_resource"]
        await tool(url="/local/card.js", res_type="module")

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/create"
        assert call_args["url"] == "/local/card.js"
        assert call_args["res_type"] == "module"

    @pytest.mark.asyncio
    async def test_update_sends_correct_message(self, registered_tools, mock_client):
        """Update resource should send correctly formatted WebSocket message."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/new-card.js", "type": "js"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        await tool(resource_id="res-id", url="/local/new-card.js", res_type="js")

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/update"
        assert call_args["resource_id"] == "res-id"
        assert call_args["url"] == "/local/new-card.js"
        assert call_args["res_type"] == "js"

    @pytest.mark.asyncio
    async def test_update_only_includes_provided_fields(self, registered_tools, mock_client):
        """Update should only include fields that were provided."""
        mock_client.send_websocket_message.return_value = {
            "result": {"id": "res-id", "url": "/local/new.js", "type": "module"}
        }

        tool = registered_tools["ha_config_update_dashboard_resource"]
        await tool(resource_id="res-id", url="/local/new.js")

        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert "url" in call_args
        assert "res_type" not in call_args

    @pytest.mark.asyncio
    async def test_delete_sends_correct_message(self, registered_tools, mock_client):
        """Delete resource should send correctly formatted WebSocket message."""
        mock_client.send_websocket_message.return_value = {"result": None}

        tool = registered_tools["ha_config_delete_dashboard_resource"]
        await tool(resource_id="resource-to-delete")

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources/delete"
        assert call_args["resource_id"] == "resource-to-delete"

    @pytest.mark.asyncio
    async def test_list_sends_correct_message(self, registered_tools, mock_client):
        """List resources should send correctly formatted WebSocket message."""
        mock_client.send_websocket_message.return_value = {"result": []}

        tool = registered_tools["ha_config_list_dashboard_resources"]
        await tool()

        mock_client.send_websocket_message.assert_called_once()
        call_args = mock_client.send_websocket_message.call_args[0][0]
        assert call_args["type"] == "lovelace/resources"
