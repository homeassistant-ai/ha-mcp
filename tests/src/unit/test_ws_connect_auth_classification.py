"""Core's token rejection stays distinct from an unreachable WebSocket."""

from typing import Any

import pytest
from aiohttp import web

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import websocket_manager


async def test_auth_connect_failure_raises_auth_error(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real Core token rejection must not turn into connection guidance."""
    websocket_response = web.WebSocketResponse

    def without_heartbeat(**kwargs: Any) -> web.WebSocketResponse:
        # Heartbeats are irrelevant before authentication. Disable this real
        # transport's heartbeat: aiohttp can re-arm it on the CLOSE reply after
        # Core rejects authentication, leaving a timer after fixture teardown.
        return websocket_response(**(kwargs | {"heartbeat": None}))

    monkeypatch.setattr(web, "WebSocketResponse", without_heartbeat)
    with pytest.raises(HomeAssistantAuthError):
        await websocket_manager.get_client(
            url=core_client.base_url, token="invalid-token", verify_ssl=False
        )


async def test_non_auth_connect_failure_still_raises_connection_error(
    unreachable_client: HomeAssistantClient,
) -> None:
    """A refused connection remains a connection error, never an auth error."""
    with pytest.raises(HomeAssistantConnectionError):
        await websocket_manager.get_client(
            url=unreachable_client.base_url,
            token=unreachable_client.token,
            verify_ssl=False,
        )
