"""Unit tests for add-on tools (_call_addon_api, _call_addon_ws, and list_addons)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import websockets.exceptions
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_addons import _call_addon_api, _call_addon_ws, list_addons

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
        assert (
            "traversal" in result["error"]["message"].lower()
            or ".." in result["error"]["message"]
        )

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
                return_value={
                    "success": False,
                    "error": {"code": "RESOURCE_NOT_FOUND", "message": "Not found"},
                },
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
            "error": {
                "code": "RESOURCE_NOT_FOUND",
                "message": "Add-on 'fake_addon' not found",
            },
        }

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=error_response,
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_api(client, "fake_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_no_ingress_support(self):
        """Should raise ToolError when add-on doesn't support Ingress."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_addon_not_running(self):
        """Should raise ToolError when add-on is not running."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not running" in result["error"]["message"].lower()
        assert "stopped" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_addon_no_ingress_entry(self):
        """Should raise ToolError when add-on has Ingress but no entry path."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert (
            "ingress_entry" in result["error"]["message"].lower()
            or "ingress" in result["error"]["message"].lower()
        )

    @pytest.mark.asyncio
    async def test_addon_missing_network_info(self):
        """Should raise ToolError when add-on is missing ip_address or ingress_port."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert (
            "network info" in result["error"]["message"].lower()
            or "ip_address" in str(result).lower()
        )

    @pytest.mark.asyncio
    async def test_http_timeout(self):
        """Should raise ToolError when add-on API doesn't respond."""
        client = _make_mock_client()

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO,
            ),
            patch(
                "ha_mcp.tools.tools_addons.httpx.AsyncClient",
            ) as mock_httpx,
        ):
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.TimeoutException("timed out")
            mock_httpx.return_value.__aenter__ = AsyncMock(
                return_value=mock_http_client
            )
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_api(client, "test_addon", "/api/test", timeout=5)

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert (
            "timeout" in result["error"]["message"].lower()
            or "timed out" in str(result).lower()
        )

    @pytest.mark.asyncio
    async def test_http_connection_error(self):
        """Should raise ToolError when can't reach add-on."""
        client = _make_mock_client()

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO,
            ),
            patch(
                "ha_mcp.tools.tools_addons.httpx.AsyncClient",
            ) as mock_httpx,
        ):
            mock_http_client = AsyncMock()
            mock_http_client.request.side_effect = httpx.ConnectError(
                "Connection refused"
            )
            mock_httpx.return_value.__aenter__ = AsyncMock(
                return_value=mock_http_client
            )
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_api(client, "test_addon", "/api/test")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert (
            "connect" in result["error"]["message"].lower()
            or "connection" in str(result).lower()
        )


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
        assert (
            "traversal" in result["error"]["message"].lower()
            or ".." in result["error"]["message"]
        )

    @pytest.mark.asyncio
    async def test_ws_addon_not_found(self):
        """Should raise ToolError when add-on slug doesn't exist."""
        client = _make_mock_client()
        error_response = {
            "success": False,
            "error": {
                "code": "RESOURCE_NOT_FOUND",
                "message": "Add-on 'fake_addon' not found",
            },
        }

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=error_response,
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_ws(client, "fake_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not found" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_addon_no_ingress_support(self):
        """Should raise ToolError when add-on doesn't support Ingress and no port override."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "ingress" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_addon_no_ingress_with_port_override(self):
        """Should succeed past Ingress check when port override is provided."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
            # Simulate a quick connection that closes immediately
            mock_ws = AsyncMock()
            mock_ws.recv.side_effect = websockets.exceptions.ConnectionClosed(
                None, None
            )
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

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "not running" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_handshake_failure(self):
        """Should raise ToolError when WebSocket handshake fails."""
        client = _make_mock_client()

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
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

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
            mock_ws = AsyncMock()
            mock_ws.send.side_effect = websockets.exceptions.ConnectionClosed(
                None, None
            )
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_ws(
                    client,
                    "test_addon",
                    "/compile",
                    body={"type": "spawn", "configuration": "test.yaml"},
                )

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "closed unexpectedly" in result["error"]["message"].lower()

    @pytest.mark.asyncio
    async def test_ws_connection_error(self):
        """Should raise ToolError when can't connect to add-on WebSocket."""
        client = _make_mock_client()

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
            mock_ws_connect.return_value.__aenter__ = AsyncMock(
                side_effect=OSError("Connection refused"),
            )
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert (
            "connect" in result["error"]["message"].lower()
            or "connection" in str(result).lower()
        )

    @pytest.mark.asyncio
    async def test_ws_collects_messages(self):
        """Should collect text messages and parse JSON ones."""
        client = _make_mock_client()

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
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

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
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

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
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

        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO_WS,
            ),
            patch(
                "ha_mcp.tools.tools_addons.websockets.connect",
            ) as mock_ws_connect,
        ):
            mock_ws = AsyncMock()
            # First message arrives, then silence (TimeoutError)
            mock_ws.recv.side_effect = [
                "first response",
                TimeoutError(),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client,
                "test_addon",
                "/events",
                wait_for_close=False,
                timeout=10,
            )

        assert result["success"] is True
        assert result["message_count"] == 1
        assert result["closed_by"] == "silence"

    @pytest.mark.asyncio
    async def test_ws_missing_network_info(self):
        """Should raise ToolError when add-on is missing ip_address."""
        client = _make_mock_client()

        with (
            patch(
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
            ),
            pytest.raises(ToolError) as exc_info,
        ):
            await _call_addon_ws(client, "test_addon", "/compile")

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert (
            "network info" in result["error"]["message"].lower()
            or "ip_address" in str(result).lower()
        )


