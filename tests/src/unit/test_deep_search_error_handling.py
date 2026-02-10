"""Unit tests for ha_deep_search error handling.

Validates that ha_deep_search uses structured error responses and does NOT
leak internal tracebacks to clients (issue #517).
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ha_mcp.tools.tools_search import register_search_tools


class TestDeepSearchErrorHandling:
    """Test ha_deep_search error path produces structured errors without traceback leaks."""

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
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(return_value=[])
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        """Create a mock smart_tools instance."""
        smart_tools = MagicMock()
        smart_tools.deep_search = AsyncMock()
        return smart_tools

    @pytest.fixture
    def deep_search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        """Register tools and return the ha_deep_search function."""
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_deep_search"]

    @pytest.mark.asyncio
    async def test_error_does_not_leak_traceback_or_raw_fields(
        self, mock_mcp, mock_client, mock_smart_tools, deep_search_tool
    ):
        """Error response must NOT contain traceback or ad-hoc error fields (issue #517).

        The old implementation returned 'traceback' (from traceback.format_exc())
        and 'error_type' (from type(e).__name__). These leak internals to clients.
        """
        mock_smart_tools.deep_search = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        result = await deep_search_tool(query="test_query")

        data = result["data"]
        assert "traceback" not in data
        assert "error_type" not in data

    @pytest.mark.asyncio
    async def test_error_includes_search_specific_suggestions(
        self, mock_mcp, mock_client, mock_smart_tools, deep_search_tool
    ):
        """Error response must include the suggestions added by ha_deep_search."""
        mock_smart_tools.deep_search = AsyncMock(
            side_effect=RuntimeError("Something went wrong")
        )

        result = await deep_search_tool(query="test_query")

        data = result["data"]
        suggestions = data["error"]["suggestions"]
        assert "Check Home Assistant connection" in suggestions
        assert "Try simpler search terms" in suggestions
        assert (
            "Check search_types are valid: 'automation', 'script', 'helper'"
            in suggestions
        )
