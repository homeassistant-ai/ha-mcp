"""Unit tests for the ha_manage_theme tool in tools_themes module."""

import json
from unittest.mock import AsyncMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_themes import ThemesTools


def _client(themes_dict=None, default_theme="default", default_dark_theme=None):
    """Mock client whose websocket returns themes and whose services succeed."""
    client = AsyncMock()
    result = {
        "success": True,
        "result": {
            "themes": themes_dict or {},
            "default_theme": default_theme,
            "default_dark_theme": default_dark_theme,
        },
    }
    client.send_websocket_message.return_value = result
    client.call_service.return_value = []
    return client


def _tool_error_payload(exc_info) -> dict:
    """Parse the structured error JSON out of a raised ToolError."""
    return json.loads(str(exc_info.value))


class TestManageThemeList:
    """Coverage for action='list'."""

    @pytest.mark.asyncio
    async def test_list_returns_sorted_names_and_defaults(self):
        client = _client(
            themes_dict={"nord": {"primary-color": "#5e81ac"}, "iceberg": {}},
            default_theme="nord",
            default_dark_theme="iceberg",
        )
        tools = ThemesTools(client)

        result = await tools.ha_manage_theme(action="list")

        assert result["success"] is True
        data = result["data"]
        assert data["themes"] == ["iceberg", "nord"]
        assert data["count"] == 2
        assert data["default_theme"] == "nord"
        assert data["default_dark_theme"] == "iceberg"
        client.send_websocket_message.assert_awaited_once_with(
            {"type": "frontend/get_themes"}
        )

    @pytest.mark.asyncio
    async def test_list_with_no_themes_installed(self):
        tools = ThemesTools(_client())

        result = await tools.ha_manage_theme(action="list")

        assert result["success"] is True
        assert result["data"]["themes"] == []
        assert result["data"]["count"] == 0

    @pytest.mark.asyncio
    async def test_list_websocket_failure_raises_tool_error(self):
        client = AsyncMock()
        client.send_websocket_message.return_value = {
            "success": False,
            "error": {"message": "not allowed"},
        }
        tools = ThemesTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_theme(action="list")

        payload = _tool_error_payload(exc_info)
        assert payload["success"] is False
        assert "not allowed" in payload["error"]["message"]


class TestManageThemeSet:
    """Coverage for action='set'."""

    @pytest.mark.asyncio
    async def test_set_requires_theme_name(self):
        tools = ThemesTools(_client())

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_theme(action="set")

        payload = _tool_error_payload(exc_info)
        assert payload["error"]["code"] == "VALIDATION_MISSING_PARAMETER"

    @pytest.mark.asyncio
    async def test_set_calls_service_and_verifies(self):
        client = _client(themes_dict={"nord": {}}, default_theme="nord")
        tools = ThemesTools(client)

        result = await tools.ha_manage_theme(action="set", theme_name="nord")

        client.call_service.assert_awaited_once_with(
            "frontend", "set_theme", {"name": "nord"}
        )
        assert result["success"] is True
        assert result["data"]["theme"] == "nord"
        assert result["data"]["mode"] == "light"
        assert result["data"]["default_theme"] == "nord"

    @pytest.mark.asyncio
    async def test_set_with_dark_mode_passes_mode(self):
        client = _client(
            themes_dict={"nord": {}},
            default_theme="default",
            default_dark_theme="nord",
        )
        tools = ThemesTools(client)

        result = await tools.ha_manage_theme(
            action="set", theme_name="nord", mode="dark"
        )

        client.call_service.assert_awaited_once_with(
            "frontend", "set_theme", {"name": "nord", "mode": "dark"}
        )
        assert result["data"]["mode"] == "dark"
        assert result["data"]["default_dark_theme"] == "nord"

    @pytest.mark.asyncio
    async def test_set_service_failure_raises_tool_error(self):
        client = _client()
        client.call_service.side_effect = RuntimeError("Theme nope not found")
        tools = ThemesTools(client)

        with pytest.raises(ToolError) as exc_info:
            await tools.ha_manage_theme(action="set", theme_name="nope")

        payload = _tool_error_payload(exc_info)
        assert payload["success"] is False
        assert "nope" in payload["error"]["message"]
        assert any(
            "action='list'" in s for s in payload["error"].get("suggestions", [])
        )
