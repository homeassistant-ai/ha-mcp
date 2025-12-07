"""Unit tests for dashboard resource tools module.

Tests validation logic and error handling for dashboard resource management tools:
- ha_config_list_dashboard_resources
- ha_config_add_dashboard_resource
- ha_config_update_dashboard_resource
- ha_config_delete_dashboard_resource
"""

import copy

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


# =============================================================================
# Card Navigation Helper Tests
# =============================================================================


class TestGetCardsContainer:
    """Test _get_cards_container helper function for card navigation."""

    def test_flat_view_returns_cards_list(self):
        """Should return cards list from flat view (no sections)."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {
            "views": [
                {"title": "View 1", "cards": [{"type": "markdown", "content": "test"}]}
            ]
        }
        result = _get_cards_container(config, view_index=0, section_index=None)

        assert result["success"] is True
        assert result["cards"] == [{"type": "markdown", "content": "test"}]
        assert result["view"] == config["views"][0]
        assert result["section"] is None

    def test_sections_view_returns_section_cards(self):
        """Should return cards list from section within sections view."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {
            "views": [
                {
                    "title": "View 1",
                    "type": "sections",
                    "sections": [
                        {"title": "Section 0", "cards": [{"type": "tile", "entity": "light.one"}]},
                        {"title": "Section 1", "cards": [{"type": "tile", "entity": "light.two"}]},
                    ],
                }
            ]
        }
        result = _get_cards_container(config, view_index=0, section_index=1)

        assert result["success"] is True
        assert result["cards"] == [{"type": "tile", "entity": "light.two"}]
        assert result["section"] == config["views"][0]["sections"][1]

    def test_view_index_out_of_bounds_returns_error(self):
        """Should return error for invalid view index."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {"views": [{"title": "View 1", "cards": []}]}
        result = _get_cards_container(config, view_index=5, section_index=None)

        assert result["success"] is False
        assert "view" in result["error"].lower()
        assert "out of bounds" in result["error"].lower()
        assert "suggestions" in result

    def test_section_index_out_of_bounds_returns_error(self):
        """Should return error for invalid section index."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {
            "views": [{"title": "View 1", "type": "sections", "sections": [{"cards": []}]}]
        }
        result = _get_cards_container(config, view_index=0, section_index=10)

        assert result["success"] is False
        assert "section" in result["error"].lower()
        assert "out of bounds" in result["error"].lower()

    def test_section_required_for_sections_view(self):
        """Should return error when section_index missing for sections view."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {
            "views": [{"title": "View 1", "type": "sections", "sections": [{"cards": []}]}]
        }
        result = _get_cards_container(config, view_index=0, section_index=None)

        assert result["success"] is False
        assert "section_index" in result["error"].lower()
        assert "required" in result["error"].lower()

    def test_section_not_applicable_for_flat_view(self):
        """Should return error when section_index provided for flat view."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {"views": [{"title": "View 1", "cards": []}]}
        result = _get_cards_container(config, view_index=0, section_index=0)

        assert result["success"] is False
        assert "section_index" in result["error"].lower()
        assert "not applicable" in result["error"].lower()

    def test_strategy_dashboard_rejected(self):
        """Should return error for strategy-based dashboards."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {"strategy": {"type": "home"}}
        result = _get_cards_container(config, view_index=0, section_index=None)

        assert result["success"] is False
        assert "strategy" in result["error"].lower()
        assert "suggestions" in result

    def test_no_views_returns_error(self):
        """Should return error when dashboard has no views."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {}
        result = _get_cards_container(config, view_index=0, section_index=None)

        assert result["success"] is False
        assert "no views" in result["error"].lower()

    def test_initializes_missing_cards_list_in_view(self):
        """Should initialize cards list if missing in flat view."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {"views": [{"title": "View 1"}]}  # No cards key
        result = _get_cards_container(config, view_index=0, section_index=None)

        assert result["success"] is True
        assert result["cards"] == []
        assert "cards" in config["views"][0]  # Cards list was added

    def test_initializes_missing_cards_list_in_section(self):
        """Should initialize cards list if missing in section."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {
            "views": [{"type": "sections", "sections": [{"title": "Section 0"}]}]  # No cards key
        }
        result = _get_cards_container(config, view_index=0, section_index=0)

        assert result["success"] is True
        assert result["cards"] == []
        assert "cards" in config["views"][0]["sections"][0]  # Cards list was added

    def test_returns_mutable_reference(self):
        """Cards list should be a mutable reference to original config."""
        from ha_mcp.tools.tools_config_dashboards import _get_cards_container

        config = {"views": [{"cards": [{"type": "markdown"}]}]}
        result = _get_cards_container(config, view_index=0, section_index=None)

        # Modify through returned reference
        result["cards"].append({"type": "button"})

        # Original config should be modified
        assert len(config["views"][0]["cards"]) == 2
        assert config["views"][0]["cards"][1]["type"] == "button"


