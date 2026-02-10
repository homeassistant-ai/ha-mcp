"""Unit tests for ha_dashboard_find_card error handling.

Validates that ha_dashboard_find_card uses structured error responses and
does NOT leak internal Python type names or tracebacks.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_config_dashboards import register_config_dashboard_tools


class TestDashboardFindCardErrorHandling:
    """Test ha_dashboard_find_card error path does not leak internals."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures registered tools."""
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            side_effect=RuntimeError("Connection lost")
        )
        return client

    @pytest.fixture
    def find_card_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_dashboard_find_card function."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_dashboard_find_card"]

    @pytest.mark.asyncio
    async def test_error_does_not_leak_internals(self, find_card_tool):
        """Error response must NOT contain 'error_type' or 'traceback'."""
        result = await find_card_tool(url_path="lovelace", entity_id="light.test")

        assert "error_type" not in result
        assert "traceback" not in result

    @pytest.mark.asyncio
    async def test_error_includes_suggestions(self, find_card_tool):
        """Error response must include dashboard-specific suggestions."""
        result = await find_card_tool(url_path="lovelace", entity_id="light.test")

        suggestions = result["error"]["suggestions"]
        assert "Check HA connection" in suggestions
        assert (
            "Verify dashboard with ha_config_get_dashboard(list_only=True)"
            in suggestions
        )
