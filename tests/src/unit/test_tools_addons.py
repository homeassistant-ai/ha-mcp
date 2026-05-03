"""Unit tests for add-on tools (_call_addon_api, _call_addon_ws, and list_addons)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import websockets.exceptions
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_addons import (
    _apply_response_transform,
    _call_addon_api,
    _call_addon_ws,
    _extract_addon_log_level,
    _is_signal_message,
    _slice_ws_messages,
    _summarize_ws_messages,
    get_addon_info,
    list_addons,
)

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


class TestSliceWsMessages:
    """Tests for the _slice_ws_messages helper (pure function)."""

    def test_no_offset_no_limit_returns_all(self):
        """Without offset/limit, all collected messages pass through."""
        messages = [1, 2, 3, 4, 5]
        sliced, meta = _slice_ws_messages(messages, offset=0, limit=None)
        assert sliced == messages
        assert meta == {"total_collected": 5, "offset": 0, "returned": 5}

    def test_limit_truncates(self):
        """Limit caps the returned count."""
        sliced, meta = _slice_ws_messages([1, 2, 3, 4, 5], offset=0, limit=3)
        assert sliced == [1, 2, 3]
        assert meta["returned"] == 3
        assert meta["limit"] == 3

    def test_offset_skips_head(self):
        """Offset drops the first N messages."""
        sliced, _ = _slice_ws_messages([1, 2, 3, 4, 5], offset=2, limit=None)
        assert sliced == [3, 4, 5]

    def test_offset_plus_limit(self):
        """Offset + limit selects a window."""
        sliced, meta = _slice_ws_messages([1, 2, 3, 4, 5], offset=1, limit=2)
        assert sliced == [2, 3]
        assert meta["offset"] == 1
        assert meta["limit"] == 2
        assert meta["returned"] == 2

    def test_offset_beyond_total_returns_empty(self):
        """Offset past the end yields an empty slice but stable metadata."""
        sliced, meta = _slice_ws_messages([1, 2, 3], offset=10, limit=None)
        assert sliced == []
        assert meta["total_collected"] == 3
        assert meta["returned"] == 0

    def test_negative_offset_clamped_to_zero(self):
        """Negative offset is clamped — no reverse slicing."""
        sliced, _ = _slice_ws_messages([1, 2, 3], offset=-5, limit=None)
        assert sliced == [1, 2, 3]

    def test_negative_limit_clamped_to_zero(self):
        """Negative limit is clamped to zero (empty return)."""
        sliced, _ = _slice_ws_messages([1, 2, 3], offset=0, limit=-1)
        assert sliced == []


class TestIsSignalMessage:
    """Tests for the _is_signal_message heuristic."""

    def test_info_log_is_signal(self):
        assert _is_signal_message("INFO Reading configuration")

    def test_warning_log_is_signal(self):
        assert _is_signal_message("WARNING Something looks off")
        assert _is_signal_message("WARN deprecated setting")

    def test_error_log_is_signal(self):
        assert _is_signal_message("ERROR Compile failed")
        assert _is_signal_message({"level": "ERROR", "msg": "boom"})

    def test_exit_event_is_signal(self):
        assert _is_signal_message({"event": "exit", "code": 0})
        assert _is_signal_message({"returncode": 1})

    def test_config_valid_is_signal(self):
        assert _is_signal_message("Configuration is valid!")

    def test_yaml_dump_line_is_not_signal(self):
        """Plain YAML-shaped lines are non-signal (expected to be elided)."""
        assert not _is_signal_message("  - platform: gpio")
        assert not _is_signal_message("sensor:")
        assert not _is_signal_message("    pin: GPIO0")


class TestSummarizeWsMessages:
    """Tests for the _summarize_ws_messages heuristic."""

    def test_short_non_signal_run_passes_through(self):
        """A run shorter than the threshold is not elided."""
        messages = ["  key1: val", "  key2: val", "  key3: val"]
        result, meta = _summarize_ws_messages(messages, run_threshold=10)
        assert result == messages
        assert meta["elided_count"] == 0

    def test_long_non_signal_run_is_elided(self):
        """A run ≥ threshold is collapsed with context kept at each end."""
        messages = [f"  line_{i}: value" for i in range(50)]
        result, meta = _summarize_ws_messages(
            messages, run_threshold=10, context_keep=2
        )
        # 2 head + 1 elision marker + 2 tail = 5 entries
        assert len(result) == 5
        assert result[0] == "  line_0: value"
        assert result[1] == "  line_1: value"
        assert isinstance(result[2], dict)
        assert result[2]["elided"] == 46
        assert "summarize=False" in result[2]["note"]
        assert result[3] == "  line_48: value"
        assert result[4] == "  line_49: value"
        assert meta["original_count"] == 50
        assert meta["elided_count"] == 46

    def test_signal_messages_split_runs(self):
        """Signal messages break non-signal runs so each run is checked separately."""
        messages = (
            [f"  k{i}: v" for i in range(5)]
            + ["INFO something happened"]
            + [f"  k{i}: v" for i in range(5)]
        )
        result, meta = _summarize_ws_messages(messages, run_threshold=10)
        # Neither 5-long run is ≥ threshold → nothing elided
        assert result == messages
        assert meta["elided_count"] == 0

    def test_mixed_with_esphome_validate_shape(self):
        """Simulates a realistic ESPHome /validate stream: header signals, big
        YAML dump, trailing success signal."""
        messages = (
            ["INFO Reading configuration motion1.yaml..."]
            + [f"    key_{i}: val_{i}" for i in range(100)]
            + [
                "INFO Configuration is valid!",
                {"event": "exit", "code": 0},
            ]
        )
        result, meta = _summarize_ws_messages(messages, run_threshold=10)
        # 1 header INFO + (2 context + 1 elided + 2 context) + 2 trailing signals
        assert len(result) == 8
        assert "Reading configuration" in str(result[0])
        assert result[1] == "    key_0: val_0"
        assert isinstance(result[3], dict)
        assert result[3]["elided"] == 96
        assert "Configuration is valid" in str(result[6])
        assert result[7] == {"event": "exit", "code": 0}
        assert meta["elided_count"] == 96

    def test_heterogeneous_dict_and_string_list(self):
        """Transforms and summarize both accept list[dict | str] (explicit shape contract)."""
        messages: list = [
            {"level": "INFO", "text": "hi"},
            "plain string 1",
            "plain string 2",
            {"event": "exit"},
        ]
        result, meta = _summarize_ws_messages(messages, run_threshold=10)
        # Short run, no elision
        assert result == messages
        assert meta["original_count"] == 4


class TestApplyResponseTransform:
    """Tests for _apply_response_transform wrapper."""

    def test_filter_list(self):
        """A reassigning expression narrows a list."""
        messages = [{"level": "INFO"}, {"level": "ERROR"}, {"level": "INFO"}]
        result = _apply_response_transform(
            messages,
            "response = [m for m in response if m.get('level') == 'ERROR']",
        )
        assert result == [{"level": "ERROR"}]

    def test_in_place_mutation(self):
        """An in-place mutation is reflected in the return value."""
        messages = [1, 2, 3]
        result = _apply_response_transform(messages, "response.append(4)")
        assert result == [1, 2, 3, 4]

    def test_heterogeneous_shape(self):
        """Mixed dict/string messages (WS shape) can be transformed."""
        messages = [
            {"level": "INFO", "msg": "start"},
            "raw text",
            {"level": "ERROR", "msg": "boom"},
            "another raw",
        ]
        # Filter by stringified form — works uniformly on dicts and strings.
        result = _apply_response_transform(
            messages,
            "response = [m for m in response if 'ERROR' in str(m) or 'raw' in str(m)]",
        )
        assert len(result) == 3
        assert "raw text" in result
        assert {"level": "ERROR", "msg": "boom"} in result

    def test_invalid_expression_raises_tool_error(self):
        """Forbidden operations raise ToolError with VALIDATION_FAILED."""
        with pytest.raises(ToolError) as exc_info:
            _apply_response_transform([1, 2, 3], "import os")
        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "python_transform failed" in result["error"]["message"]

    def test_runtime_error_raises_tool_error(self):
        """Runtime execution errors surface as ToolError with preview."""
        with pytest.raises(ToolError) as exc_info:
            _apply_response_transform(
                {}, "response['missing']['key'] = 1"
            )
        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "python_transform failed" in result["error"]["message"]


class TestCallAddonWsNewParams:
    """Integration tests for message_limit/offset/summarize/python_transform in _call_addon_ws."""

    @pytest.mark.asyncio
    async def test_message_limit_caps_collection(self):
        """message_limit lowers the collection cap so we stop early."""
        client = _make_mock_client()

        # Produce many messages, then a close
        messages_to_send = [f"msg {i}" for i in range(100)]
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
            mock_ws.recv.side_effect = messages_to_send + [
                websockets.exceptions.ConnectionClosed(None, None)
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client,
                "test_addon",
                "/compile",
                message_limit=5,
                summarize=False,
            )

        assert result["success"] is True
        assert result["closed_by"] == "message_limit"
        assert result["message_count"] == 5
        assert result["pagination"]["total_collected"] == 5
        assert result["pagination"]["limit"] == 5

    @pytest.mark.asyncio
    async def test_safety_ceiling_distinct_from_message_limit(self):
        """Hitting the global ceiling without a caller-set message_limit
        reports closed_by="safety_ceiling", not "message_limit"."""
        client = _make_mock_client()

        messages_to_send = [f"msg {i}" for i in range(10)]
        with (
            patch(
                "ha_mcp.tools.tools_addons._MAX_WS_MESSAGES",
                5,
            ),
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
            mock_ws.recv.side_effect = messages_to_send + [
                websockets.exceptions.ConnectionClosed(None, None)
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client,
                "test_addon",
                "/compile",
                summarize=False,
            )

        assert result["success"] is True
        assert result["closed_by"] == "safety_ceiling"
        assert result["message_count"] == 5

    @pytest.mark.asyncio
    async def test_message_offset_skips_head(self):
        """message_offset drops the first N messages from the returned list."""
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
                "msg 0",
                "msg 1",
                "msg 2",
                "msg 3",
                websockets.exceptions.ConnectionClosed(None, None),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client,
                "test_addon",
                "/logs",
                message_offset=2,
                summarize=False,
            )

        assert result["success"] is True
        assert result["messages"] == ["msg 2", "msg 3"]
        assert result["pagination"]["offset"] == 2
        assert result["pagination"]["total_collected"] == 4

    @pytest.mark.asyncio
    async def test_summarize_elides_yaml_dump(self):
        """The summarize pass collapses a long non-signal run from the WS stream."""
        client = _make_mock_client()

        # 30 plain YAML-ish lines with one surrounding INFO on each end
        yaml_lines = [f"  key_{i}: value" for i in range(30)]
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
            mock_ws.recv.side_effect = (
                ["INFO Reading configuration..."]
                + yaml_lines
                + [
                    "INFO Configuration is valid!",
                    websockets.exceptions.ConnectionClosed(None, None),
                ]
            )
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(client, "test_addon", "/validate")

        assert result["success"] is True
        # 1 header + 2 context + 1 elision + 2 context + 1 footer = 7
        assert result["message_count"] == 7
        assert "summary" in result
        assert result["summary"]["elided_count"] == 26
        # Verify the elision marker is present
        assert any(
            isinstance(m, dict) and m.get("elided") == 26 for m in result["messages"]
        )

    @pytest.mark.asyncio
    async def test_summarize_false_returns_raw_stream(self):
        """With summarize=False, no elision happens."""
        client = _make_mock_client()

        yaml_lines = [f"  key_{i}: value" for i in range(30)]
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
            mock_ws.recv.side_effect = yaml_lines + [
                websockets.exceptions.ConnectionClosed(None, None)
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client, "test_addon", "/validate", summarize=False
            )

        assert result["success"] is True
        assert result["message_count"] == 30
        assert "summary" not in result

    @pytest.mark.asyncio
    async def test_python_transform_filters_messages(self):
        """python_transform post-processes the message list after summarize."""
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
                '{"level": "INFO", "msg": "start"}',
                '{"level": "ERROR", "msg": "boom"}',
                '{"level": "INFO", "msg": "done"}',
                websockets.exceptions.ConnectionClosed(None, None),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_ws(
                client,
                "test_addon",
                "/validate",
                summarize=False,
                python_transform=(
                    "response = [m for m in response if 'ERROR' in str(m)]"
                ),
            )

        assert result["success"] is True
        assert result["transformed"] is True
        assert result["pre_transform_message_count"] == 3
        assert result["message_count"] == 1
        assert result["messages"][0]["level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_python_transform_invalid_raises(self):
        """Invalid python_transform surfaces VALIDATION_FAILED as ToolError."""
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
                "msg",
                websockets.exceptions.ConnectionClosed(None, None),
            ]
            mock_ws_connect.return_value.__aenter__ = AsyncMock(return_value=mock_ws)
            mock_ws_connect.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_ws(
                    client,
                    "test_addon",
                    "/logs",
                    python_transform="import os",
                )

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "python_transform failed" in result["error"]["message"]


class TestCallAddonApiPythonTransform:
    """Tests for python_transform in HTTP mode (_call_addon_api)."""

    @pytest.mark.asyncio
    async def test_transform_applies_to_json_array(self):
        """Transform reshapes a JSON array response before return."""
        client = _make_mock_client()
        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO,
            ),
            patch("ha_mcp.tools.tools_addons.httpx.AsyncClient") as mock_httpx,
        ):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.json.return_value = [
                {"id": 1, "name": "alice"},
                {"id": 2, "name": "bob"},
            ]
            mock_client_ctx = AsyncMock()
            mock_client_ctx.request = AsyncMock(return_value=mock_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_client_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_api(
                client,
                "test_addon",
                "/users",
                python_transform="response = [u['id'] for u in response]",
            )

        assert result["success"] is True
        assert result["transformed"] is True
        assert result["response"] == [1, 2]

    @pytest.mark.asyncio
    async def test_transform_applies_to_dict_body(self):
        """Transform on dict content-type."""
        client = _make_mock_client()
        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO,
            ),
            patch("ha_mcp.tools.tools_addons.httpx.AsyncClient") as mock_httpx,
        ):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.json.return_value = {"status": "ok", "details": "..." * 100}
            mock_client_ctx = AsyncMock()
            mock_client_ctx.request = AsyncMock(return_value=mock_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_client_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await _call_addon_api(
                client,
                "test_addon",
                "/status",
                python_transform="del response['details']",
            )

        assert result["success"] is True
        assert result["transformed"] is True
        assert result["response"] == {"status": "ok"}

    @pytest.mark.asyncio
    async def test_transform_invalid_raises(self):
        """HTTP mode: invalid transform raises ToolError."""
        client = _make_mock_client()
        with (
            patch(
                "ha_mcp.tools.tools_addons.get_addon_info",
                new_callable=AsyncMock,
                return_value=_RUNNING_ADDON_INFO,
            ),
            patch("ha_mcp.tools.tools_addons.httpx.AsyncClient") as mock_httpx,
        ):
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.headers = {"content-type": "application/json"}
            mock_response.json.return_value = {"x": 1}
            mock_client_ctx = AsyncMock()
            mock_client_ctx.request = AsyncMock(return_value=mock_response)
            mock_httpx.return_value.__aenter__ = AsyncMock(return_value=mock_client_ctx)
            mock_httpx.return_value.__aexit__ = AsyncMock(return_value=False)

            with pytest.raises(ToolError) as exc_info:
                await _call_addon_api(
                    client,
                    "test_addon",
                    "/x",
                    python_transform="open('/etc/passwd')",
                )

        result = _parse_tool_error(exc_info)
        assert result["success"] is False
        assert "python_transform failed" in result["error"]["message"]


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
        """Config mode: options are merged with current values then POSTed."""

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {"FF_KIOSK": False, "FF_OPEN_URL": "https://old.example.com"},
                        "schema": [
                            {"name": "FF_KIOSK", "required": False, "type": "bool"},
                            {"name": "FF_OPEN_URL", "required": False, "type": "str"},
                        ],
                    },
                }
            # POST /addons/test_addon/options
            return {"success": True, "result": {}}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await manage_addon_tool(
                slug="test_addon",
                options={"FF_OPEN_URL": "https://example.com"},
            )

        assert result["status"] == "pending_restart"
        assert result["submitted_fields"] == ["options"]
        # Caller only sent FF_OPEN_URL; FF_KIOSK is carried over from current options
        assert "ignored_fields" not in result

    @pytest.mark.asyncio
    async def test_config_mode_options_merge_preserves_required_fields(self, manage_addon_tool):
        """Merge ensures required fields are present even when caller omits them (Bug A fix)."""

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {"required_key": "existing_value", "log_level": "info"},
                        "schema": [
                            {"name": "required_key", "required": True, "type": "str"},
                            {"name": "log_level", "required": False, "type": "str"},
                        ],
                    },
                }
            return {"success": True, "result": {}}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ) as mock_sup:
            result = await manage_addon_tool(slug="test_addon", options={"log_level": "debug"})

        assert result["status"] == "pending_restart"
        # POST call should have included required_key from current options
        post_call = [c for c in mock_sup.call_args_list if "method" in c[1]][-1]
        assert post_call[1]["data"]["options"]["required_key"] == "existing_value"
        assert post_call[1]["data"]["options"]["log_level"] == "debug"

    @pytest.mark.asyncio
    async def test_config_mode_options_nested_deep_merge(self, manage_addon_tool):
        """Deep merge preserves sibling fields in nested option dicts (Bug C fix)."""

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {
                            "ssh": {"sftp": False, "authorized_keys": ["key1"]},
                            "log_level": "info",
                        },
                        "schema": [
                            {"name": "ssh", "type": "schema"},
                            {"name": "log_level", "required": False, "type": "str"},
                        ],
                    },
                }
            return {"success": True, "result": {}}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ) as mock_sup:
            result = await manage_addon_tool(slug="test_addon", options={"ssh": {"sftp": True}})

        assert result["status"] == "pending_restart"
        post_call = [c for c in mock_sup.call_args_list if "method" in c[1]][-1]
        merged = post_call[1]["data"]["options"]
        # sftp overridden, authorized_keys preserved, top-level log_level preserved
        assert merged["ssh"]["sftp"] is True
        assert merged["ssh"]["authorized_keys"] == ["key1"]
        assert merged["log_level"] == "info"

    @pytest.mark.asyncio
    async def test_config_mode_options_unknown_fields_warned(self, manage_addon_tool):
        """Unknown option fields are removed pre-write and reported in ignored_fields (Bug B fix)."""

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {"log_level": "info"},
                        "schema": [{"name": "log_level", "required": False, "type": "str"}],
                    },
                }
            return {"success": True, "result": {}}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await manage_addon_tool(
                slug="test_addon",
                options={"log_level": "debug", "zombie_field": "ghost"},
            )

        assert result["status"] == "pending_restart"
        assert "ignored_fields" in result
        assert "zombie_field" in result["ignored_fields"]
        assert "warning" in result

    @pytest.mark.asyncio
    async def test_config_mode_boot(self, manage_addon_tool):
        """Config mode: boot field is included in POST payload."""
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            return_value={"success": True, "result": {}},
        ) as mock_sup:
            result = await manage_addon_tool(slug="test_addon", boot="manual")

        assert result["success"] is True
        assert result["submitted_fields"] == ["boot"]
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
        assert set(result["submitted_fields"]) == {"auto_update", "watchdog"}
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

        assert result["status"] == "pending_restart"
        assert result["submitted_fields"] == ["network"]
        assert mock_sup.call_args[1]["data"]["network"] == {"5800/tcp": 8082}

    @pytest.mark.asyncio
    async def test_config_mode_supervisor_schema_error_raises(self, manage_addon_tool):
        """Config mode: Supervisor schema error on POST /options maps to VALIDATION_FAILED.

        The real pipeline: ws_client.send_command raises
        HomeAssistantCommandError → _supervisor_api_call funnels it
        through exception_to_structured_error → _classify_by_message's
        schema branch recognises the vol.Invalid markers and routes to
        VALIDATION_FAILED (issue #993 fix).

        This test mocks _supervisor_api_call directly and injects the
        already-classified ToolError at the /options boundary. End-to-end
        coverage of the classifier itself (HomeAssistantCommandError →
        VALIDATION_FAILED) lives in TestSupervisorApiCall.
        """
        from ha_mcp.errors import create_validation_error
        from ha_mcp.tools.helpers import raise_tool_error

        async def mock_supervisor_api(client, endpoint, **kwargs):
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {"ssh": {"sftp": False}},
                        "schema": [{"name": "ssh", "required": True, "type": "dict"}],
                    },
                }
            # POST /addons/test_addon/options with a partial nested update:
            # Supervisor rejects with vol.Invalid, classifier produces
            # VALIDATION_FAILED.
            raise_tool_error(
                create_validation_error(
                    "Command failed: Missing option 'authorized_keys' in ssh "
                    "in SSH (core_ssh)",
                )
            )

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ), pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                options={"ssh": {"sftp": True}},
            )
        payload = _parse_tool_error(exc_info)
        assert payload["success"] is False
        assert payload["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_config_mode_all_five_params(self, manage_addon_tool):
        """Config mode: all five config params submitted in a single POST call."""
        call_count = 0
        calls = []

        async def mock_supervisor_api(client, endpoint, **kwargs):
            nonlocal call_count
            call_count += 1
            calls.append((endpoint, kwargs))
            if endpoint == "/addons/test_addon/info":
                return {
                    "success": True,
                    "result": {
                        "options": {},
                        "schema": [{"name": "log_level", "required": False, "type": "str"}],
                    },
                }
            return {"success": True, "result": {}}

        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            side_effect=mock_supervisor_api,
        ):
            result = await manage_addon_tool(
                slug="test_addon",
                options={"log_level": "debug"},
                boot="manual",
                auto_update=False,
                watchdog=True,
                network={"8080/tcp": 9090},
            )

        # GET /info + single POST (not five separate calls)
        assert call_count == 2
        post_call = calls[-1]
        data = post_call[1]["data"]
        assert set(data.keys()) == {"options", "boot", "auto_update", "watchdog", "network"}
        assert set(result["submitted_fields"]) == {"options", "boot", "auto_update", "watchdog", "network"}

    # --- Validation: mutual exclusion ---

    @pytest.mark.asyncio
    async def test_path_and_config_mutually_exclusive(self, manage_addon_tool):
        """Providing both path and config params raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                path="/api/events",
                options={"key": "value"},
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "Cannot combine" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_no_path_no_config_raises(self, manage_addon_tool):
        """Providing neither path nor config params raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(slug="test_addon")
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "path" in error["error"]["message"] or "config" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_path_empty_string_raises(self, manage_addon_tool):
        """Empty string path is explicitly rejected with VALIDATION_FAILED."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(slug="test_addon", path="")
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "path" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_proxy_params_in_config_mode_raise(self, manage_addon_tool):
        """Proxy-only params (e.g. method) combined with config params raise ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                options={"key": "val"},
                method="DELETE",
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "method" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_proxy_params_websocket_in_config_mode_raise(self, manage_addon_tool):
        """websocket=True combined with config params raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                auto_update=False,
                websocket=True,
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "websocket" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_proxy_params_wait_for_close_in_config_mode_raise(self, manage_addon_tool):
        """wait_for_close=False combined with config params raises ToolError."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                auto_update=False,
                wait_for_close=False,
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "wait_for_close" in error["error"]["message"]

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
        ), pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon", path="/flows", method="INVALID"
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "method" in error["error"]["message"] or "INVALID" in error["error"]["message"]

    # --- New WS response-control params (issue #992) ---

    @pytest.mark.asyncio
    async def test_ws_params_forwarded_to_call_addon_ws(self, manage_addon_tool):
        """message_limit/offset/summarize/python_transform reach _call_addon_ws."""
        with patch(
            "ha_mcp.tools.tools_addons._call_addon_ws",
            return_value={"success": True, "messages": []},
        ) as mock_ws:
            await manage_addon_tool(
                slug="test_addon",
                path="/validate",
                websocket=True,
                message_limit=25,
                message_offset=5,
                summarize=False,
                python_transform="response = response[:1]",
            )

        call_kwargs = mock_ws.call_args[1]
        assert call_kwargs["message_limit"] == 25
        assert call_kwargs["message_offset"] == 5
        assert call_kwargs["summarize"] is False
        assert call_kwargs["python_transform"] == "response = response[:1]"

    @pytest.mark.asyncio
    async def test_http_python_transform_forwarded(self, manage_addon_tool):
        """python_transform reaches _call_addon_api in HTTP mode."""
        with patch(
            "ha_mcp.tools.tools_addons._call_addon_api",
            return_value={"success": True, "response": []},
        ) as mock_api:
            await manage_addon_tool(
                slug="test_addon",
                path="/flows",
                python_transform="response = [f['id'] for f in response]",
            )
        assert (
            mock_api.call_args[1]["python_transform"]
            == "response = [f['id'] for f in response]"
        )

    @pytest.mark.asyncio
    async def test_ws_only_params_rejected_in_http_mode(self, manage_addon_tool):
        """HTTP mode rejects message_limit/offset/summarize."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                path="/flows",
                message_limit=10,
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "WebSocket" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_ws_params_rejected_in_config_mode(self, manage_addon_tool):
        """Config mode rejects the new WS-only params."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                auto_update=False,
                message_limit=10,
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "message_limit" in error["error"]["message"]

    @pytest.mark.asyncio
    async def test_python_transform_rejected_in_config_mode(self, manage_addon_tool):
        """Config mode rejects python_transform."""
        with pytest.raises(ToolError) as exc_info:
            await manage_addon_tool(
                slug="test_addon",
                auto_update=False,
                python_transform="response = []",
            )
        error = _parse_tool_error(exc_info)
        assert error["error"]["code"] == "VALIDATION_FAILED"
        assert "python_transform" in error["error"]["message"]


class TestExtractAddonLogLevel:
    """Tests for _extract_addon_log_level — surfaces add-on options.log_level."""

    def test_user_configured_log_level_wins(self):
        """A user-set options.log_level is returned verbatim."""
        assert _extract_addon_log_level({"options": {"log_level": "debug"}}) == "debug"

    def test_empty_user_value_falls_back_to_schema_default(self):
        """An empty string in options falls through to the schema default marker."""
        addon = {
            "options": {"log_level": ""},
            "schema": [{"name": "log_level", "type": "list(info|debug|...)"}],
        }
        assert _extract_addon_log_level(addon) == "default"

    def test_schema_only_returns_default_marker(self):
        """Add-on with log_level in schema but no option set reports 'default'."""
        addon = {
            "options": {},
            "schema": [{"name": "log_level", "type": "list(info|debug|...)"}],
        }
        assert _extract_addon_log_level(addon) == "default"

    def test_no_log_level_returns_none(self):
        """Add-on with no log_level anywhere returns None (field omitted in response)."""
        assert (
            _extract_addon_log_level({"options": {"port": 8080}, "schema": []})
            is None
        )

    def test_schema_without_log_level_returns_none(self):
        """Schema list that doesn't include log_level returns None."""
        addon = {
            "options": {},
            "schema": [{"name": "port", "type": "int"}],
        }
        assert _extract_addon_log_level(addon) is None

    def test_malformed_options_ignored(self):
        """Non-dict options don't crash the extractor."""
        assert _extract_addon_log_level({"options": "not a dict"}) is None

    def test_non_string_log_level_ignored(self):
        """A non-string log_level is not surfaced (avoids leaking junk to users)."""
        addon = {
            "options": {"log_level": 42},
            "schema": [{"name": "log_level", "type": "..."}],
        }
        # Falls through past options (non-string) and then uses schema → "default"
        assert _extract_addon_log_level(addon) == "default"

    def test_schema_dict_legacy_shape_returns_none(self):
        """Legacy dict-shaped schema is no longer recognized (Supervisor returns a list)."""
        addon = {
            "options": {},
            "schema": {"log_level": "list(info|debug|...)"},
        }
        assert _extract_addon_log_level(addon) is None


class TestGetAddonInfoLogLevel:
    """Tests for get_addon_info — verifies top-level log_level enrichment."""

    @pytest.mark.asyncio
    async def test_includes_log_level_when_option_set(self):
        client = _make_mock_client()
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "result": {
                    "name": "Example",
                    "slug": "example",
                    "options": {"log_level": "debug"},
                },
            },
        ):
            result = await get_addon_info(client, "example")

        assert result["success"] is True
        assert result["log_level"] == "debug"
        assert result["addon"]["slug"] == "example"

    @pytest.mark.asyncio
    async def test_omits_log_level_when_addon_has_none(self):
        client = _make_mock_client()
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "result": {
                    "name": "NoLogLevel",
                    "slug": "nll",
                    "options": {"port": 1883},
                },
            },
        ):
            result = await get_addon_info(client, "nll")

        assert result["success"] is True
        assert "log_level" not in result

    @pytest.mark.asyncio
    async def test_passes_through_supervisor_error(self):
        """Error responses shouldn't gain a synthetic log_level field."""
        client = _make_mock_client()
        error_response = {
            "success": False,
            "error": {"code": "RESOURCE_NOT_FOUND", "message": "no supervisor"},
        }
        with patch(
            "ha_mcp.tools.tools_addons._supervisor_api_call",
            new_callable=AsyncMock,
            return_value=error_response,
        ):
            result = await get_addon_info(client, "whatever")

        assert result == error_response


