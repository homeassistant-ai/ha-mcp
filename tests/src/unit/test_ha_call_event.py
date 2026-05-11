"""Unit tests for ha_call_event tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_service import ServiceTools


def _make_tools(fire_event_return: dict | Exception) -> ServiceTools:
    client = MagicMock()
    if isinstance(fire_event_return, Exception):
        client.fire_event = AsyncMock(side_effect=fire_event_return)
    else:
        client.fire_event = AsyncMock(return_value=fire_event_return)
    tools = ServiceTools.__new__(ServiceTools)
    tools._client = client
    tools._device_tools = MagicMock()
    return tools


class TestHaCallEvent:
    async def test_fires_event_with_no_data(self):
        tools = _make_tools({"message": "Event my_event fired."})
        result = await tools.ha_call_event("my_event")
        assert result["success"] is True
        assert result["event_type"] == "my_event"
        assert "fired" in result["message"]
        tools._client.fire_event.assert_called_once_with("my_event", None)

    async def test_fires_event_with_dict_data(self):
        tools = _make_tools({"message": "Event custom_event fired."})
        result = await tools.ha_call_event("custom_event", {"key": "value"})
        assert result["success"] is True
        tools._client.fire_event.assert_called_once_with("custom_event", {"key": "value"})

    async def test_fires_event_with_json_string_data(self):
        tools = _make_tools({"message": "Event my_event fired."})
        result = await tools.ha_call_event("my_event", '{"temperature": 22}')
        assert result["success"] is True
        tools._client.fire_event.assert_called_once_with("my_event", {"temperature": 22})

    async def test_raises_tool_error_on_list_data(self):
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event", "[1, 2, 3]")

    async def test_raises_tool_error_on_invalid_json_string(self):
        """Invalid JSON string raises ToolError with invalid_json path (not generic error)."""
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_fire_event("my_event", "{not valid json")

    async def test_returns_fallback_message_when_response_empty(self):
        tools = _make_tools({})
        result = await tools.ha_call_event("my_event")
        assert "my_event" in result["message"]

    async def test_propagates_connection_error_as_tool_error(self):
        tools = _make_tools(ConnectionError("HA unreachable"))
        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event")

    async def test_event_type_passed_to_client(self):
        tools = _make_tools({"message": "Event zone_entered fired."})
        await tools.ha_call_event("zone_entered", {"zone": "home"})
        call_args = tools._client.fire_event.call_args
        assert call_args[0][0] == "zone_entered"
        assert call_args[0][1] == {"zone": "home"}

    async def test_raises_tool_error_on_invalid_json_string(self):
        """Invalid JSON string raises ToolError with invalid_json path (not generic error)."""
        tools = _make_tools({"message": "ok"})
        with pytest.raises(ToolError):
            await tools.ha_call_event("my_event", "{not valid json")
