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

Every connection here is the server's client talking to Core's own
``websocket_api``; Core holds a command open with ``execute_script`` waiting
on a state, so the close lands while the request is pending.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import weakref
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import MAX_WS_MESSAGE_BYTES
from ha_mcp.client.websocket_state import WebSocketConnectionState

from ._ws_transport import (
    HELD_SCRIPT,
    abort_transport,
    close_gate,
    connected_ws,
    open_gate,
)

WS_LOGGER = "ha_mcp.client.websocket_client"

# The Supervisor's Core-connection limit (MAX_MESSAGE_SIZE_FROM_CORE).
SUPERVISOR_CORE_MESSAGE_LIMIT = 64 * 1024 * 1024


# Core closes the socket; the client's close reply then re-arms aiohttp's
# heartbeat timer on that closed server socket, which nothing cancels (as in
# test_backup_auth_refusal.py).
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_core_closing_the_socket_fails_the_waiting_command_not_cancels_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_access_token: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A close mid-request raises a normal exception, not a cancellation.

    Pre-#1721-fix behaviour: ``reset_connection`` cancelled the pending
    future, ``send_command`` re-raised ``CancelledError``, and the MCP
    request was silently dropped. Core closes a connection whose token is
    revoked with a normal close, which raises no alarm: a routine teardown
    logs no WARNING.
    """
    caplog.set_level(logging.INFO, logger=WS_LOGGER)
    close_gate(hass)
    refresh_token = hass.auth.async_validate_access_token(hass_access_token)
    assert refresh_token is not None
    try:
        async with connected_ws(core_client) as client:
            command = asyncio.create_task(
                client.send_command("execute_script", sequence=HELD_SCRIPT)
            )
            # Core answers in order: once ``ping`` is back, the script is held.
            assert await client.ping() is True
            hass.auth.async_remove_refresh_token(refresh_token)

            # Bounded: the broken behaviour was an escaping CancelledError.
            async with asyncio.timeout(5):
                with pytest.raises(HomeAssistantConnectionError):
                    await command
    finally:
        await open_gate(hass)

    assert not [
        record
        for record in caplog.records
        if record.name == WS_LOGGER and record.levelno >= logging.WARNING
    ]


async def test_a_frame_over_the_size_cap_fails_the_command_with_close_1009(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The literal #1721 trigger: Core's reply is larger than the client accepts.

    The client fails the connection itself with close code 1009. The code and
    the reason must reach both the raised error and a WARNING, and the cap
    must reach ``websockets.connect``, or the oversized reply would be read.
    """
    caplog.set_level(logging.INFO, logger=WS_LOGGER)
    for index in range(40):
        hass.states.async_set(f"sensor.b11_big_{index}", "on", {"blob": "x" * 200})
    monkeypatch.setattr("ha_mcp.client.websocket_client.MAX_WS_MESSAGE_BYTES", 4 * 1024)

    async with connected_ws(core_client) as client:
        async with asyncio.timeout(5):
            with pytest.raises(HomeAssistantConnectionError) as excinfo:
                await client.send_command("get_states")

    assert "close code 1009" in str(excinfo.value)
    # Abnormal closes must stay visible at WARNING -- the #1721 pain was
    # a close that logged nothing actionable at default log levels.
    assert any(
        record.levelno == logging.WARNING
        and "close code 1009" in record.getMessage()
        and "frame exceeds limit" in record.getMessage()
        for record in caplog.records
    ), f"close code/reason not logged at WARNING; got: {caplog.records}"


