"""Unit tests for project_fields helper in util_helpers (issue #1199)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.tools.util_helpers import project_fields


class TestProjectFields:
    """Test the project_fields shared helper."""

    def test_none_fields_returns_data_unchanged(self):
        data = {"success": True, "results": [1, 2], "count": 2}
        result = project_fields(data, None)
        assert result is data

    def test_single_field_plus_success_retained(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = project_fields(data, ["results"])
        assert set(result.keys()) == {"success", "results"}
        assert result["results"] == [1, 2]

    def test_multiple_fields_retained(self):
        data = {"success": True, "results": [], "count": 0, "query": "x", "has_more": False}
        result = project_fields(data, ["results", "count"])
        assert set(result.keys()) == {"success", "results", "count"}

    def test_success_always_included_even_if_not_in_fields(self):
        data = {"success": True, "results": [], "count": 0}
        result = project_fields(data, ["count"])
        assert "success" in result
        assert result["success"] is True

    def test_unknown_field_silently_omitted(self):
        data = {"success": True, "results": []}
        result = project_fields(data, ["nonexistent"])
        assert set(result.keys()) == {"success"}

    def test_empty_fields_list_returns_only_success(self):
        data = {"success": True, "results": [], "count": 0}
        result = project_fields(data, [])
        assert set(result.keys()) == {"success"}

    def test_success_in_fields_not_duplicated(self):
        data = {"success": True, "results": []}
        result = project_fields(data, ["success", "results"])
        assert list(result.keys()).count("success") == 1

    def test_empty_data_with_none_fields(self):
        data: dict = {}
        result = project_fields(data, None)
        assert result == {}

    def test_projection_does_not_mutate_original(self):
        data = {"success": True, "results": [1], "count": 1}
        project_fields(data, ["results"])
        assert "count" in data

    def test_csv_string_input_parsed_correctly(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = project_fields(data, "results,count")
        assert set(result.keys()) == {"success", "results", "count"}

    def test_json_array_string_input_parsed_correctly(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = project_fields(data, '["results"]')
        assert set(result.keys()) == {"success", "results"}


class TestHaSearchEntitiesFieldsProjection:
    """Tool-level tests for fields= projection in ha_search_entities."""

    @pytest.fixture
    def mock_mcp(self):
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
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # exact_match=True (default) calls client.get_states() + send_websocket_message()
        client.get_states = AsyncMock(return_value=[
            {
                "entity_id": "light.kitchen",
                "state": "on",
                "attributes": {"friendly_name": "Kitchen Light", "brightness": 200},
            }
        ])
        client.send_websocket_message = AsyncMock(return_value={"success": True, "result": []})
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        return MagicMock()

    @pytest.fixture
    def search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_search_entities"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, search_tool):
        result = await search_tool(query="kitchen")
        data = result["data"]
        assert "success" in data
        assert "results" in data

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, search_tool):
        result = await search_tool(query="kitchen", fields=["results"])
        data = result["data"]
        assert "results" in data
        assert "success" in data
        assert "total_matches" not in data

    @pytest.mark.asyncio
    async def test_fields_success_always_present(self, search_tool):
        result = await search_tool(query="kitchen", fields=["results"])
        assert result["data"]["success"] is True

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, search_tool):
        with pytest.raises(ToolError):
            await search_tool(query="kitchen", fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, search_tool):
        with pytest.raises(ToolError):
            await search_tool(query="kitchen", fields='["')
