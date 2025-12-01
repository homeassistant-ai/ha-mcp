"""Unit tests for the bug report tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import __version__
from ha_mcp.tools.tools_bug_report import register_bug_report_tools


class TestBugReportTool:
    """Test suite for the ha_bug_report tool."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server."""
        mcp = MagicMock()
        # Store registered tools so we can call them in tests
        mcp._tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                mcp._tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
        return mcp

    @pytest.fixture
    def mock_client(self):
        """Create a mock Home Assistant client."""
        client = MagicMock()
        client.get_config = AsyncMock()
        client.get_states = AsyncMock()
        return client

    @pytest.fixture
    def registered_tools(self, mock_mcp, mock_client):
        """Register the bug report tools and return the mock MCP."""
        register_bug_report_tools(mock_mcp, mock_client)
        return mock_mcp

    @pytest.mark.asyncio
    async def test_bug_report_success(self, registered_tools, mock_client):
        """Test successful bug report generation."""
        # Setup mock responses
        mock_client.get_config.return_value = {
            "version": "2024.12.0",
            "location_name": "Test Home",
            "time_zone": "America/New_York",
        }
        mock_client.get_states.return_value = [
            {"entity_id": f"light.light_{i}"} for i in range(150)
        ]

        # Get the registered tool
        ha_bug_report = registered_tools._tools["ha_bug_report"]

        # Strip the decorator wrapper to get the actual function
        # The log_tool_usage decorator wraps the function
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        # Call the tool
        result = await actual_func()

        # Verify the result
        assert result["success"] is True
        assert "diagnostic_info" in result
        assert "formatted_report" in result
        assert "issue_url" in result

        # Check diagnostic info
        diag = result["diagnostic_info"]
        assert diag["ha_mcp_version"] == __version__
        assert diag["home_assistant_version"] == "2024.12.0"
        assert diag["connection_status"] == "Connected"
        assert diag["entity_count"] == 150
        assert diag["location_name"] == "Test Home"
        assert diag["time_zone"] == "America/New_York"

        # Check formatted report contains expected content
        report = result["formatted_report"]
        assert "=== ha-mcp Bug Report Info ===" in report
        assert "Home Assistant Version: 2024.12.0" in report
        assert f"ha-mcp Version: {__version__}" in report
        assert "Connection Status: Connected" in report
        assert "Entity Count: 150" in report
        assert "=== How to Report a Bug ===" in report
        assert "https://github.com/homeassistant-ai/ha-mcp/issues/new" in report

    @pytest.mark.asyncio
    async def test_bug_report_connection_error(self, registered_tools, mock_client):
        """Test bug report when connection fails."""
        # Setup mock to raise exception
        mock_client.get_config.side_effect = Exception("Connection refused")
        mock_client.get_states.side_effect = Exception("Connection refused")

        # Get the registered tool
        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        # Call the tool
        result = await actual_func()

        # Should still succeed but with error info
        assert result["success"] is True
        diag = result["diagnostic_info"]
        assert diag["ha_mcp_version"] == __version__
        assert "Connection Error" in diag["connection_status"]
        assert diag["home_assistant_version"] == "Unknown"
        assert diag["entity_count"] == 0

    @pytest.mark.asyncio
    async def test_bug_report_partial_failure(self, registered_tools, mock_client):
        """Test bug report when only some data is available."""
        # Config succeeds but states fails
        mock_client.get_config.return_value = {
            "version": "2024.12.0",
            "location_name": "Test Home",
        }
        mock_client.get_states.side_effect = Exception("States unavailable")

        # Get the registered tool
        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        # Call the tool
        result = await actual_func()

        # Should succeed with partial info
        assert result["success"] is True
        diag = result["diagnostic_info"]
        assert diag["connection_status"] == "Connected"
        assert diag["home_assistant_version"] == "2024.12.0"
        assert diag["entity_count"] == 0  # Failed to get states

    @pytest.mark.asyncio
    async def test_bug_report_issue_url(self, registered_tools, mock_client):
        """Test that the issue URL is correctly included."""
        mock_client.get_config.return_value = {"version": "2024.12.0"}
        mock_client.get_states.return_value = []

        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        result = await actual_func()

        assert (
            result["issue_url"]
            == "https://github.com/homeassistant-ai/ha-mcp/issues/new"
        )

    @pytest.mark.asyncio
    async def test_bug_report_no_timezone(self, registered_tools, mock_client):
        """Test bug report when timezone is not available."""
        mock_client.get_config.return_value = {
            "version": "2024.12.0",
            # No time_zone key
        }
        mock_client.get_states.return_value = []

        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        result = await actual_func()

        # Should still have time_zone with Unknown value
        assert result["success"] is True
        assert result["diagnostic_info"]["time_zone"] == "Unknown"
