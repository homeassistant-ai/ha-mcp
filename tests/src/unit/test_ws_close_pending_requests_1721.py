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
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

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
        # Abnormal closes must stay visible at WARNING -- the #1721 pain was
        # a close that logged nothing actionable at default log levels.
        assert any(
            record.levelno == logging.WARNING
            and "1009" in record.getMessage()
            and "frame exceeds limit" in record.getMessage()
            for record in caplog.records
        ), f"close code/reason not logged at WARNING; got: {caplog.records}"
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

    # reset_connection sets the exception synchronously, so the futures are
    # already done: result() re-raises the stored exception directly, which is
    # the state contract under test (no event loop round-trip needed).
    assert not request_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError, match="1009"):
        request_future.result()
    assert not event_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError):
        event_future.result()


async def test_reset_connection_without_reason_still_raises_connection_error():
    """Explicit disconnects (no close frame details) must fail futures too."""
    state = WebSocketConnectionState()
    request_future = state.register_pending_request(1)

    state.mark_disconnected()

    assert not request_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError):
        request_future.result()


def test_max_ws_message_bytes_matches_supervisor_ceiling():
    """The cap must stay at the Supervisor's Core-connection limit (64MB).

    On the add-on path frames above ``MAX_MESSAGE_SIZE_FROM_CORE`` die at the
    Supervisor proxy anyway, so a smaller client cap only re-introduces the
    #1721 failure mode ahead of the platform limit. Registry list responses
    scale with entity count and arrive as ONE frame (no pagination in the HA
    WebSocket API); a ~6.4k-entity instance overflowed the previous 20MB cap.
    """
    assert MAX_WS_MESSAGE_BYTES == 64 * 1024 * 1024


async def test_connect_wires_max_size_into_websockets(monkeypatch):
    """MAX_WS_MESSAGE_BYTES must actually reach websockets.connect."""
    captured: dict = {}

    async def fake_connect(url, **kwargs):
        captured.update(kwargs)
        raise OSError("abort after capture")

    monkeypatch.setattr(websockets, "connect", fake_connect)
    client = HomeAssistantWebSocketClient(
        "http://127.0.0.1:1", "test-token", verify_ssl=False
    )
    assert await client.connect() is False
    assert captured["max_size"] == MAX_WS_MESSAGE_BYTES


class _ClosingFakeWebSocket:
    """Async-iterable stub whose iteration raises a given exception.

    Lets tests drive ``_message_handler`` through close paths a real
    loopback server cannot produce on demand -- most importantly the
    self-initiated close (``rcvd=None, sent=Close(1009)``), which is the
    literal #1721 trigger (the client fails the connection on an
    over-``max_size`` frame).
    """

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __aiter__(self) -> "_ClosingFakeWebSocket":
        return self

    async def __anext__(self) -> str:
        raise self._exc

    async def close(self) -> None:
        return None


async def _run_handler_with(
    exc: BaseException,
) -> tuple[HomeAssistantWebSocketClient, "asyncio.Future[dict]"]:
    """Drive _message_handler against a stub socket that dies with ``exc``."""
    client = HomeAssistantWebSocketClient(
        "http://127.0.0.1:1", "test-token", verify_ssl=False
    )
    client.websocket = _ClosingFakeWebSocket(exc)  # type: ignore[assignment]
    future = client._state.register_pending_request(1)
    await client._message_handler()
    return client, future


async def test_sent_close_1009_reaches_future_and_logs_warning(caplog):
    """Self-initiated close (over-max_size frame) surfaces 'sent close code 1009'."""
    caplog.set_level(logging.INFO, logger="ha_mcp.client.websocket_client")
    exc = ConnectionClosedError(
        rcvd=None,
        sent=Close(1009, "frame exceeds limit of 67108864 bytes"),
    )
    _, future = await _run_handler_with(exc)

    with pytest.raises(HomeAssistantConnectionError, match="sent close code 1009"):
        future.result()
    assert any(
        record.levelno == logging.WARNING
        and "sent close code 1009" in record.getMessage()
        for record in caplog.records
    )


async def test_clean_close_logs_info(caplog):
    """A normal close (1000) stays at INFO -- no alarm for routine teardown."""
    caplog.set_level(logging.INFO, logger="ha_mcp.client.websocket_client")
    exc = ConnectionClosedOK(
        rcvd=Close(1000, ""),
        sent=Close(1000, ""),
        rcvd_then_sent=True,
    )
    _, future = await _run_handler_with(exc)

    # Consume the future's exception: even a clean close fails pending
    # requests (and an unretrieved exception would pollute caplog with
    # asyncio's own ERROR record).
    assert isinstance(future.exception(), HomeAssistantConnectionError)
    close_records = [
        record
        for record in caplog.records
        if record.name == "ha_mcp.client.websocket_client"
        and "received close code 1000" in record.getMessage()
    ]
    assert close_records, f"clean close not logged; got: {caplog.records}"
    assert all(record.levelno == logging.INFO for record in close_records)


async def test_abrupt_drop_without_close_frame_logs_warning(caplog):
    """Transport loss with no close frame still warns and fails the future."""
    caplog.set_level(logging.INFO, logger="ha_mcp.client.websocket_client")
    exc = ConnectionClosedError(rcvd=None, sent=None)
    _, future = await _run_handler_with(exc)

    with pytest.raises(
        HomeAssistantConnectionError, match="dropped without a close frame"
    ):
        future.result()
    assert any(
        record.levelno == logging.WARNING
        and "dropped without a close frame" in record.getMessage()
        for record in caplog.records
    )


async def test_handler_generic_exception_fails_future_with_reason():
    """A non-ConnectionClosed handler error still fails futures with its text."""
    _, future = await _run_handler_with(RuntimeError("boom"))

    with pytest.raises(HomeAssistantConnectionError, match="boom"):
        future.result()


async def test_command_with_event_drop_retrieves_event_future_exception():
    """A drop during the result phase must not leak event_future's exception.

    reset_connection fails BOTH futures registered by send_command_with_event;
    only result_future is awaited on that path, so the client must retrieve
    event_future's exception itself or asyncio logs an ERROR-level "Future
    exception was never retrieved" when the future is garbage-collected.
    ``_log_traceback`` is asyncio's retrieved-flag: True after set_exception,
    flipped False only by result()/exception() — asserting it False proves the
    production code consumed the exception (GC-log-based detection is not
    deterministic because the raised exception's traceback keeps the future
    alive).
    """

    async def close_on_command(connection, msg):
        await connection.close(code=1011, reason="backend went away")

    server = await websockets.serve(_fake_ha_server(close_on_command), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        client = await _connect_client(port)
        captured: list[asyncio.Future] = []
        original = client.register_event_response

        def capturing(message_id: int) -> asyncio.Future:
            future = original(message_id)
            captured.append(future)
            return future

        client.register_event_response = capturing  # type: ignore[method-assign]
        async with asyncio.timeout(5):
            with pytest.raises(HomeAssistantConnectionError):
                await client.send_command_with_event("system_health/info")

        (event_future,) = captured
        assert event_future.done() and not event_future.cancelled()
        assert event_future._log_traceback is False, (
            "event_future's exception was never retrieved -- asyncio would "
            "log 'Future exception was never retrieved' at GC"
        )
        assert isinstance(event_future.exception(), HomeAssistantConnectionError)
        await client.disconnect()
    finally:
        server.close()
        await server.wait_closed()
