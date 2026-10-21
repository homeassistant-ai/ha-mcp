"""A refused app port must explain listener setup before blaming routing."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_addons import _call_addon_api, _call_addon_ws

from .test_tools_addons import (
    _RUNNING_ADDON_INFO,
    _make_mock_client,
    _parse_tool_error,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("websocket", [False, True])
async def test_refused_app_port_explains_listener_setup(monkeypatch, websocket):
    """Both proxy transports must return actionable setup and auth guidance."""
    monkeypatch.setenv("SUPERVISOR_TOKEN", "supervisor-test-token")
    client = _make_mock_client()
    with (
        patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO,
        ),
        patch("ha_mcp.tools.tools_addons.httpx.AsyncClient") as mock_httpx,
        patch("ha_mcp.tools.tools_addons.websockets.connect") as mock_ws_connect,
    ):
        if websocket:
            mock_ws_connect.return_value.__aenter__ = AsyncMock(
                side_effect=OSError("Connection refused")
            )
            call = _call_addon_ws
        else:
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.ConnectError(
                "Connection refused"
            )
            mock_httpx.return_value.__aenter__ = AsyncMock(
                return_value=mock_http_client
            )
            call = _call_addon_api

        with pytest.raises(ToolError) as exc_info:
            await call(client, "test_addon", "/", port=6052)

    result = _parse_tool_error(exc_info)
    assert result["error"]["code"] == "CONNECTION_FAILED"
    guidance = " ".join(result["error"]["suggestions"])
    assert "ha_get_app(slug='test_addon')" in guidance
    assert "6052" in guidance and "Network" in guidance
    assert "listening" in guidance and "restart" in guidance.lower()
    assert "leave_front_door_open" in guidance and "authentication" in guidance
    assert "user" in guidance and "explicit" in guidance
