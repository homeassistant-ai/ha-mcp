"""Unit tests for ha_get_diagnostics tool."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantConnectionError
from ha_mcp.tools.tools_integrations import IntegrationTools


def _make_tools(diagnostics_return: dict | Exception) -> IntegrationTools:
    client = MagicMock()
    if isinstance(diagnostics_return, Exception):
        client.get_diagnostics = AsyncMock(side_effect=diagnostics_return)
    else:
        client.get_diagnostics = AsyncMock(return_value=diagnostics_return)
    tools = IntegrationTools.__new__(IntegrationTools)
    tools._client = client
    return tools


class TestHaGetDiagnostics:
    async def test_returns_diagnostics_for_config_entry(self):
        dump = {"home_assistant": {"version": "2026.5.0"}, "data": {"auth": "ok"}}
        tools = _make_tools(dump)
        result = await tools.ha_get_diagnostics("entry_abc")
        assert result["success"] is True
        assert result["config_entry_id"] == "entry_abc"
        assert result["diagnostics"] == dump
        assert "device_id" not in result
        tools._client.get_diagnostics.assert_called_once_with("entry_abc", None)

    async def test_passes_device_id_when_provided(self):
        dump = {"device": "data"}
        tools = _make_tools(dump)
        result = await tools.ha_get_diagnostics("entry_abc", "device_xyz")
        assert result["success"] is True
        assert result["device_id"] == "device_xyz"
        tools._client.get_diagnostics.assert_called_once_with("entry_abc", "device_xyz")

    async def test_omits_device_id_key_when_not_provided(self):
        tools = _make_tools({"data": "ok"})
        result = await tools.ha_get_diagnostics("entry_abc")
        assert "device_id" not in result

    async def test_raises_tool_error_on_404(self):
        tools = _make_tools(HomeAssistantAPIError("Not found", status_code=404))
        with pytest.raises(ToolError):
            await tools.ha_get_diagnostics("nonexistent")

    async def test_raises_tool_error_on_connection_error(self):
        tools = _make_tools(HomeAssistantConnectionError("HA unreachable"))
        with pytest.raises(ToolError):
            await tools.ha_get_diagnostics("entry_abc")

    async def test_raises_tool_error_on_403(self):
        tools = _make_tools(HomeAssistantAPIError("Forbidden", status_code=403))
        with pytest.raises(ToolError):
            await tools.ha_get_diagnostics("entry_abc")

    async def test_diagnostics_payload_forwarded_unchanged(self):
        nested = {"a": {"b": [1, 2, 3]}, "redacted": True}
        tools = _make_tools(nested)
        result = await tools.ha_get_diagnostics("entry_abc")
        assert result["diagnostics"] is nested
