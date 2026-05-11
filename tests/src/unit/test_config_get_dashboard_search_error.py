"""Unit tests for ha_config_get_dashboard error handling.

Covers two distinct failure axes:
 - Search mode: structured error responses must not leak internal Python type
   names or tracebacks.
 - List mode (list_only=True): unexpected HA response shapes must emit a
   warning and return an empty list, not a silent degradation.

Replaces test_dashboard_find_card_error.py after ha_dashboard_find_card
was merged into ha_config_get_dashboard (issue #901).
"""

import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_config_dashboards import register_config_dashboard_tools


class TestConfigGetDashboardSearchErrorHandling:
    """Test ha_config_get_dashboard search mode error path does not leak internals."""

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
    def get_dashboard_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_config_get_dashboard function."""
        register_config_dashboard_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_get_dashboard"]

    @pytest.mark.asyncio
    async def test_error_does_not_leak_internals(self, get_dashboard_tool):
        """Error response must NOT contain 'error_type' or 'traceback'."""
        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="lovelace", entity_id="light.test")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert isinstance(result["error"], dict), "error must be structured dict, not raw string"
        assert "code" in result["error"]
        assert "message" in result["error"]
        assert "error_type" not in result
        assert "traceback" not in result

    @pytest.mark.asyncio
    async def test_error_includes_suggestions(self, get_dashboard_tool):
        """Error response must include dashboard-specific suggestions."""
        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="lovelace", entity_id="light.test")

        result = json.loads(str(exc_info.value))
        suggestions = result["error"]["suggestions"]
        assert "Check HA connection" in suggestions
        assert (
            "Verify dashboard with ha_config_get_dashboard(list_only=True)"
            in suggestions
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exception_cls,exception_msg,expected_code",
        [
            (ValueError, "invalid dashboard", "VALIDATION_FAILED"),
            (TimeoutError, "timed out", "TIMEOUT_OPERATION"),
            (RuntimeError, "unexpected failure", "INTERNAL_ERROR"),
        ],
    )
    async def test_different_exception_types_produce_correct_error_codes(
        self,
        mock_mcp,
        mock_client,
        get_dashboard_tool,
        exception_cls,
        exception_msg,
        expected_code,
    ):
        """Different exception types should map to appropriate error codes."""
        mock_client.send_websocket_message = AsyncMock(
            side_effect=exception_cls(exception_msg)
        )

        with pytest.raises(ToolError) as exc_info:
            await get_dashboard_tool(url_path="lovelace", entity_id="light.test")

        result = json.loads(str(exc_info.value))
        assert result["success"] is False
        assert result["error"]["code"] == expected_code


class TestGetDashboardListOnlyUnexpectedShape:
    """list_only=True emits a warning (not a failure) on unexpected HA response shape.

    _fetch_dashboards_list logs at WARNING and returns None; the ``or []``
    fallback means the tool still returns a valid success response with an
    empty dashboards list. This test pins the behavior introduced when the
    inline fetch was extracted to the shared helper so that a silent ``[]``
    return can no longer mask a future HA shape change at the
    ``ha_config_get_dashboard`` list path (``list_only=True`` branch).
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.send_websocket_message = AsyncMock()
        return client

    @pytest.fixture
    def get_dashboard_tool(self, mock_mcp, mock_client):
        register_config_dashboard_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_config_get_dashboard"]

    @pytest.mark.asyncio
    async def test_unexpected_shape_logs_warning_and_returns_empty_list(
        self, get_dashboard_tool, mock_client, caplog
    ):
        mock_client.send_websocket_message.return_value = "unexpected string"

        with caplog.at_level(
            logging.WARNING, logger="ha_mcp.tools.tools_config_dashboards"
        ):
            result = await get_dashboard_tool(list_only=True)

        assert result["success"] is True
        assert result["dashboards"] == []
        assert result["count"] == 0
        assert any(
            "unexpected shape" in rec.message and "type=str" in rec.message
            for rec in caplog.records
        ), (
            f"expected an 'unexpected shape' warning naming the response "
            f"type; got {[rec.message for rec in caplog.records]}"
        )
