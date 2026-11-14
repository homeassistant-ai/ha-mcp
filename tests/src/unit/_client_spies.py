"""Call-through spies on the server's client: the test sees what was sent, Core still answers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient


@contextmanager
def sent_messages(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """Spy on the client's WebSocket bridge."""
    with patch.object(
        client, "send_websocket_message", wraps=client.send_websocket_message
    ) as spy:
        yield spy


@contextmanager
def sent_commands(socket: HomeAssistantWebSocketClient) -> Iterator[AsyncMock]:
    """Spy on the socket's commands; Core still answers each one."""
    with patch.object(socket, "send_command", wraps=socket.send_command) as spy:
        yield spy


@contextmanager
def routing_spies(
    client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
) -> Iterator[tuple[AsyncMock, AsyncMock]]:
    """Spies on the legacy bridge and the pooled component socket."""
    with (
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as legacy,
        patch.object(socket, "send_command", wraps=socket.send_command) as component,
    ):
        yield legacy, component
