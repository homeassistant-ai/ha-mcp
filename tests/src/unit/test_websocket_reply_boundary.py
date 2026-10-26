"""Malformed acknowledgements must leave dispatched writes uncertain, not replayed."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient


@pytest.fixture
def ready_socket() -> HomeAssistantWebSocketClient:
    ws = HomeAssistantWebSocketClient(
        "http://reply.test", "test-token", verify_ssl=True
    )
    ws._state.mark_connected()
    ws._state.mark_authenticated()
    return ws


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            {
                "type": "unexpected_proxy_reply",
                "success": False,
                "error": "403 Forbidden: acknowledgement missing",
                "error_code": "invalid_format",
            },
            id="unexpected-frame-with-refusal-code",
        ),
        pytest.param({"type": "pong"}, id="pong-for-write"),
        pytest.param({"type": "result", "result": None}, id="missing-success"),
        pytest.param({"type": "result", "success": 0}, id="non-boolean-success"),
    ],
)
async def test_malformed_reply_after_effect_is_unknown_without_replay(
    ready_socket: HomeAssistantWebSocketClient,
    monkeypatch: pytest.MonkeyPatch,
    reply: dict[str, Any],
) -> None:
    """Remote fields cannot impersonate a parsed Core verdict after a write landed."""
    effects: list[dict[str, Any]] = []

    async def mutate_and_reply(message: dict[str, Any]) -> None:
        effects.append(message["service_data"])
        await ready_socket._process_message({"id": message["id"], **reply})

    send = AsyncMock(side_effect=mutate_and_reply)
    acquire = AsyncMock(return_value=ready_socket)
    sleep = AsyncMock()
    monkeypatch.setattr(ready_socket, "send_json_message", send)
    monkeypatch.setattr(websocket_client, "get_websocket_client", acquire)
    monkeypatch.setattr("ha_mcp.client.rest_client.asyncio.sleep", sleep)
    async with HomeAssistantClient(
        "http://reply.test", "test-token", verify_ssl=True, is_admin=True
    ) as client:
        with pytest.raises(HomeAssistantConnectionError) as caught:
            await client.send_websocket_message(
                {
                    "type": "call_service",
                    "domain": "counter",
                    "service": "increment",
                    "service_data": {"entity_id": "counter.test"},
                }
            )

    assert not isinstance(caught.value, HomeAssistantCommandNotSent)
    assert effects == [{"entity_id": "counter.test"}]
    send.assert_awaited_once()
    acquire.assert_awaited_once()
    sleep.assert_not_awaited()
    assert not ready_socket._state._pending_requests


async def test_pong_still_acknowledges_ping(
    ready_socket: HomeAssistantWebSocketClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rejecting a pong for a write must not break the connection health check."""

    async def pong(message: dict[str, Any]) -> None:
        assert message["type"] == "ping"
        await ready_socket._process_message({"id": message["id"], "type": "pong"})

    send = AsyncMock(side_effect=pong)
    monkeypatch.setattr(ready_socket, "send_json_message", send)
    assert await ready_socket.ping() is True
    send.assert_awaited_once()
    assert not ready_socket._state._pending_requests
