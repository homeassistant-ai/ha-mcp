"""Unit tests for add-on tools (_call_addon_api and _call_addon_ws error paths)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import websockets.exceptions
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_addons import _call_addon_api, _call_addon_ws

# Standard mock return for a running addon with Ingress support
_RUNNING_ADDON_INFO = {
    "success": True,
    "addon": {
        "name": "Test Addon",
        "slug": "test_addon",
        "ingress": True,
        "state": "started",
        "ingress_entry": "/api/hassio_ingress/abc123",
        "ip_address": "172.30.33.99",
        "ingress_port": 5000,
    },
}


def _make_mock_client() -> MagicMock:
    """Create a mock HomeAssistantClient."""
    client = MagicMock()
    client.base_url = "http://localhost:8123"
    client.token = "test-token"
    return client


def _parse_tool_error(exc_info: pytest.ExceptionInfo[ToolError]) -> dict:
    """Parse the JSON payload from a ToolError."""
    return json.loads(str(exc_info.value))


class TestCallAddonApiErrors:
    """Tests for _call_addon_api error paths."""

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self):
        """Paths containing '..' components should be rejected."""
        client = _make_mock_client()
        with pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "../../etc/passwd")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "traversal" in result["error"]["message"].lower() or ".." in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_traversal_middle_segment(self):
        """Paths with '..' in the middle should also be rejected."""
        client = _make_mock_client()
        with pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "api/../secret/data")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert ".." in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_with_dotdot_in_name_allowed(self):
        """Paths where '..' is part of a filename (not a segment) should pass traversal check."""
        client = _make_mock_client()

        # "..foo" is not a ".." path segment, so it should pass the traversal check
        # but it will fail on the addon info lookup (next step)
        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value={"success": False, "error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found"}},
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_api(client, "test_addon", "..foo/bar")

        # Should have passed traversal check and failed on addon lookup instead
        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "Not found" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_addon_not_found(self):
        """Should raise ToolError when add-on slug doesn't exist."""
        client = _make_mock_client()
        error_response = {
            "success": False,
            "error": {"code": "RESOURCE_NOT_FOUND", "message": "Add-on 'fake_addon' not found"},
        }

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=error_response,
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "fake_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_no_ingress_support(self):
        """Should raise ToolError when add-on doesn't support Ingress."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": False,
                    "state": "started",
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_not_running(self):
        """Should raise ToolError when add-on is not running."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "stopped",
                    "ingress_entry": "/api/hassio_ingress/abc123",
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not running" in result["error"]["message"].lower()
        assert "stopped" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_addon_no_ingress_entry(self):
        """Should raise ToolError when add-on has Ingress but no entry path."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "started",
                    "ingress_entry": "",
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "ingress_entry" in result["error"]["message"].lower() or "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_missing_network_info(self):
        """Should raise ToolError when add-on is missing ip_address or ingress_port."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "started",
                    "ip_address": "",
                    "ingress_port": None,
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "network info" in result["error"]["message"].lower() or "ip_address" in str(result).lower()

    @pytest.mark.asyncio
    async def test_http_timeout(self):
        """Should raise ToolError when add-on API doesn't respond."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO,
        ), patch(
            "ha_mcp.tools.tools_addons.httpx.AsyncClient",
        ) as mock_httpx:
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.TimeoutException("timed out")
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_api(client, "test_addon", "/api/test", timeout=5)

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "timeout" in result["error"]["message"].lower() or "timed out" in str(result).lower()

    @pytest.mark.asyncio
    async def test_http_connection_error(self):
        """Should raise ToolError when can't reach add-on."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO,
        ), patch(
            "ha_mcp.tools.tools_addons.httpx.AsyncClient",
        ) as mock_httpx:
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.ConnectError("Connection refused")
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_http_client)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "connect" in result["error"]["message"].lower() or "connection" in str(result).lower()


# Standard mock return for a running addon with Ingress support (for WS tests)
_RUNNING_ADDON_INFO_WS = {
    "success": True,
    "addon": {
        "name": "Test Addon",
        "slug": "test_addon",
        "ingress": True,
        "state": "started",
        "ingress_entry": "/api/hassio_ingress/abc123",
        "ip_address": "172.30.33.99",
        "ingress_port": 5000,
    },
}