async def test_a_dropped_connection_fails_the_waiting_command_and_warns(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Transport loss with no close frame still warns and fails the request."""
    caplog.set_level(logging.INFO, logger=WS_LOGGER)
    close_gate(hass)
    try:
        async with connected_ws(core_client) as client:
            command = asyncio.create_task(
                client.send_command("execute_script", sequence=HELD_SCRIPT)
            )
            assert await client.ping() is True
            abort_transport(client)

            async with asyncio.timeout(5):
                with pytest.raises(
                    HomeAssistantConnectionError, match="dropped without a close frame"
                ):
                    await command
    finally:
        await open_gate(hass)

    assert any(
        record.levelno == logging.WARNING
        and "dropped without a close frame" in record.getMessage()
        for record in caplog.records
    )


async def test_command_with_event_drop_retrieves_event_future_exception(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A drop during the result phase must not leak event_future's exception.

    reset_connection fails BOTH futures registered by send_command_with_event;
    only result_future is awaited on that path, so the client must retrieve
    event_future's exception itself or asyncio logs an ERROR-level "Future
    exception was never retrieved" when the future is garbage-collected. The
    test holds only a weak reference, so the future is collected here and its
    finaliser reports to the loop's exception handler.
    """
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    close_gate(hass)
    event_futures: list[weakref.ref[asyncio.Future[dict[str, Any]]]] = []

    async def drop_during_the_result_phase() -> None:
        # Its own frame, so nothing of the failed call outlives it here.
        async with connected_ws(core_client) as client:
            register = client.register_event_response

            def recording(message_id: int) -> asyncio.Future[dict[str, Any]]:
                future = register(message_id)
                event_futures.append(weakref.ref(future))
                return future

            with pytest.MonkeyPatch.context() as patcher:
                patcher.setattr(client, "register_event_response", recording)
                command = asyncio.create_task(
                    client.send_command_with_event(
                        "execute_script", sequence=HELD_SCRIPT
                    )
                )
                assert await client.ping() is True
                abort_transport(client)
                async with asyncio.timeout(5):
                    with pytest.raises(HomeAssistantConnectionError):
                        await command

    try:
        await drop_during_the_result_phase()
        # In debug mode the loop keeps the handle that resumed this task, and
        # through it the failed call; one more step lets it go.
        await asyncio.sleep(0)
        gc.collect()

        (event_future,) = event_futures
        assert event_future() is None, "the event future was never collected"
        assert not [
            context
            for context in reported
            if "never retrieved" in str(context.get("message"))
        ], "event_future's exception was never retrieved"
    finally:
        loop.set_exception_handler(previous_handler)
        await open_gate(hass)


async def test_reset_connection_fails_pending_futures_with_exception() -> None:
    """State-level contract: pending futures get an exception, not a cancel."""
    state = WebSocketConnectionState()
    request_future = state.register_pending_request(1)
    event_future = state.register_event_response(2)

    state.mark_disconnected("received close code 1009 (frame exceeds limit)")

    # reset_connection sets the exception synchronously, so the futures are
    # already done: result() re-raises the stored exception directly.
    assert not request_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError, match="1009"):
        request_future.result()
    assert not event_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError):
        event_future.result()


async def test_reset_connection_without_reason_still_raises_connection_error() -> None:
    """Explicit disconnects (no close frame details) must fail futures too."""
    state = WebSocketConnectionState()
    request_future = state.register_pending_request(1)

    state.mark_disconnected()

    assert not request_future.cancelled()
    with pytest.raises(HomeAssistantConnectionError):
        request_future.result()


def test_message_cap_is_not_below_the_supervisor_ceiling() -> None:
    """The cap must not undercut the Supervisor's Core-connection limit.

    On the app (add-on) path, frames above ``MAX_MESSAGE_SIZE_FROM_CORE`` die
    at the Supervisor proxy anyway, so a smaller client cap only
    re-introduces the #1721 failure mode ahead of the platform limit.
    Registry list responses scale with entity count and arrive as ONE frame
    (no pagination in the HA WebSocket API); a ~6.4k-entity instance
    overflowed the previous 20MB cap.
    """
    assert MAX_WS_MESSAGE_BYTES >= SUPERVISOR_CORE_MESSAGE_LIMIT
