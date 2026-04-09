"""Unit tests for ha_get_history tool exception handling."""

import json
from unittest.mock import MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_history import register_history_tools


class TestHaGetHistoryExceptionSuggestions:
    """Test that except Exception provides source-specific error suggestions."""

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
        """Create a minimal mock HA client."""
        client = MagicMock()
        client.base_url = "http://homeassistant.local"
        client.token = "test_token"
        return client

    @pytest.fixture
    def history_tool(self, mock_mcp, mock_client):
        """Register history tools and return ha_get_history."""
        register_history_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_get_history"]

    @pytest.mark.asyncio
    async def test_statistics_exception_includes_state_class_hint(self, history_tool):
        """Unexpected exception with source=statistics surfaces state_class suggestion."""
        with patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            side_effect=RuntimeError("unexpected"),
        ):
            with pytest.raises(ToolError) as exc_info:
                await history_tool(entity_ids="sensor.test", source="statistics")

        suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
        assert any("state_class" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_history_exception_does_not_include_state_class_hint(self, history_tool):
        """Unexpected exception with source=history does not surface state_class suggestion."""
        with patch(
            "ha_mcp.tools.tools_history.get_connected_ws_client",
            side_effect=RuntimeError("unexpected"),
        ):
            with pytest.raises(ToolError) as exc_info:
                await history_tool(entity_ids="sensor.test", source="history")

        suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
        assert not any("state_class" in s for s in suggestions)
        assert any("entity" in s.lower() for s in suggestions)