class TestCallAddonWsErrors:
    """Tests for _call_addon_ws error paths."""

    @pytest.mark.asyncio
    async def test_ws_path_traversal_rejected(self):
        """Paths containing '..' components should be rejected."""
        client = _make_mock_client()
        with pytest.raises(ToolError) as exc_info:
            await _call_addon_ws(client, "test_addon", "../../etc/passwd")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "traversal" in result["error"]["message"].lower() or ".." in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_ws_addon_not_found(self):
        """Should raise ToolError when add-on slug doesn't exist."""
        client = _make_mock_client()
        error_response = {
            "success": False,
            "error": {"code": "RESOURCE_NOT_FOUND", "message": "Add-on 'fake_addon' not found"},
        }

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=error_response,
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_ws(client, "fake_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_addon_no_ingress_support(self):
        """Should raise ToolError when add-on doesn't support Ingress and no port override."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": False,
                    "state": "started",
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_addon_no_ingress_with_port_override(self):
        """Should succeed past Ingress check when port override is provided."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": False,
                    "state": "started",
                    "ip_address": "172.30.33.99",
                },
            },
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            # Simulate a quick connection that closes immediately
            mock_ws = AsyncMock()
            mock_ws.recv.side_effect = websockets.exceptions.ConnectionClosed(None, None)
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(client, "test_addon", "/compile", port=6052)

        # Should have passed the Ingress check (port override bypasses it)
        assert result["success"] is True
        assert result["closed_by"] == "server_closed"

    @pytest.mark.asyncio
    async def test_ws_addon_not_running(self):
        """Should raise ToolError when add-on is not running."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "stopped",
                    "ingress_entry": "/api/hassio_ingress/abc123",
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not running" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_handshake_failure(self):
        """Should raise ToolError when WebSocket handshake fails."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws_connect.return_value.__aenter__ = AsyncMock(
                side_effect=websockets.exceptions.InvalidHandshake("403 Forbidden"),
            )
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "handshake" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_connection_closed_during_send(self):
        """Should raise ToolError when connection closes during send."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws = AsyncMock()
            mock_ws.send.side_effect = websockets.exceptions.ConnectionClosed(None, None)
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_ws(
                    client, "test_addon", "/compile",
                    body={"type": "spawn", "configuration": "test.yaml"},
                )

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "closed unexpectedly" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_connection_error(self):
        """Should raise ToolError when can't connect to add-on WebSocket."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws_connect.return_value.__aenter__ = AsyncMock(
                side_effect=OSError("Connection refused"),
            )
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "connect" in result["error"]["message"].lower() or "connection" in str(result).lower()

    @pytest.mark.asyncio
    async def test_ws_collects_messages(self):
        """Should collect text messages and parse JSON ones."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws = AsyncMock()
            # Simulate 3 messages then connection close
            mock_ws.recv.side_effect = [
                '{"event": "line", "data": "Compiling..."}',
                '{"event": "line", "data": "Done."}',
                '{"event": "exit", "code": 0}',
                websockets.exceptions.ConnectionClosed(None, None),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(client, "test_addon", "/compile")

        assert result["success"] is True
        assert result["message_count"] == 3
        assert result["closed_by"] == "server_closed"
        # JSON messages should be parsed
        assert result["messages"][0] == {"event": "line", "data": "Compiling..."}
        assert result["messages"][2] == {"event": "exit", "code": 0}

    @pytest.mark.asyncio
    async def test_ws_strips_ansi_codes(self):
        """Should strip ANSI escape codes from messages."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws = AsyncMock()
            mock_ws.recv.side_effect = [
                "\x1b[32mSUCCESS\x1b[0m Build complete",
                websockets.exceptions.ConnectionClosed(None, None),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(client, "test_addon", "/compile")

        assert result["success"] is True
        assert result["messages"][0] == "SUCCESS Build complete"

    @pytest.mark.asyncio
    async def test_ws_skips_binary_frames(self):
        """Should skip binary WebSocket frames."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws = AsyncMock()
            mock_ws.recv.side_effect = [
                b"\x00\x01\x02",  # binary frame, should be skipped
                "text message",
                websockets.exceptions.ConnectionClosed(None, None),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(client, "test_addon", "/compile")

        assert result["success"] is True
        assert result["message_count"] == 1
        assert result["messages"][0] == "text message"

    @pytest.mark.asyncio
    async def test_ws_wait_for_close_false_returns_early(self):
        """With wait_for_close=False, should return after silence timeout."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value=_RUNNING_ADDON_INFO_WS,
        ), patch(
            "ha_mcp.tools.tools_addons.websockets.connect",
        ) as mock_ws_connect:
            mock_ws = AsyncMock()
            # First message arrives, then silence (TimeoutError)
            mock_ws.recv.side_effect = [
                "first response",
                TimeoutError(),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client, "test_addon", "/events",
                wait_for_close=False, timeout=10,
            )

        assert result["success"] is True
        assert result["message_count"] == 1
        assert result["closed_by"] == "silence"

    @pytest.mark.asyncio
    async def test_ws_missing_network_info(self):
        """Should raise ToolError when add-on is missing ip_address."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "addon": {
                    "name": "Test Addon",
                    "slug": "test_addon",
                    "ingress": True,
                    "state": "started",
                    "ip_address": "",
                    "ingress_port": None,
                },
            },
        ), pytest.raises(ToolError) as exc_info:
            await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "network info" in result["error"]["message"].lower() or "ip_address" in str(result).lower()
