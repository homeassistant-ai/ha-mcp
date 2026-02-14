"""Unit tests for tools_mcp_component module.

Tests the ha_install_mcp_tools error handling path to verify that
exceptions are properly converted to ToolError with structured error
information and HACS-specific suggestions.
"""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_mcp_component import (
    register_mcp_component_tools,
    FEATURE_FLAG,
)


def _register_and_capture(mock_check_hacs):
    """Register tools with a mock MCP and return the captured tool function.

    Must be called while _check_hacs_available is already patched,
    so the local import inside register_mcp_component_tools picks up the mock.
    """
    mock_mcp = MagicMock()
    mock_client = AsyncMock()
    captured = {}

    def fake_tool(**kwargs):
        def decorator(fn):
            captured["fn"] = fn
            return fn
        return decorator

    mock_mcp.tool = fake_tool

    with patch.dict(os.environ, {FEATURE_FLAG: "true"}):
        register_mcp_component_tools(mock_mcp, mock_client)

    assert "fn" in captured, "ha_install_mcp_tools was not registered"
    return captured["fn"]


class TestHaInstallMcpToolsErrorHandling:
    """Tests for the exception handler in ha_install_mcp_tools."""

    @pytest.mark.asyncio
    async def test_exception_raises_tool_error(self):
        """Exceptions in ha_install_mcp_tools should raise ToolError, not return a dict."""
        mock_check = AsyncMock(side_effect=RuntimeError("Unexpected HACS failure"))
        with patch("ha_mcp.tools.tools_hacs._check_hacs_available", mock_check):
            tool_fn = _register_and_capture(mock_check)
            with pytest.raises(ToolError) as exc_info:
                await tool_fn(restart=False)

        error_data = json.loads(str(exc_info.value))
        assert error_data["success"] is False

    @pytest.mark.asyncio
    async def test_exception_includes_hacs_suggestions(self):
        """ToolError from ha_install_mcp_tools should include HACS-specific suggestions."""
        mock_check = AsyncMock(side_effect=ConnectionError("Cannot reach HACS"))
        with patch("ha_mcp.tools.tools_hacs._check_hacs_available", mock_check):
            tool_fn = _register_and_capture(mock_check)
            with pytest.raises(ToolError) as exc_info:
                await tool_fn(restart=False)

        error_data = json.loads(str(exc_info.value))
        suggestions = error_data["error"]["suggestions"]
        assert any("HACS" in s for s in suggestions)
        assert any("hacs.xyz" in s for s in suggestions)
        assert any("GitHub" in s for s in suggestions)

    @pytest.mark.asyncio
    async def test_exception_preserves_tool_context(self):
        """ToolError should include the tool name and restart parameter in context."""
        mock_check = AsyncMock(side_effect=RuntimeError("Something went wrong"))
        with patch("ha_mcp.tools.tools_hacs._check_hacs_available", mock_check):
            tool_fn = _register_and_capture(mock_check)
            with pytest.raises(ToolError) as exc_info:
                await tool_fn(restart=True)

        error_data = json.loads(str(exc_info.value))
        assert error_data.get("tool") == "ha_install_mcp_tools"
        assert error_data.get("restart") is True
