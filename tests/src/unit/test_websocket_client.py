"""Unit tests for WebSocket client URL construction.

These tests verify that the WebSocket client correctly constructs WebSocket URLs
for both standard Home Assistant installations and Supervisor proxy environments.
"""

import pytest


class TestWebSocketURLConstruction:
    """Tests for WebSocket URL construction logic."""

    def test_standard_http_url_produces_ws_api_websocket(self):
        """Standard HTTP URL should produce ws://host:port/api/websocket."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )
        assert client.ws_url == "ws://homeassistant.local:8123/api/websocket"

    def test_standard_https_url_produces_wss_api_websocket(self):
        """Standard HTTPS URL should produce wss://host:port/api/websocket."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="https://homeassistant.local:8123",
            token="test-token",
        )
        assert client.ws_url == "wss://homeassistant.local:8123/api/websocket"

    def test_supervisor_proxy_url_produces_core_websocket(self):
        """Supervisor proxy URL should produce ws://supervisor/core/websocket.

        This is critical for add-on WebSocket connections. The Supervisor
        proxies WebSocket connections to Home Assistant at /core/websocket,
        not at /api/websocket.

        Fixes: https://github.com/homeassistant-ai/ha-mcp/issues/186
        Fixes: https://github.com/homeassistant-ai/ha-mcp/issues/189
        """
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://supervisor/core",
            token="test-supervisor-token",
        )
        assert client.ws_url == "ws://supervisor/core/websocket"

    def test_url_with_trailing_slash_is_handled(self):
        """URL with trailing slash should work correctly."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123/",
            token="test-token",
        )
        assert client.ws_url == "ws://homeassistant.local:8123/api/websocket"

    def test_supervisor_url_with_trailing_slash_is_handled(self):
        """Supervisor URL with trailing slash should work correctly."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://supervisor/core/",
            token="test-supervisor-token",
        )
        assert client.ws_url == "ws://supervisor/core/websocket"

    def test_custom_path_url_uses_path_plus_websocket(self):
        """URL with custom path should append /websocket to the path."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://proxy.local/homeassistant",
            token="test-token",
        )
        assert client.ws_url == "ws://proxy.local/homeassistant/websocket"

    def test_localhost_url_produces_standard_websocket_path(self):
        """Localhost URL should use standard /api/websocket path."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://localhost:8123",
            token="test-token",
        )
        assert client.ws_url == "ws://localhost:8123/api/websocket"

    def test_ip_address_url_produces_standard_websocket_path(self):
        """IP address URL should use standard /api/websocket path."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://192.168.1.100:8123",
            token="test-token",
        )
        assert client.ws_url == "ws://192.168.1.100:8123/api/websocket"

    def test_base_url_is_stored_without_trailing_slash(self):
        """Base URL should be stored without trailing slash."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123/",
            token="test-token",
        )
        assert client.base_url == "http://homeassistant.local:8123"

    def test_token_is_stored(self):
        """Token should be stored for authentication."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="my-secret-token",
        )
        assert client.token == "my-secret-token"


class TestSendCommandErrorContract:
    """Tests that pin the HomeAssistantCommandError raise contract.

    ``WebSocketClient.send_command`` and ``send_command_with_event`` raise
    ``HomeAssistantCommandError(f"Command failed: {msg}")`` when Home
    Assistant replies with ``{type: "result", success: False}``. The
    message is derived from the response's ``error`` field — dict
    payloads use ``error["message"]``, string/other payloads use
    ``str(error)``. These tests cover the raise sites at
    ``websocket_client.py`` L443 (send_command) and L524
    (send_command_with_event), which are not exercised by the
    classifier tests (those mock HomeAssistantCommandError directly).

    Mock strategy: stub ``send_json_message`` so that it resolves the
    pending-response future with a pre-built failure payload using the
    message ID carried in the outgoing message. This avoids depending
    on the private message-ID counter and keeps the tests robust to
    internal state changes.
    """

    @staticmethod
    def _prepare_client():
        """Build a client whose state passes is_ready and skips real I/O."""
        from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient

        client = HomeAssistantWebSocketClient(
            url="http://homeassistant.local:8123",
            token="test-token",
        )
        client._state.mark_connected()
        client._state.mark_authenticated()
        return client

    @pytest.mark.asyncio
    async def test_send_command_raises_on_dict_error(self):
        """send_command raises HomeAssistantCommandError with dict error payload."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve_with_failure(message: dict) -> None:
            message_id = message["id"]
            future = client._state._pending_requests.get(message_id)
            assert future is not None, "send_command did not register a pending future"
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_error", "message": "entity not available"},
                }
            )

        client.send_json_message = _resolve_with_failure  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.send_command("test/ping")
        assert "Command failed:" in str(exc_info.value)
        assert "entity not available" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_command_raises_on_string_error(self):
        """send_command raises HomeAssistantCommandError when error is a string."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve_with_failure(message: dict) -> None:
            message_id = message["id"]
            future = client._state._pending_requests.get(message_id)
            assert future is not None, "send_command did not register a pending future"
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": False,
                    "error": "bare string error",
                }
            )

        client.send_json_message = _resolve_with_failure  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.send_command("test/ping")
        assert "Command failed:" in str(exc_info.value)
        assert "bare string error" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_send_command_with_event_raises_on_dict_error(self):
        """send_command_with_event raises HomeAssistantCommandError on failure result."""
        from ha_mcp.client.rest_client import HomeAssistantCommandError

        client = self._prepare_client()

        async def _resolve_with_failure(message: dict) -> None:
            message_id = message["id"]
            future = client._state._pending_requests.get(message_id)
            assert future is not None, "send_command did not register a pending future"
            future.set_result(
                {
                    "id": message_id,
                    "type": "result",
                    "success": False,
                    "error": {"code": "unknown_error", "message": "system_health failure"},
                }
            )

        client.send_json_message = _resolve_with_failure  # type: ignore[method-assign]

        with pytest.raises(HomeAssistantCommandError) as exc_info:
            await client.send_command_with_event("system_health/info")
        assert "Command failed:" in str(exc_info.value)
        assert "system_health failure" in str(exc_info.value)