# Mock Supervisor API responses for list_addons tests
_ADDONS_LIST_RESPONSE = {
    "success": True,
    "result": {
        "addons": [
            {
                "name": "Matter Server",
                "slug": "core_matter_server",
                "description": "Matter support",
                "version": "8.3.0",
                "state": "started",
                "update_available": False,
                "repository": "core",
            },
            {
                "name": "Music Assistant",
                "slug": "music_assistant",
                "description": "Music player",
                "version": "1.4.0",
                "state": "started",
                "update_available": False,
                "repository": "community",
            },
            {
                "name": "Stopped Addon",
                "slug": "stopped_addon",
                "description": "Not running",
                "version": "1.0.0",
                "state": "stopped",
                "update_available": False,
                "repository": "core",
            },
        ],
    },
}

_MATTER_STATS_RESPONSE = {
    "success": True,
    "result": {
        "cpu_percent": 0.5,
        "memory_percent": 2.0,
        "memory_usage": 163987456,
        "memory_limit": 8312754176,
    },
}

_MUSIC_STATS_RESPONSE = {
    "success": True,
    "result": {
        "cpu_percent": 1.2,
        "memory_percent": 10.8,
        "memory_usage": 896094208,
        "memory_limit": 8312754176,
    },
}


class TestListAddonsStats:
    """Tests for list_addons with include_stats=True."""

    @pytest.mark.asyncio
    async def test_include_stats_returns_real_data(self):
        """Running addons should have real stats from /addons/{slug}/stats."""
        client = _make_mock_client()

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons":
                return _ADDONS_LIST_RESPONSE
            if endpoint == "/addons/core_matter_server/stats":
                return _MATTER_STATS_RESPONSE
            if endpoint == "/addons/music_assistant/stats":
                return _MUSIC_STATS_RESPONSE
            return {"success": False}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await list_addons(client, include_stats=True)

        assert result["success"] is True
        addons = {a["slug"]: a for a in result["addons"]}

        # Running addons should have real stats
        matter_stats = addons["core_matter_server"]["stats"]
        assert matter_stats is not None
        assert matter_stats["cpu_percent"] == 0.5
        assert matter_stats["memory_usage"] == 163987456

        music_stats = addons["music_assistant"]["stats"]
        assert music_stats is not None
        assert music_stats["memory_percent"] == 10.8

    @pytest.mark.asyncio
    async def test_stopped_addon_gets_none_stats(self):
        """Stopped addons should get stats=None without making an API call."""
        client = _make_mock_client()
        stats_calls = []

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons":
                return _ADDONS_LIST_RESPONSE
            stats_calls.append(endpoint)
            if endpoint == "/addons/core_matter_server/stats":
                return _MATTER_STATS_RESPONSE
            if endpoint == "/addons/music_assistant/stats":
                return _MUSIC_STATS_RESPONSE
            return {"success": False}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await list_addons(client, include_stats=True)

        addons = {a["slug"]: a for a in result["addons"]}

        # Stopped addon should have None stats
        assert addons["stopped_addon"]["stats"] is None

        # Should NOT have made a stats call for the stopped addon
        assert "/addons/stopped_addon/stats" not in stats_calls

    @pytest.mark.asyncio
    async def test_one_addon_stats_failure_does_not_break_others(self):
        """If one addon's stats fetch fails, others should still return stats."""
        client = _make_mock_client()

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons":
                return _ADDONS_LIST_RESPONSE
            if endpoint == "/addons/core_matter_server/stats":
                raise Exception("Connection reset")
            if endpoint == "/addons/music_assistant/stats":
                return _MUSIC_STATS_RESPONSE
            return {"success": False}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await list_addons(client, include_stats=True)

        assert result["success"] is True
        addons = {a["slug"]: a for a in result["addons"]}

        # Failed addon should have None stats
        assert addons["core_matter_server"]["stats"] is None

        # Other addon should still have real stats
        music_stats = addons["music_assistant"]["stats"]
        assert music_stats is not None
        assert music_stats["memory_percent"] == 10.8

    @pytest.mark.asyncio
    async def test_no_stats_key_without_include_stats(self):
        """When include_stats=False, addons should not have a stats key."""
        client = _make_mock_client()

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value=_ADDONS_LIST_RESPONSE,
        ):
            result = await list_addons(client, include_stats=False)

        assert result["success"] is True
        for addon in result["addons"]:
            assert "stats" not in addon


