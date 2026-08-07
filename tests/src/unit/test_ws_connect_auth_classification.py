"""Auth-failure classification through the WebSocket connection manager.

``connect()`` catches its own ``HomeAssistantAuthError`` and returns False;
the manager used to collapse every failed connect into
``HomeAssistantConnectionError``, burying the auth cause inside the message
string. The manager now re-raises an auth failure AS an auth failure so
callers — e.g. the fail-closed registry validation of issue #2159 — classify
it as AUTH_* instead of CONNECTION_FAILED.
"""

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    WebSocketManager,
)


class FailingConnectClient:
    """Client stand-in whose connect() fails with a recorded exception."""

    def __init__(self, exc: Exception) -> None:
        self.is_connected = False
        self.last_connect_error = f"{type(exc).__name__}: {exc}"
        self.last_connect_exception = exc

    async def connect(self) -> bool:
        return False

    async def disconnect(self) -> None:
        return None


@pytest.fixture
def manager():
    """Yield the singleton manager with isolated, restored pool state."""
    mgr = WebSocketManager()
    saved = (
        dict(mgr._clients),
        dict(mgr._last_used),
        mgr._current_loop,
        mgr._lock,
        mgr._lock_loop,
        mgr._client_factory,
    )
    mgr._clients.clear()
    mgr._last_used.clear()
    mgr._current_loop = None
    mgr._lock = None
    mgr._lock_loop = None
    try:
        yield mgr
    finally:
        mgr._clients.clear()
        mgr._clients.update(saved[0])
        mgr._last_used.clear()
        mgr._last_used.update(saved[1])
        mgr._current_loop = saved[2]
        mgr._lock = saved[3]
        mgr._lock_loop = saved[4]
        mgr.configure(client_factory=saved[5] or HomeAssistantWebSocketClient)


async def test_auth_connect_failure_raises_auth_error(manager):
    """A token rejection during connect classifies as an auth failure."""
    manager.configure(
        client_factory=lambda url, token: FailingConnectClient(
            HomeAssistantAuthError("Authentication failed: Invalid token")
        )
    )

    with pytest.raises(HomeAssistantAuthError):
        await manager.get_client(url="http://ha.local", token="t")


async def test_non_auth_connect_failure_still_raises_connection_error(manager):
    """Every other connect miss keeps the collapsed connection error."""
    manager.configure(
        client_factory=lambda url, token: FailingConnectClient(OSError("no route"))
    )

    with pytest.raises(HomeAssistantConnectionError):
        await manager.get_client(url="http://ha.local", token="t")
