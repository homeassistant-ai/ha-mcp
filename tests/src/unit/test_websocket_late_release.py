"""Background subscription cleanup must collect failures on its original socket."""

import asyncio
import gc
import json
import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.websockets.exceptions import ConnectionClosed
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient


@pytest.mark.asyncio
@pytest.mark.parametrize("release_path", ["late_ack", "cleanup_deadline"])
@pytest.mark.parametrize("failure", ["disconnect", "send_closed", "timeout", "cancel"])
async def test_background_release_collects_outcome(
    release_path: str,
    failure: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost connection or timeout must not become an unhandled task error.

    Keep dispatch, send_command and the release real. Only the socket is fake;
    disconnect resets the actual pending unsubscribe future. Never await the
    background task or read its exception here: that would hide the bug.
    """
    client = HomeAssistantWebSocketClient("http://ha.local:8123", "test-token")
    client._state.mark_connected()
    client._state.mark_authenticated()
    sent = asyncio.Event()
    messages: list[dict[str, Any]] = []

    async def send(payload: str) -> None:
        messages.append(json.loads(payload))
        sent.set()
        if failure == "send_closed":
            raise ConnectionClosed(None, None)
        if failure == "disconnect":
            client._state.mark_disconnected("test connection dropped")
        if failure == "cancel":
            await asyncio.Event().wait()

    client.websocket = AsyncMock(send=send)
    real_send_command = client.send_command

    async def send_command(command: str, **kwargs: Any) -> dict[str, Any]:
        return await real_send_command(command, _wait_timeout=0.01, **kwargs)

    monkeypatch.setattr(client, "send_command", send_command)
    monkeypatch.setattr(
        "ha_mcp.client.websocket_client.CLEANUP_TIMEOUT_SECONDS", 0.01
    )
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    caplog.set_level(logging.DEBUG, logger="ha_mcp.client.websocket_client")
    try:
        if release_path == "late_ack":
            client._state.mark_abandoned_subscription(7)
            await client._process_message(
                {"id": 7, "type": "result", "success": True, "result": None}
            )
        else:
            client._ensure_send_lock()
            assert client._send_lock is not None
            async with client._send_lock:
                await client.release_subscription(7)

        assert len(client._late_releases) == 1
        task = next(iter(client._late_releases))
        completed = asyncio.Event()
        task.add_done_callback(lambda _task: completed.set())
        if failure == "cancel":
            await asyncio.wait_for(sent.wait(), timeout=1)
            task.cancel()
        await asyncio.wait_for(completed.wait(), timeout=1)
        del task
        gc.collect()

        assert len(messages) == 1
        assert messages[0]["type"] == "unsubscribe_events"
        assert messages[0]["subscription"] == 7
        assert not client._late_releases
        assert not client._state._pending_requests
        assert not unhandled, [context["message"] for context in unhandled]
        assert not [
            record for record in caplog.records if record.levelno >= logging.WARNING
        ]
    finally:
        loop.set_exception_handler(previous_handler)
