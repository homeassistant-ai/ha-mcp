"""Unit tests for fields= projection in ha_list_services (issue #1199)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_services import register_services_tools
from ha_mcp.tools.util_helpers import project_fields

_SERVICES_RESULT = {
    "success": True,
    "domains": ["light", "switch"],
    "services": {
        "light.turn_on": {"name": "Turn on", "description": "Turn on a light."},
        "light.turn_off": {"name": "Turn off", "description": "Turn off a light."},
    },
    "total_count": 2,
    "count": 2,
    "offset": 0,
    "limit": 50,
    "has_more": False,
    "next_offset": None,
    "detail_level": "summary",
    "filters_applied": {"domain": None, "query": None},
}


class TestListServicesProjection:
    """Test fields= projection applied to ha_list_services responses."""

    def test_none_fields_returns_full_response(self):
        result = project_fields(dict(_SERVICES_RESULT), None)
        assert set(result.keys()) == set(_SERVICES_RESULT.keys())

    def test_single_field_services_only(self):
        result = project_fields(dict(_SERVICES_RESULT), ["services"])
        assert set(result.keys()) == {"success", "services"}
        assert result["services"] == _SERVICES_RESULT["services"]

    def test_multiple_fields_retained(self):
        result = project_fields(dict(_SERVICES_RESULT), ["services", "domains"])
        assert set(result.keys()) == {"success", "services", "domains"}

    def test_success_always_retained(self):
        result = project_fields(dict(_SERVICES_RESULT), ["domains"])
        assert "success" in result
        assert result["success"] is True

    def test_unknown_field_silently_dropped(self):
        result = project_fields(dict(_SERVICES_RESULT), ["nonexistent"])
        assert result == {"success": True}

    def test_csv_string_input(self):
        result = project_fields(dict(_SERVICES_RESULT), "services,domains")
        assert set(result.keys()) == {"success", "services", "domains"}

    def test_json_array_string_input(self):
        result = project_fields(dict(_SERVICES_RESULT), '["services"]')
        assert set(result.keys()) == {"success", "services"}

    def test_empty_list_returns_only_success(self):
        result = project_fields(dict(_SERVICES_RESULT), [])
        assert set(result.keys()) == {"success"}

    def test_does_not_mutate_original(self):
        original = dict(_SERVICES_RESULT)
        project_fields(original, ["services"])
        assert "domains" in original
        assert "detail_level" in original


class TestHaListServicesFieldsProjection:
    """Tool-level tests for fields= projection in ha_list_services."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_tool(func):
            self.registered_tools[func.__name__] = func
            return func

        mcp.add_tool = capture_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        # get_services returns an empty dict (no services), _process_services handles it
        client.get_services = AsyncMock(return_value={})
        # send_websocket_message used by _get_service_translations
        client.send_websocket_message = AsyncMock(return_value={"success": True, "result": {"resources": {}}})
        return client

    @pytest.fixture
    def list_services_tool(self, mock_mcp, mock_client):
        register_services_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_list_services"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, list_services_tool):
        result = await list_services_tool()
        assert "success" in result
        assert "services" in result

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, list_services_tool):
        result = await list_services_tool(fields=["services"])
        assert "services" in result
        assert "success" in result
        assert "domains" not in result

    @pytest.mark.asyncio
    async def test_fields_success_always_retained(self, list_services_tool):
        result = await list_services_tool(fields=["services"])
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, list_services_tool):
        with pytest.raises(ToolError):
            await list_services_tool(fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, list_services_tool):
        with pytest.raises(ToolError):
            await list_services_tool(fields='["')
