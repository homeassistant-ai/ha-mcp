"""Unit tests for fields= projection in ha_list_services (issue #1199)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_services import register_services_tools


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
