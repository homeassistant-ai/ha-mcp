"""Core's token rejection stays distinct from an unreachable WebSocket."""

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import websocket_manager


async def test_auth_connect_failure_raises_auth_error(core_client: HomeAssistantClient):
    """A real Core token rejection must not turn into connection guidance."""
    with pytest.raises(HomeAssistantAuthError):
        await websocket_manager.get_client(
            url=core_client.base_url, token="invalid-token", verify_ssl=False
        )


async def test_non_auth_connect_failure_still_raises_connection_error(
    unreachable_client: HomeAssistantClient,
):
    """A refused connection remains a connection error, never an auth error."""
    with pytest.raises(HomeAssistantConnectionError):
        await websocket_manager.get_client(
            url=unreachable_client.base_url,
            token=unreachable_client.token,
            verify_ssl=False,
        )
