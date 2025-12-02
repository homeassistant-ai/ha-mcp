"""Unit tests for the bug report tool."""

from unittest.mock import AsyncMock, MagicMock, patch

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

        # Call the tool with default parameters
        result = await actual_func()

        # Verify the result
        assert result["success"] is True
        assert "diagnostic_info" in result
        assert "formatted_report" in result
        assert "issue_url" in result
        assert "bug_report_template" in result
        assert "anonymization_guide" in result
        assert "recent_logs" in result
        assert "log_count" in result
        assert "instructions" in result

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

        # Check bug report template
        template = result["bug_report_template"]
        assert "## Bug Report Template" in template
        assert "I want to file a bug for:" in template
        assert "What I was trying to do" in template
        assert "What happened" in template
        assert "What I expected" in template
        assert "Steps to reproduce" in template
        assert "Environment" in template
        assert "https://github.com/homeassistant-ai/ha-mcp/issues/new" in template

        # Check anonymization guide
        anon_guide = result["anonymization_guide"]
        assert "## Anonymization Guide" in anon_guide
        assert "MUST ANONYMIZE" in anon_guide
        assert "CONSIDER ANONYMIZING" in anon_guide
        assert "KEEP AS-IS" in anon_guide

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

    @pytest.mark.asyncio
    async def test_bug_report_with_custom_tool_call_count(
        self, registered_tools, mock_client
    ):
        """Test bug report with custom tool_call_count parameter."""
        mock_client.get_config.return_value = {"version": "2024.12.0"}
        mock_client.get_states.return_value = []

        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        # Mock get_recent_logs to verify it's called with correct max_entries
        # Formula: AVG_LOG_ENTRIES_PER_TOOL (3) * 2 * tool_call_count
        with patch(
            "ha_mcp.tools.tools_bug_report.get_recent_logs"
        ) as mock_get_logs:
            mock_get_logs.return_value = [
                {
                    "timestamp": "2024-12-01T10:00:00",
                    "tool_name": "ha_search_entities",
                    "success": True,
                    "execution_time_ms": 150,
                },
                {
                    "timestamp": "2024-12-01T10:00:01",
                    "tool_name": "ha_call_service",
                    "success": False,
                    "execution_time_ms": 50,
                    "error_message": "Service not found",
                },
            ]

            # Call with tool_call_count=5
            result = await actual_func(tool_call_count=5)

            # Verify get_recent_logs was called with 3 * 2 * 5 = 30
            mock_get_logs.assert_called_once_with(max_entries=30)

            # Verify logs are included in result
            assert result["log_count"] == 2
            assert len(result["recent_logs"]) == 2

    @pytest.mark.asyncio
    async def test_bug_report_log_formatting(self, registered_tools, mock_client):
        """Test that logs are properly formatted in the report."""
        mock_client.get_config.return_value = {"version": "2024.12.0"}
        mock_client.get_states.return_value = []

        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        with patch(
            "ha_mcp.tools.tools_bug_report.get_recent_logs"
        ) as mock_get_logs:
            mock_get_logs.return_value = [
                {
                    "timestamp": "2024-12-01T10:00:00.123456",
                    "tool_name": "ha_get_state",
                    "success": True,
                    "execution_time_ms": 100,
                },
                {
                    "timestamp": "2024-12-01T10:00:01.654321",
                    "tool_name": "ha_call_service",
                    "success": False,
                    "execution_time_ms": 50,
                    "error_message": "Entity not found",
                },
            ]

            result = await actual_func()

            # Check formatted_report includes log section
            report = result["formatted_report"]
            assert "=== Recent Tool Calls (2 entries) ===" in report
            assert "ha_get_state" in report
            assert "ha_call_service" in report
            assert "OK" in report
            assert "FAIL" in report

            # Check template includes logs
            template = result["bug_report_template"]
            assert "### Recent tool calls" in template
            assert "ha_get_state" in template

    @pytest.mark.asyncio
    async def test_bug_report_instructions(self, registered_tools, mock_client):
        """Test that instructions for the AI agent are included."""
        mock_client.get_config.return_value = {"version": "2024.12.0"}
        mock_client.get_states.return_value = []

        ha_bug_report = registered_tools._tools["ha_bug_report"]
        actual_func = ha_bug_report
        while hasattr(actual_func, "__wrapped__"):
            actual_func = actual_func.__wrapped__

        result = await actual_func()

        # Check instructions are present and useful
        instructions = result["instructions"]
        assert "bug_report_template" in instructions
        assert "anonymization_guide" in instructions
        assert "privacy" in instructions.lower()
