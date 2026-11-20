"""The WebSocket address the client derives from the configured Home Assistant URL."""

from __future__ import annotations

import pytest

from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient


@pytest.mark.parametrize(
    ("url", "ws_url"),
    [
        (
            "http://homeassistant.local:8123",
            "ws://homeassistant.local:8123/api/websocket",
        ),
        (
            "https://homeassistant.local:8123",
            "wss://homeassistant.local:8123/api/websocket",
        ),
        (
            "http://homeassistant.local:8123/",
            "ws://homeassistant.local:8123/api/websocket",
        ),
        # The add-on's Supervisor proxy serves Core's socket at
        # /core/websocket, not /api/websocket (#186, #189).
        ("http://supervisor/core", "ws://supervisor/core/websocket"),
        ("http://supervisor/core/", "ws://supervisor/core/websocket"),
    ],
)
def test_the_socket_address_follows_the_configured_url(url: str, ws_url: str) -> None:
    """A wrong address leaves every WebSocket tool unable to connect."""
    client = HomeAssistantWebSocketClient(url=url, token="test-token", verify_ssl=True)

    assert client.ws_url == ws_url


def test_a_trailing_slash_is_dropped_from_the_base_url() -> None:
    """Paths built on the base URL would otherwise carry a double slash."""
    client = HomeAssistantWebSocketClient(
        url="http://homeassistant.local:8123/", token="test-token", verify_ssl=True
    )

    assert client.base_url == "http://homeassistant.local:8123"
