"""Unit tests for tools_traces module."""

import pytest
from unittest.mock import AsyncMock

from ha_mcp.tools.tools_traces import (
    _format_trace_list,
    _gather_diagnostics,
)


class TestFormatTraceList:
    """Test _format_trace_list function."""

    def test_empty_traces_without_diagnostics(self):
        """Empty traces without diagnostics return basic structure."""
        result = _format_trace_list("automation.test", [], 10)

        assert result["success"] is True
        assert result["automation_id"] == "automation.test"
        assert result["trace_count"] == 0
        assert result["total_available"] == 0
        assert result["traces"] == []
        assert "diagnostics" not in result

    def test_empty_traces_with_diagnostics(self):
        """Empty traces with diagnostics include diagnostics in result."""
        diagnostics = {
            "automation_exists": True,
            "automation_enabled": True,
            "trace_storage_enabled": True,
            "last_triggered": "2025-11-30T15:00:00Z",
            "suggestion": "Traces may have been cleared.",
        }

        result = _format_trace_list("automation.test", [], 10, diagnostics)

        assert result["success"] is True
        assert result["trace_count"] == 0
        assert "diagnostics" in result
        assert result["diagnostics"]["automation_exists"] is True
        assert result["diagnostics"]["last_triggered"] == "2025-11-30T15:00:00Z"

    def test_non_empty_traces_ignore_diagnostics(self):
        """Non-empty traces do not include diagnostics even if provided."""
        traces = [
            {
                "run_id": "123.456",
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "stopped",
                "trigger": "time pattern",
            }
        ]
        diagnostics = {
            "automation_exists": True,
            "automation_enabled": True,
            "trace_storage_enabled": True,
            "last_triggered": "2025-11-30T15:00:00Z",
            "suggestion": "Test suggestion",
        }

        result = _format_trace_list("automation.test", traces, 10, diagnostics)

        assert result["success"] is True
        assert result["trace_count"] == 1
        assert "diagnostics" not in result

    def test_traces_limited_by_limit_param(self):
        """Traces are limited by the limit parameter."""
        traces = [
            {"run_id": f"{i}.0", "timestamp": f"2025-11-30T15:0{i}:00Z", "state": "stopped"}
            for i in range(5)
        ]

        result = _format_trace_list("automation.test", traces, 3)

        assert result["trace_count"] == 3
        assert result["total_available"] == 5

    def test_trace_with_error_included(self):
        """Traces with errors include the error field."""
        traces = [
            {
                "run_id": "123.456",
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "error",
                "error": "Service not found",
            }
        ]

        result = _format_trace_list("automation.test", traces, 10)

        assert result["traces"][0]["error"] == "Service not found"

    def test_script_execution_field_included(self):
        """Script traces include script_execution as execution field."""
        traces = [
            {
                "run_id": "123.456",
                "timestamp": "2025-11-30T15:00:00Z",
                "state": "stopped",
                "script_execution": "finished",
            }
        ]

        result = _format_trace_list("script.test", traces, 10)

        assert result["traces"][0]["execution"] == "finished"


class TestGatherDiagnostics:
    """Test _gather_diagnostics function."""

    @pytest.mark.asyncio
    async def test_automation_exists_and_enabled(self):
        """Diagnostics correctly identify existing, enabled automation."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        ws_client.send_command.return_value = {
            "success": True,
            "result": {"stored_traces": 5},
        }

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        assert result["automation_exists"] is True
        assert result["automation_enabled"] is True
        assert result["last_triggered"] == "2025-11-30T15:00:00Z"
        assert result["trace_storage_enabled"] is True

    @pytest.mark.asyncio
    async def test_automation_disabled(self):
        """Diagnostics correctly identify disabled automation."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "off",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": None,
            },
        }
        # Return valid config so ws_client.send_command coroutine is awaited
        ws_client.send_command.return_value = {
            "success": True,
            "result": {"stored_traces": 5},
        }

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        assert result["automation_exists"] is True
        assert result["automation_enabled"] is False
        assert "disabled" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_automation_never_triggered(self):
        """Diagnostics correctly identify automation that never triggered."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": None,
            },
        }
        # Return valid config so ws_client.send_command coroutine is awaited
        ws_client.send_command.return_value = {
            "success": True,
            "result": {"stored_traces": 5},
        }

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        assert result["automation_exists"] is True
        assert result["automation_enabled"] is True
        assert result["last_triggered"] is None
        assert "never been triggered" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_automation_not_found(self):
        """Diagnostics handle non-existent automation gracefully."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.side_effect = Exception("Entity not found")

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        assert result["automation_exists"] is False
        assert "could not find" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_trace_storage_disabled(self):
        """Diagnostics detect when trace storage is disabled."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        ws_client.send_command.return_value = {
            "success": True,
            "result": {"stored_traces": 0},
        }

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        assert result["trace_storage_enabled"] is False
        assert "trace storage is disabled" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_script_domain(self):
        """Diagnostics work correctly for script domain."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "last_triggered": None,
            },
        }

        result = await _gather_diagnostics(
            ws_client, client, "script.test", "script"
        )

        assert result["automation_exists"] is True
        # For scripts, we should see "script" in suggestion, not "automation"
        assert "script" in result["suggestion"].lower()

    @pytest.mark.asyncio
    async def test_config_fetch_failure_graceful(self):
        """Diagnostics handle config fetch failure gracefully."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        ws_client.send_command.side_effect = Exception("WebSocket error")

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        # Should still return diagnostics even if config fetch fails
        assert result["automation_exists"] is True
        assert result["automation_enabled"] is True
        # trace_storage_enabled defaults to True when we can't fetch config
        assert result["trace_storage_enabled"] is True

    @pytest.mark.asyncio
    async def test_traces_cleared_or_expired_suggestion(self):
        """Diagnostics suggest traces may have expired for enabled, triggered automation."""
        ws_client = AsyncMock()
        client = AsyncMock()
        client.get_entity_state.return_value = {
            "state": "on",
            "attributes": {
                "id": "test_unique_id",
                "last_triggered": "2025-11-30T15:00:00Z",
            },
        }
        ws_client.send_command.return_value = {
            "success": True,
            "result": {"stored_traces": 5},  # Trace storage enabled
        }

        result = await _gather_diagnostics(
            ws_client, client, "automation.test", "automation"
        )

        # For enabled automation with last_triggered and trace storage enabled,
        # suggestion should mention traces may have been cleared or expired
        assert "cleared or expired" in result["suggestion"].lower()
