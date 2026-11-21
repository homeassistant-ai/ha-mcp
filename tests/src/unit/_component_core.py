"""Helpers for component tests that run on a real ``hass``."""

from __future__ import annotations

from typing import Any


async def ws_call(client: Any, command: str, /, **params: Any) -> dict[str, Any]:
    """Send one WebSocket command and return Home Assistant's reply message."""
    await client.send_json_auto_id({"type": command, **params})
    reply: dict[str, Any] = await client.receive_json()
    return reply


async def ws_result(client: Any, command: str, /, **params: Any) -> Any:
    """Send one WebSocket command and return its result; fail on an error reply."""
    reply = await ws_call(client, command, **params)
    assert reply["success"], reply
    return reply["result"]