# =============================================================================
# Card Tool Tests
# =============================================================================


class TestHaDashboardRemoveCard:
    """Test ha_dashboard_remove_card tool."""

    @pytest.fixture
    def mock_mcp(self):
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_remove_card_from_flat_view(self, registered_tools, mock_client):
        """Should remove card from flat view and save config."""
        config_response = {
            "result": {
                "views": [{
                    "title": "View",
                    "cards": [
                        {"type": "markdown", "content": "Card 0"},
                        {"type": "markdown", "content": "Card 1"},
                        {"type": "markdown", "content": "Card 2"},
                    ]
                }]
            }
        }
        mock_client.send_websocket_message.side_effect = [
            # First call: get config
            copy.deepcopy(config_response),
            # Second call: verify config unchanged (optimistic locking) - fresh copy
            copy.deepcopy(config_response),
            # Third call: save config
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=1,
        )

        assert result["success"] is True
        assert result["action"] == "remove_card"
        assert result["removed_card"]["content"] == "Card 1"

        # Verify save was called with correct config (card removed)
        save_call = mock_client.send_websocket_message.call_args_list[2]
        saved_config = save_call[0][0]["config"]
        assert len(saved_config["views"][0]["cards"]) == 2
        assert saved_config["views"][0]["cards"][0]["content"] == "Card 0"
        assert saved_config["views"][0]["cards"][1]["content"] == "Card 2"

    @pytest.mark.asyncio
    async def test_remove_card_from_section(self, registered_tools, mock_client):
        """Should remove card from sections view."""
        config_response = {
            "result": {
                "views": [{
                    "type": "sections",
                    "sections": [
                        {"cards": [{"type": "tile", "entity": "light.one"}]},
                        {"cards": [
                            {"type": "tile", "entity": "light.two"},
                            {"type": "tile", "entity": "light.three"},
                        ]},
                    ]
                }]
            }
        }
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            section_index=1,
            card_index=0,
        )

        assert result["success"] is True
        assert result["removed_card"]["entity"] == "light.two"

    @pytest.mark.asyncio
    async def test_remove_card_index_out_of_bounds(self, registered_tools, mock_client):
        """Should return error for invalid card index."""
        mock_client.send_websocket_message.return_value = {
            "result": {
                "views": [{"cards": [{"type": "markdown"}]}]
            }
        }

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=5,
        )

        assert result["success"] is False
        assert "card" in result["error"].lower()
        assert "out of bounds" in result["error"].lower()


