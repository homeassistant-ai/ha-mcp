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
@pytest.mark.parametrize("slug", ["test_addon", "5c53de3b_esphome"])
@pytest.mark.parametrize("port", [6052, 9999])
async def test_refused_app_port_explains_listener_setup(
    monkeypatch: pytest.MonkeyPatch, websocket: bool, slug: str, port: int
) -> None:
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
            await call(client, slug, "/", port=port)

    result = _parse_tool_error(exc_info)
    assert result["error"]["code"] == "CONNECTION_FAILED"
    guidance = " ".join(result["error"]["suggestions"])
    assert f"ha_get_app(slug='{slug}')" in guidance
    assert str(port) in guidance and "listening" in guidance
    if slug == "test_addon" or port != 6052:
        assert "leave_front_door_open" not in guidance
        assert "Configuration > Network" not in guidance
    else:
        assert "Network" in guidance and "restart" in guidance.lower()
        assert "leave_front_door_open" in guidance and "authentication" in guidance
        assert "user" in guidance and "explicit" in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("port", [None, 6052])
async def test_connect_timeout_preserves_route_guidance(
    monkeypatch: pytest.MonkeyPatch, port: int | None
) -> None:
    """A request that never connected needs connectivity advice, not status checks."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    client = _make_mock_client()
    with (
        patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO,
        ),
        patch(
            "ha_mcp.tools.tools_addons._create_ingress_session",
            new_callable=AsyncMock,
            return_value="test-session",
        ),
        patch("ha_mcp.tools.tools_addons.httpx.AsyncClient") as mock_httpx,
    ):
        http_client = AsyncMock()
        http_client.request.side_effect = httpx.ConnectTimeout("connect timed out")
        mock_httpx.return_value.__aenter__ = AsyncMock(return_value=http_client)
        with pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "/", port=port)

    result = _parse_tool_error(exc_info)
    guidance = " ".join(
        result["error"].get("suggestions", [result["error"]["suggestion"]])
    )
    assert "still be running" not in guidance
    assert result["error"]["code"] == "CONNECTION_FAILED"
    if port:
        assert "'port' parameter" in guidance and "Ingress" in guidance
    else:
        assert client.base_url in guidance and "connectivity" in guidance


@pytest.mark.asyncio
@pytest.mark.parametrize("websocket", [False, True])
@pytest.mark.parametrize("port", [None, 6052])
async def test_app_timeout_does_not_change_listener_auth(
    monkeypatch: pytest.MonkeyPatch, websocket: bool, port: int | None
) -> None:
    """A connected app timing out must not prompt disabling authentication."""
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
            ws = AsyncMock()
            ws.send.side_effect = TimeoutError("send timed out")
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=ws)
            call = _call_addon_ws
        else:
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.ReadTimeout("read timed out")
            mock_httpx.return_value.__aenter__ = AsyncMock(
                return_value=mock_http_client
            )
            call = _call_addon_api

        with pytest.raises(ToolError) as exc_info:
            await call(
                client, "5c53de3b_esphome", "/", port=port, body={"command": "run"}
            )

    result = _parse_tool_error(exc_info)
    assert result["error"]["code"] == "TIMEOUT_OPERATION"
    guidance = " ".join(
        result["error"].get("suggestions", [result["error"]["suggestion"]])
    )
    assert "leave_front_door_open" not in guidance
    assert "Configuration > Network" not in guidance
    assert "restart" not in guidance.lower()
    assert "'port' parameter" not in guidance
    assert "logs" in guidance.lower() and "retry" in guidance.lower()
