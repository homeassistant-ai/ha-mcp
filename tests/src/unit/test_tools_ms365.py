"""
Unit tests for Microsoft 365 tools (tools_ms365.py).

All tests use mocked HTTP — no real MS365 credentials needed.
"""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestMS365TokenManagement:
    """Tests for _get_access_token caching and refresh logic."""

    def setup_method(self):
        import ha_mcp.tools.tools_ms365 as m
        m._token_cache.clear()

    @pytest.mark.asyncio
    async def test_raises_when_credentials_missing(self):
        from fastmcp.exceptions import ToolError
        from ha_mcp.tools.tools_ms365 import _get_access_token

        for key in ["MS365_CLIENT_ID", "MS365_CLIENT_SECRET", "MS365_REFRESH_TOKEN"]:
            os.environ.pop(key, None)

        with pytest.raises(ToolError, match="credentials not configured"):
            await _get_access_token()

    @pytest.mark.asyncio
    async def test_caches_token_on_success(self):
        from ha_mcp.tools.tools_ms365 import _get_access_token, _token_cache

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "test-token-abc",
            "expires_in": 3600,
        }

        with patch.dict(os.environ, {
            "MS365_CLIENT_ID": "fake-client-id",
            "MS365_CLIENT_SECRET": "fake-secret",
            "MS365_REFRESH_TOKEN": "fake-refresh-token",
            "MS365_TENANT_ID": "common",
        }):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_http = AsyncMock()
                mock_http.__aenter__ = AsyncMock(return_value=mock_http)
                mock_http.__aexit__ = AsyncMock(return_value=False)
                mock_http.post = AsyncMock(return_value=mock_response)
                mock_cls.return_value = mock_http

                token = await _get_access_token()

        assert token == "test-token-abc"
        assert "default" in _token_cache

    @pytest.mark.asyncio
    async def test_returns_cached_token_without_network_call(self):
        from ha_mcp.tools.tools_ms365 import _get_access_token, _token_cache

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        _token_cache["default"] = ("cached-token-xyz", future)

        token = await _get_access_token()
        assert token == "cached-token-xyz"

    @pytest.mark.asyncio
    async def test_per_user_token_from_cache(self):
        from ha_mcp.tools.tools_ms365 import _get_access_token, _token_cache

        future = datetime(2099, 1, 1, tzinfo=timezone.utc)
        _token_cache["sonia"] = ("sonia-token-xyz", future)

        token = await _get_access_token(user="sonia")
        assert token == "sonia-token-xyz"


class TestMS365CalendarTools:
    """Tests for calendar CRUD operations."""

    def _tools(self):
        from ha_mcp.tools.tools_ms365 import MS365CalendarTools
        return MS365CalendarTools()

    @pytest.mark.asyncio
    async def test_get_events_success(self):
        tools = self._tools()
        mock_events = [
            {"id": "e1", "subject": "Dentist", "start": {"dateTime": "2026-05-10T10:00:00"}},
            {"id": "e2", "subject": "Team sync", "start": {"dateTime": "2026-05-11T09:00:00"}},
        ]
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = {"value": mock_events}
            result = await tools.ms365_get_calendar_events()

        assert result["success"] is True
        assert result["count"] == 2
        assert result["events"][0]["subject"] == "Dentist"

    @pytest.mark.asyncio
    async def test_get_events_empty_returns_zero(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = {"value": []}
            result = await tools.ms365_get_calendar_events()

        assert result["success"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_get_events_passes_user(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = {"value": []}
            await tools.ms365_get_calendar_events(user="sonia")

        assert m.call_args.kwargs.get("user") == "sonia"

    @pytest.mark.asyncio
    async def test_create_event_returns_event_id(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = {"id": "new-event-123"}
            result = await tools.ms365_create_calendar_event(
                subject="Doctor",
                start="2026-05-15T14:00:00",
                end="2026-05-15T15:00:00",
                location="Lægehuset",
            )

        assert result["success"] is True
        assert result["event_id"] == "new-event-123"
        assert result["subject"] == "Doctor"

    @pytest.mark.asyncio
    async def test_update_event_no_fields_returns_failure(self):
        tools = self._tools()
        result = await tools.ms365_update_calendar_event(event_id="evt-123")
        assert result["success"] is False
        assert "No fields" in result["message"]

    @pytest.mark.asyncio
    async def test_update_event_patches_subject(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = None
            result = await tools.ms365_update_calendar_event(
                event_id="evt-456",
                subject="Updated subject",
            )

        assert result["success"] is True
        assert "subject" in result["updated_fields"]

    @pytest.mark.asyncio
    async def test_delete_event_success(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = None
            result = await tools.ms365_delete_calendar_event(event_id="evt-789")

        assert result["success"] is True
        assert result["event_id"] == "evt-789"


class TestMS365TodoTools:
    """Tests for To Do task operations."""

    def _tools(self):
        from ha_mcp.tools.tools_ms365 import MS365TodoTools
        return MS365TodoTools()

    def _mock_lists(self):
        return {
            "value": [
                {
                    "id": "list-default",
                    "displayName": "Tasks",
                    "wellknownListName": "defaultList",
                },
                {
                    "id": "list-shopping",
                    "displayName": "Shopping",
                    "wellknownListName": None,
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_get_tasks_default_list(self):
        tools = self._tools()
        mock_tasks = [
            {"id": "t1", "title": "Buy milk", "status": "notStarted"},
            {"id": "t2", "title": "Call bank", "status": "notStarted"},
        ]
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.side_effect = [self._mock_lists(), {"value": mock_tasks}]
            result = await tools.ms365_get_todo_tasks()

        assert result["success"] is True
        assert result["count"] == 2
        assert result["list_name"] == "Tasks"

    @pytest.mark.asyncio
    async def test_get_tasks_named_list(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.side_effect = [
                self._mock_lists(),
                {"value": [{"id": "t3", "title": "Eggs", "status": "notStarted"}]},
            ]
            result = await tools.ms365_get_todo_tasks(list_name="Shopping")

        assert result["success"] is True
        assert result["list_name"] == "Shopping"

    @pytest.mark.asyncio
    async def test_get_tasks_unknown_list_raises(self):
        from fastmcp.exceptions import ToolError
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = self._mock_lists()
            with pytest.raises(ToolError, match="not found"):
                await tools.ms365_get_todo_tasks(list_name="Nonexistent")

    @pytest.mark.asyncio
    async def test_add_task_success(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.side_effect = [self._mock_lists(), {"id": "new-task-id"}]
            result = await tools.ms365_add_todo_task(title="Pick up dry cleaning")

        assert result["success"] is True
        assert result["title"] == "Pick up dry cleaning"
        assert result["task_id"] == "new-task-id"

    @pytest.mark.asyncio
    async def test_complete_task_success(self):
        tools = self._tools()
        with patch("ha_mcp.tools.tools_ms365._graph_request", new_callable=AsyncMock) as m:
            m.return_value = None
            result = await tools.ms365_complete_todo_task(
                task_id="t1",
                list_id="list-default",
            )

        assert result["success"] is True
        assert result["task_id"] == "t1"


class TestMS365Registration:
    """Verify register_ms365_tools wires up both tool classes."""

    def test_register_calls_register_tool_methods_twice(self):
        from ha_mcp.tools.tools_ms365 import register_ms365_tools

        mock_mcp = MagicMock()
        with patch("ha_mcp.tools.tools_ms365.register_tool_methods") as mock_reg:
            register_ms365_tools(mock_mcp)

        assert mock_reg.call_count == 2