class TestManageAddon:
    """Tests for ha_manage_addon tool (config mode and proxy mode)."""

    @pytest.fixture
    def mock_mcp(self):
        """Create a mock MCP server that captures registered tools."""
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
        """Create a mock HomeAssistantClient."""
        return _make_mock_client()

    @pytest.fixture
    def manage_addon_tool(self, mock_mcp, mock_client):
        """Register tools and return the ha_manage_addon function."""
        from ha_mcp.tools.tools_addons import register_addon_tools
        register_addon_tools(mock_mcp, mock_client)
        return self.registered_tools["ha_manage_addon"]

    # --- Config mode ---

    @pytest.mark.asyncio
    async def test_config_mode_options(self, manage_addon_tool):
        """Config mode: options dict is POSTed to Supervisor API."""
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value={"success": True, "result": {}},
        ) as mock_sup:
            result = await manage_addon_tool(
                slug="test_addon",
                options={"FF_KIOSK": True, "FF_OPEN_URL": "https://example.com"},
            )

        assert result["success"] is True
        assert "options" in result["updated_fields"]
        mock_sup.assert_called_once()
        call_kwargs = mock_sup.call_args
        assert call_kwargs[0][1] == "/addons/test_addon/options"
        assert call_kwargs[1]["method"] == "POST"
        assert call_kwargs[1]["data"]["options"] == {"FF_KIOSK": True, "FF_OPEN_URL": "https://example.com"}

    @pytest.mark.asyncio
    async def test_config_mode_boot(self, manage_addon_tool):
        """Config mode: boot field is included in POST payload."""
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value={"success": True, "result": {}},
        ) as mock_sup:
            result = await manage_addon_tool(slug="test_addon", boot="manual")

        assert result["success"] is True
        assert "boot" in result["updated_fields"]
        data = mock_sup.call_args[1]["data"]
        assert data == {"boot": "manual"}

    @pytest.mark.asyncio
    async def test_config_mode_auto_update_and_watchdog(self, manage_addon_tool):
        """Config mode: auto_update and watchdog are sent together in one call."""
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value={"success": True, "result": {}},
        ) as mock_sup:
            result = await manage_addon_tool(
                slug="test_addon", auto_update=False, watchdog=True
            )

        assert result["success"] is True
        assert set(result["updated_fields"]) == {"auto_update", "watchdog"}
        data = mock_sup.call_args[1]["data"]
        assert data == {"auto_update": False, "watchdog": True}

    @pytest.mark.asyncio
    async def test_config_mode_network(self, manage_addon_tool):
        """Config mode: network port mapping is sent correctly."""
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value={"success": True, "result": {}},
        ) as mock_sup:
            result = await manage_addon_tool(
                slug="test_addon", network={"5800/tcp": 8082}
            )

        assert result["success"] is True
        assert "network" in result["updated_fields"]
        assert mock_sup.call_args[1]["data"]["network"] == {"5800/tcp": 8082}

    @pytest.mark.asyncio
    async def test_config_mode_supervisor_error_raises(self, manage_addon_tool):
        """Config mode: Supervisor error propagates as ToolError."""
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value={"success": False, "error": "boot_config locked"},
        ), pytest.raises(ToolError):
            await manage_addon_tool(slug="test_addon", boot="auto")

    # --- Validation: mutual exclusion ---

    @pytest.mark.asyncio
    async def test_path_and_config_mutually_exclusive(self, manage_addon_tool):
        """Providing both path and config params raises ToolError."""
        with pytest.raises(ToolError):
            await manage_addon_tool(
                slug="test_addon",
                path="/api/events",
                options={"key": "value"},
            )

    @pytest.mark.asyncio
    async def test_no_path_no_config_raises(self, manage_addon_tool):
        """Providing neither path nor config params raises ToolError."""
        with pytest.raises(ToolError):
            await manage_addon_tool(slug="test_addon")

    @pytest.mark.asyncio
    async def test_proxy_params_in_config_mode_raise(self, manage_addon_tool):
        """Proxy-only params (e.g. method) combined with config params raise ToolError."""
        with pytest.raises(ToolError):
            await manage_addon_tool(
                slug="test_addon",
                options={"key": "val"},
                method="DELETE",
            )

    @pytest.mark.asyncio
    async def test_proxy_params_websocket_in_config_mode_raise(self, manage_addon_tool):
        """websocket=True combined with config params raises ToolError."""
        with pytest.raises(ToolError):
            await manage_addon_tool(
                slug="test_addon",
                auto_update=False,
                websocket=True,
            )

    @pytest.mark.asyncio
    async def test_proxy_params_wait_for_close_in_config_mode_raise(self, manage_addon_tool):
        """wait_for_close=False combined with config params raises ToolError."""
        with pytest.raises(ToolError):
            await manage_addon_tool(
                slug="test_addon",
                auto_update=False,
                wait_for_close=False,
            )

    # --- Proxy mode (backward compat) ---

    @pytest.mark.asyncio
    async def test_proxy_mode_http_delegates_to_call_addon_api(self, manage_addon_tool):
        """Proxy mode: HTTP request is forwarded to _call_addon_api."""
        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            return_value={"success": True, "status": 200, "data": []},
        ) as mock_api:
            result = await manage_addon_tool(slug="test_addon", path="/flows")

        assert result["success"] is True
        mock_api.assert_called_once()
        assert mock_api.call_args[1]["path"] == "/flows"

    @pytest.mark.asyncio
    async def test_proxy_mode_websocket_delegates_to_call_addon_ws(self, manage_addon_tool):
        """Proxy mode: WebSocket request is forwarded to _call_addon_ws."""
        with patch(
            "ha_mcp.tools.tools_addons._call_addon_ws",
            return_value={"success": True, "messages": []},
        ) as mock_ws:
            result = await manage_addon_tool(
                slug="test_addon",
                path="/validate",
                websocket=True,
            )

        assert result["success"] is True
        mock_ws.assert_called_once()
        assert mock_ws.call_args[1]["path"] == "/validate"

    @pytest.mark.asyncio
    async def test_proxy_mode_invalid_http_method_raises(self, manage_addon_tool):
        """Proxy mode: invalid HTTP method raises ToolError."""
        with patch(
            "ha_mcp.tools.tools_addons.get_addon_info",
            return_value=_RUNNING_ADDON_INFO,
        ), pytest.raises(ToolError):
            await manage_addon_tool(
                slug="test_addon", path="/flows", method="INVALID"
            )
