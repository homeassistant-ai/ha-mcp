"""Unit tests for WebSocket client URL construction.

These tests verify that the WebSocket client correctly constructs WebSocket URLs
for both standard Home Assistant installations and Supervisor proxy environments.
"""


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