class TestSupervisorApiCall:
    """Tests for Supervisor schema error classification via _classify_by_message.

    The generic classifier in helpers.py routes Supervisor vol.Invalid
    errors to VALIDATION_FAILED regardless of endpoint (issue #993).
    These tests pin that behaviour so the greedy "auth" substring bug
    stays fixed at the source — not patched at a single call site.
    """

    @pytest.mark.asyncio
    async def test_schema_error_on_options_endpoint_classified_as_validation_failed(self):
        """POST /addons/*/options schema reject => VALIDATION_FAILED via classifier."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError
        from ha_mcp.tools.tools_addons import _supervisor_api_call

        mock_ws = MagicMock()
        mock_ws.disconnect = AsyncMock()
        mock_ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: Missing option 'authorized_keys' in ssh "
                "in SSH (core_ssh)",
            )
        )

        with patch(
            "ha_mcp.tools.tools_addons.get_connected_ws_client",
            return_value=(mock_ws, None),
        ), pytest.raises(ToolError) as exc_info:
            await _supervisor_api_call(
                _make_mock_client(),
                "/addons/core_ssh/options",
                method="POST",
                data={"options": {"ssh": {"sftp": True}}},
            )
        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "VALIDATION_FAILED"

    @pytest.mark.asyncio
    async def test_schema_error_on_non_options_endpoint_also_classified(self):
        """Same schema message on a non-/options endpoint => VALIDATION_FAILED.

        Bidirectional assertion: fixes the root cause on every endpoint,
        not just POST /options. The greedy "auth" substring bug at
        helpers.py previously misclassified this as AUTH_INVALID_TOKEN
        because "authorized_keys" contains "auth"; the phrase-list fix
        in _classify_by_message closes that without endpoint gating.
        """
        from ha_mcp.client.rest_client import HomeAssistantCommandError
        from ha_mcp.tools.tools_addons import _supervisor_api_call

        mock_ws = MagicMock()
        mock_ws.disconnect = AsyncMock()
        mock_ws.send_command = AsyncMock(
            side_effect=HomeAssistantCommandError(
                "Command failed: Missing option 'authorized_keys' in ssh",
            )
        )

        with patch(
            "ha_mcp.tools.tools_addons.get_connected_ws_client",
            return_value=(mock_ws, None),
        ), pytest.raises(ToolError) as exc_info:
            await _supervisor_api_call(
                _make_mock_client(),
                "/addons/core_ssh/info",
                method="GET",
            )
        payload = _parse_tool_error(exc_info)
        assert payload["error"]["code"] == "VALIDATION_FAILED"