class TestHaDashboardAddCard:
    """Test ha_dashboard_add_card tool."""

    @pytest.fixture
    def mock_mcp(self):
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_add_card_append_to_flat_view(self, registered_tools, mock_client):
        """Should append card to end of flat view."""
        config_response = {"result": {"views": [{"cards": [{"type": "markdown"}]}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"type": "tile", "entity": "light.new"},
        )

        assert result["success"] is True
        assert result["action"] == "add_card"
        assert result["location"]["card_index"] == 1  # Appended at end

    @pytest.mark.asyncio
    async def test_add_card_with_position(self, registered_tools, mock_client):
        """Should insert card at specified position."""
        config_response = {"result": {"views": [{"cards": [
            {"type": "markdown", "content": "0"},
            {"type": "markdown", "content": "1"},
        ]}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"type": "tile", "entity": "light.inserted"},
            position=1,
        )

        assert result["success"] is True
        assert result["location"]["card_index"] == 1

        # Verify insertion position
        save_call = mock_client.send_websocket_message.call_args_list[2]
        saved_cards = save_call[0][0]["config"]["views"][0]["cards"]
        assert saved_cards[1]["entity"] == "light.inserted"

    @pytest.mark.asyncio
    async def test_add_card_to_section(self, registered_tools, mock_client):
        """Should add card to sections view."""
        config_response = {"result": {"views": [{
            "type": "sections",
            "sections": [{"cards": []}]
        }]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            section_index=0,
            card_config={"type": "tile", "entity": "light.new"},
        )

        assert result["success"] is True
        assert result["location"]["section_index"] == 0

    @pytest.mark.asyncio
    async def test_add_card_missing_type(self, registered_tools, mock_client):
        """Should reject card config without type field."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{"cards": []}]}
        }

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"entity": "light.no_type"},
        )

        assert result["success"] is False
        assert "type" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_add_card_json_string_config(self, registered_tools, mock_client):
        """Should accept card_config as JSON string."""
        config_response = {"result": {"views": [{"cards": []}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config='{"type": "markdown", "content": "Hello"}',
        )

        assert result["success"] is True


class TestHaDashboardUpdateCard:
    """Test ha_dashboard_update_card tool."""

    @pytest.fixture
    def mock_mcp(self):
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_update_card_replaces_entirely(self, registered_tools, mock_client):
        """Should replace card config entirely (no merge)."""
        config_response = {"result": {"views": [{"cards": [
            {"type": "markdown", "content": "Old content", "title": "Old Title"}
        ]}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config={"type": "markdown", "content": "New content"},
        )

        assert result["success"] is True
        assert result["action"] == "update_card"
        assert result["previous_card"]["content"] == "Old content"
        assert result["updated_card"]["content"] == "New content"
        # Title should NOT exist (replaced, not merged)
        assert "title" not in result["updated_card"]

    @pytest.mark.asyncio
    async def test_update_card_in_section(self, registered_tools, mock_client):
        """Should update card within sections view."""
        config_response = {"result": {"views": [{
            "type": "sections",
            "sections": [{"cards": [{"type": "tile", "entity": "light.old"}]}]
        }]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            section_index=0,
            card_index=0,
            card_config={"type": "tile", "entity": "light.new", "name": "Updated"},
        )

        assert result["success"] is True
        assert result["previous_card"]["entity"] == "light.old"
        assert result["updated_card"]["entity"] == "light.new"

    @pytest.mark.asyncio
    async def test_update_card_index_out_of_bounds(self, registered_tools, mock_client):
        """Should return error for invalid card index."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{"cards": [{"type": "markdown"}]}]}
        }

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=99,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "out of bounds" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_card_json_string_config(self, registered_tools, mock_client):
        """Should accept card_config as JSON string."""
        config_response = {"result": {"views": [{"cards": [{"type": "markdown"}]}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"result": None, "success": True}
        ]

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config='{"type": "button", "entity": "switch.test"}',
        )

        assert result["success"] is True
        assert result["updated_card"]["type"] == "button"

    @pytest.mark.asyncio
    async def test_update_card_missing_config(self, registered_tools, mock_client):
        """Should return error when card_config is missing."""
        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config=None,
        )

        assert result["success"] is False
        assert "required" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_card_missing_type(self, registered_tools, mock_client):
        """Should reject card config without type field."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{"cards": [{"type": "markdown"}]}]}
        }

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config={"entity": "light.no_type"},
        )

        assert result["success"] is False
        assert "type" in result["error"].lower()
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_update_card_get_dashboard_failure(self, registered_tools, mock_client):
        """Should return error when GET dashboard fails."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Dashboard not found"}
        }

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="nonexistent-dashboard",
            view_index=0,
            card_index=0,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "failed to get dashboard" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_update_card_save_failure(self, registered_tools, mock_client):
        """Should return error when SAVE fails."""
        config_response = {"result": {"views": [{"cards": [{"type": "markdown"}]}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"success": False, "error": {"message": "Permission denied"}}
        ]

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config={"type": "button"},
        )

        assert result["success"] is False
        assert "failed to save" in result["error"].lower()


class TestHaDashboardRemoveCardErrorPaths:
    """Test error paths for ha_dashboard_remove_card."""

    @pytest.fixture
    def mock_mcp(self):
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_remove_card_get_dashboard_failure(self, registered_tools, mock_client):
        """Should return error when GET dashboard fails."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Dashboard not found"}
        }

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(
            url_path="nonexistent-dashboard",
            view_index=0,
            card_index=0,
        )

        assert result["success"] is False
        assert "failed to get dashboard" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_remove_card_save_failure(self, registered_tools, mock_client):
        """Should return error when SAVE fails."""
        config_response = {"result": {"views": [{"cards": [{"type": "markdown"}]}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"success": False, "error": {"message": "Permission denied"}}
        ]

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
        )

        assert result["success"] is False
        assert "failed to save" in result["error"].lower()


class TestHaDashboardAddCardErrorPaths:
    """Test error paths for ha_dashboard_add_card."""

    @pytest.fixture
    def mock_mcp(self):
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_add_card_get_dashboard_failure(self, registered_tools, mock_client):
        """Should return error when GET dashboard fails."""
        mock_client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "Dashboard not found"}
        }

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="nonexistent-dashboard",
            view_index=0,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "failed to get dashboard" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_add_card_save_failure(self, registered_tools, mock_client):
        """Should return error when SAVE fails."""
        config_response = {"result": {"views": [{"cards": []}]}}
        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(config_response),
            copy.deepcopy(config_response),  # verify unchanged
            {"success": False, "error": {"message": "Permission denied"}}
        ]

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "failed to save" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_add_card_position_out_of_bounds(self, registered_tools, mock_client):
        """Should return error for invalid position."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{"cards": [{"type": "markdown"}]}]}
        }

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"type": "markdown"},
            position=99,
        )

        assert result["success"] is False
        assert "out of bounds" in result["error"].lower()


