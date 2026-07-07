"""Regression tests for issue #1721 — WebSocket close while a request is pending.

When the WebSocket to Home Assistant dies while ``send_command`` awaits a
response, the pending future must fail with ``HomeAssistantConnectionError``,
NOT ``asyncio.CancelledError``. A cancellation is a ``BaseException``: it
escapes every ``except Exception`` handler in the tool layer and reaches the
MCP SDK, which interprets it as a client-initiated request cancellation,
suppresses the response entirely, and leaves the MCP client hanging until its
own timeout (4 minutes in Claude clients).

The close code/reason must also be logged and carried in the raised error:
the #1721 investigation stalled for days because the log showed only a bare
"WebSocket connection closed" with no hint that (for example) a frame had
exceeded ``max_size`` (close code 1009).
"""

import asyncio
import json
import logging

import pytest
import websockets

from ha_mcp.client.rest_client import HomeAssistantConnectionError
from ha_mcp.client.websocket_client import (
    MAX_WS_MESSAGE_BYTES,
    HomeAssistantWebSocketClient,
    WebSocketConnectionState,
)

HA_VERSION = "2026.7.0"


def _fake_ha_server(on_command):
    """Return a handler speaking the HA auth handshake, then delegating."""

    async def handler(connection):
        await connection.send(
            json.dumps({"type": "auth_required", "ha_version": HA_VERSION})
        )
        async for raw in connection:
            msg = json.loads(raw)
            if msg.get("type") == "auth":
                await connection.send(
                    json.dumps({"type": "auth_ok", "ha_version": HA_VERSION})
                )
            else:
                await on_command(connection, msg)

    return handler


async def _connect_client(port: int) -> HomeAssistantWebSocketClient:
    client = HomeAssistantWebSocketClient(
        f"http://127.0.0.1:{port}", "test-token", verify_ssl=False
    )
    assert await client.connect() is True
    return client


async def test_send_command_fails_fast_when_connection_closes():
    """A close mid-request must raise a normal exception, not a cancellation.

    Pre-#1721-fix behaviour: ``reset_connection`` cancelled the pending
    future, ``send_command`` re-raised ``CancelledError``, and the MCP
    request was silently dropped.
    """

    async def close_on_command(connection, msg):
        await connection.close(code=1011, reason="backend went away")

    server = await websockets.serve(_fake_ha_server(close_on_command), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = await _connect_client(port)
        # Bounded wait: the broken behaviour was an escaping CancelledError;
        # 5s is generous for a local socket close (default timeout is 30s).
        async with asyncio.timeout(5):
            with pytest.raises(HomeAssistantConnectionError):
                await client.send_command("config/entity_registry/list")
        await client.disconnect()
    finally:
        server.close()
        await server.wait_closed()


async def test_close_code_and_reason_surface_in_log_and_error(caplog):
    """The close code/reason must reach both the log and the raised error."""

    async def close_1009(connection, msg):
        await connection.close(code=1009, reason="frame exceeds limit")

    server = await websockets.serve(_fake_ha_server(close_1009), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    caplog.set_level(logging.INFO, logger="ha_mcp.client.websocket_client")
    try:
        client = await _connect_client(port)
        async with asyncio.timeout(5):
            with pytest.raises(HomeAssistantConnectionError) as excinfo:
                await client.send_command("config/entity_registry/list")
        assert "1009" in str(excinfo.value)
        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "1009" in message and "frame exceeds limit" in message
            for message in messages
        ), f"close code/reason not logged; got: {messages}"
        await client.disconnect()
    finally:
        server.close()
        await server.wait_closed()


async def test_reset_connection_fails_pending_futures_with_exception():
    """State-level contract: pending futures get an exception, not a cancel."""
    state = WebSocketConnectionState()
    request_future = state.register_pending_request(1)
    event_future = state.register_event_response(2)

    state.mark_disconnected("received close code 1009 (frame exceeds limit)")

    assert not request_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError, match="1009"):
        await request_future
    assert not event_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError):
        await event_future


async def test_reset_connection_without_reason_still_raises_connection_error():
    """Explicit disconnects (no close frame details) must fail futures too."""
    state = WebSocketConnectionState()
    request_future = state.register_pending_request(1)

    state.mark_disconnected()

    assert not request_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError):
        await request_future


def test_max_ws_message_bytes_matches_supervisor_ceiling():
    """The cap must stay at the Supervisor's Core-connection limit (64MB).

    On the add-on path frames above ``MAX_MESSAGE_SIZE_FROM_CORE`` die at the
    Supervisor proxy anyway, so a smaller client cap only re-introduces the
    #1721 failure mode ahead of the platform limit. Registry list responses
    scale with entity count and arrive as ONE frame (no pagination in the HA
    WebSocket API); a ~6.4k-entity instance overflowed the previous 20MB cap.
    """
    assert MAX_WS_MESSAGE_BYTES == 64 * 1024 * 1024
