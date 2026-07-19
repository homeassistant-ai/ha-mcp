"""Unit tests for the ``connection_error`` marker on the WebSocket bridge.

``send_websocket_message`` catches every exception and returns a failure
envelope, which erases the distinction between "HA rejected the command" and
"the transport is dead". Callers that must honour the #1624 fail-loud policy
cannot recover that distinction from ``error_code`` (the connection classes
carry no ``code``) without matching on message text, so the bridge marks the
envelope structurally instead (issue #1947).

These tests pin the producing half of that contract; the consuming half lives
in ``test_tools_system.py::TestHomeAssistantConnectionErrorPropagation``.
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError

from ha_mcp.client.rest_client import (
    WS_CONNECTION_ERROR_KEY,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)


@pytest.fixture
def client() -> HomeAssistantClient:
    """``HomeAssistantClient`` with stubbed internals, no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
        c.base_url = "http://test.local:8123"
        c.token = "test-token"
        c.timeout = 30
        c.verify_ssl = True
        return c


async def _send_with_failure(
    client: HomeAssistantClient, exc: Exception
) -> dict[str, Any]:
    """Drive ``send_websocket_message`` with a ``send_command`` that raises."""
    ws_client = MagicMock()
    ws_client.send_command = AsyncMock(side_effect=exc)
    with patch(
        "ha_mcp.client.websocket_client.get_websocket_client",
        new=AsyncMock(return_value=ws_client),
    ):
        return await client.send_websocket_message({"type": "config_entries/get"})


async def _send_with_acquire_failure(
    client: HomeAssistantClient, exc: Exception
) -> dict[str, Any]:
    """Drive ``send_websocket_message`` with an unavailable pooled client."""
    with patch(
        "ha_mcp.client.websocket_client.get_websocket_client",
        new=AsyncMock(side_effect=exc),
    ):
        return await client.send_websocket_message({"type": "config_entries/get"})


class TestConnectionErrorMarker:
    """The marker is set for transport death and only for transport death.

    ``HomeAssistantCommandNotSent`` is marked deliberately: it subclasses the
    connection error and means the command provably never left the process, so
    a caller reading its result as authoritative would be wrong for the same
    reason. Everything HA actually received and rejected stays unmarked, which
    is what keeps callers that degrade on soft failures unaffected.
    """

    @pytest.mark.parametrize(
        ("exc", "expect_marked"),
        [
            (HomeAssistantConnectionError("ws gone"), True),
            (HomeAssistantCommandNotSent("WebSocket not authenticated"), True),
            (HomeAssistantCommandError("Unknown command."), False),
            (ValueError("boom"), False),
        ],
        ids=["connection_error", "command_not_sent", "command_error", "unexpected"],
    )
    @pytest.mark.asyncio
    async def test_marker_tracks_transport_death(
        self, client: HomeAssistantClient, exc: Exception, expect_marked: bool
    ) -> None:
        result = await _send_with_failure(client, exc)

        assert result["success"] is False
        assert str(exc) in result["error"]
        assert result.get(WS_CONNECTION_ERROR_KEY, False) is expect_marked

    @pytest.mark.asyncio
    async def test_403_blocked_transport_death_is_still_marked(
        self, client: HomeAssistantClient
    ) -> None:
        """The 403 branch returns its own envelope, and it matches on message
        text that a connection error can carry: ``_request`` wraps an httpx
        failure as ``HomeAssistantConnectionError("HTTP error: ...")``, and
        httpx spells a 403 as ``Client error '403 Forbidden' for url ...``.
        Without the marker on this path a dead transport would degrade
        silently, which is the bug this fix exists to prevent."""
        result = await _send_with_failure(
            client,
            HomeAssistantConnectionError(
                "HTTP error: Client error '403 Forbidden' for url 'http://ha/api'"
            ),
        )

        assert result["success"] is False
        assert "403 Forbidden" in result["error"]
        assert result[WS_CONNECTION_ERROR_KEY] is True

    @pytest.mark.parametrize(
        "exc",
        [
            Exception("Failed to connect to Home Assistant WebSocket"),
            HomeAssistantConnectionError("ws gone"),
        ],
        ids=["bare_exception", "connection_error"],
    )
    @pytest.mark.asyncio
    async def test_failure_to_acquire_a_client_is_marked(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        """Never obtaining a usable connection is transport death whatever the
        exception class. The pooled manager raises a bare ``Exception`` when
        ``connect()`` returns False, which is the ordinary "HA is unreachable"
        case, so this cannot be decided on type alone."""
        result = await _send_with_acquire_failure(client, exc)

        assert result["success"] is False
        assert result[WS_CONNECTION_ERROR_KEY] is True

    @pytest.mark.parametrize(
        "exc",
        [
            ConnectionClosedError(None, None),
            ConnectionResetError("peer reset"),
        ],
        ids=["connection_closed", "connection_reset"],
    )
    @pytest.mark.asyncio
    async def test_socket_write_failures_are_marked(
        self, client: HomeAssistantClient, exc: Exception
    ) -> None:
        """``send_command`` re-raises the original transport error from the
        send rather than wrapping it, to keep at-most-once semantics for write
        callers, so the bridge sees the library class untranslated. A socket
        that dies mid-request is still a dead transport."""
        result = await _send_with_failure(client, exc)

        assert result["success"] is False
        assert result[WS_CONNECTION_ERROR_KEY] is True

    def test_connection_classes_carry_no_error_code(self) -> None:
        """Pins the invariant the marker exists for: the connection classes
        expose no ``code``, so ``error_code`` is None for them and cannot be
        used to tell a dead transport from a command HA rejected. Adding a
        ``code`` to them would make the marker redundant, and should fail
        here rather than leave a stale rationale in the bridge."""
        assert not hasattr(HomeAssistantConnectionError("x"), "code")
        assert not hasattr(HomeAssistantCommandNotSent("x"), "code")
        assert hasattr(HomeAssistantCommandError("x"), "code")