class TestEdgeCases:
    """Test edge cases identified during code review."""

    @pytest.fixture
    def mock_mcp(self):
        return MockToolRegistry()

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return mock_mcp.registered_tools

    @pytest.mark.asyncio
    async def test_empty_config_response(self, registered_tools, mock_client):
        """Should return error when dashboard config is empty/null."""
        mock_client.send_websocket_message.return_value = {"result": None}

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(url_path="test-dashboard", view_index=0, card_index=0)

        assert result["success"] is False
        assert "empty" in result["error"].lower()
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_remove_card_from_empty_container(self, registered_tools, mock_client):
        """Should return clear error message for empty cards list."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{"cards": []}]}
        }

        tool = registered_tools["ha_dashboard_remove_card"]
        result = await tool(url_path="test-dashboard", view_index=0, card_index=0)

        assert result["success"] is False
        assert "out of bounds" in result["error"].lower() or "no cards" in str(result).lower()

    @pytest.mark.asyncio
    async def test_add_card_invalid_json_string(self, registered_tools, mock_client):
        """Should return error for malformed JSON string config."""
        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config='{"type": "markdown", invalid}',
        )

        assert result["success"] is False
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_update_card_invalid_json_string(self, registered_tools, mock_client):
        """Should return error for malformed JSON string config."""
        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config='{"type": "markdown", bad json}',
        )

        assert result["success"] is False
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_add_card_config_as_list(self, registered_tools, mock_client):
        """Should reject card_config that is not a dict."""
        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config=["type", "markdown"],
        )

        assert result["success"] is False
        assert "dict" in result["error"].lower() or "object" in result["error"].lower()
        assert result.get("provided_type") == "list"

    @pytest.mark.asyncio
    async def test_update_card_corrupted_existing_card(self, registered_tools, mock_client):
        """Should return error when existing card is not a dict."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{"cards": [None]}]}  # Corrupted card (null)
        }

        tool = registered_tools["ha_dashboard_update_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_index=0,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "invalid" in result["error"].lower()
        assert "suggestions" in result

    @pytest.mark.asyncio
    async def test_corrupted_view_not_dict(self, registered_tools, mock_client):
        """Should return error when view is not a dict."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [None]}  # Corrupted view (null)
        }

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_corrupted_section_not_dict(self, registered_tools, mock_client):
        """Should return error when section is not a dict."""
        mock_client.send_websocket_message.return_value = {
            "result": {"views": [{
                "type": "sections",
                "sections": ["not a dict"]  # Corrupted section
            }]}
        }

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            section_index=0,
            card_config={"type": "markdown"},
        )

        assert result["success"] is False
        assert "invalid" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_optimistic_locking_conflict(self, registered_tools, mock_client):
        """Should reject update when dashboard changed between read and save."""
        # First read returns one config
        initial_config = {"result": {"views": [{"cards": [{"type": "markdown"}]}]}}
        # Verification read returns different config (simulating external modification)
        modified_config = {"result": {"views": [{"cards": [
            {"type": "markdown"},
            {"type": "tile", "entity": "light.added_externally"}
        ]}]}}

        mock_client.send_websocket_message.side_effect = [
            copy.deepcopy(initial_config),
            copy.deepcopy(modified_config),  # Different! External change detected
        ]

        tool = registered_tools["ha_dashboard_add_card"]
        result = await tool(
            url_path="test-dashboard",
            view_index=0,
            card_config={"type": "button", "entity": "switch.test"},
        )

        assert result["success"] is False
        assert "conflict" in result["error"].lower()
        assert "suggestions" in result
        # Should suggest re-reading the dashboard
        assert any("re-read" in s.lower() or "refresh" in s.lower()
                   for s in result["suggestions"])
