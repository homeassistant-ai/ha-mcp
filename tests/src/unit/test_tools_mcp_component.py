"""Unit tests for tools_mcp_component module.

Tests the ha_install_mcp_tools error handling path to verify that
exceptions are properly converted to ToolError with structured error
information and HACS-specific suggestions.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_mcp_component import McpComponentTools


class TestHaInstallMcpToolsErrorHandling:
    """Tests for the exception handler in ha_install_mcp_tools."""

    @pytest.fixture
    def tools(self):
        """Create McpComponentTools instance with a mock client."""
        return McpComponentTools(AsyncMock())

    @pytest.mark.asyncio
    async def test_exception_raises_tool_error(self, tools):
        """Exceptions in ha_install_mcp_tools should raise ToolError, not return a dict."""
        mock_check = AsyncMock(side_effect=RuntimeError("Unexpected HACS failure"))
        with patch("ha_mcp.tools.tools_hacs._assert_hacs_available", mock_check), pytest.raises(ToolError) as exc_info:
            await tools.ha_install_mcp_tools(restart=False)

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    @pytest.mark.asyncio
    async def test_exception_includes_hacs_suggestions(self, tools):
        """ToolError from ha_install_mcp_tools should include HACS-specific suggestions."""
        mock_check = AsyncMock(side_effect=ConnectionError("Cannot reach HACS"))
        with patch("ha_mcp.tools.tools_hacs._assert_hacs_available", mock_check), pytest.raises(ToolError) as exc_info:
            await tools.ha_install_mcp_tools(restart=False)

        error_data = json.loads(str(exc_info.value))
        suggestions = error_data["error"]["suggestions"]
        assert any("HACS" in s for s in suggestions)
        assert any("hacs.xyz" in s for s in suggestions)
        assert any("GitHub" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_exception_preserves_tool_context(self, tools):
        """ToolError should include the tool name and restart parameter in context."""
        mock_check = AsyncMock(side_effect=RuntimeError("Something went wrong"))
        with patch("ha_mcp.tools.tools_hacs._assert_hacs_available", mock_check), pytest.raises(ToolError) as exc_info:
            await tools.ha_install_mcp_tools(restart=True)

        error_data = json.loads(str(exc_info.value))
        assert error_data.get("tool") == "ha_install_mcp_tools"
        assert error_data.get("restart") is True
