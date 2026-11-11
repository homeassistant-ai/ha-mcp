"""How an off-host Supervisor call reports a WebSocket that gave no answer.

Off-host, Supervisor calls go through Core's ``supervisor/api`` WebSocket
command. ``send_websocket_message`` raises ``HomeAssistantCommandNotSent`` when
the command provably never left: no attempt obtained a client (raised from the
acquisition error), or the socket was not ready to send. That error keeps its
own classification. Once a command may have left, a no-answer failure is a
``HomeAssistantConnectionError``, raised from the original when it was another
type (a command timeout, say). A write in that case has an unknown outcome.

Each case runs the server's own client against a real ``hass``: Core refuses a
revoked token itself, and a dropped or timed-out command is the pooled socket
giving no answer, which the client's real retry and classification turn into
the error the Supervisor call sees.
"""

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.errors import DEFAULT_SUGGESTIONS, ErrorCode
from ha_mcp.tools.tools_addons import _supervisor_api_call

_APP_SUPPORT = "Check that this installation supports apps (add-ons)."

Scenario = Callable[
    [HomeAssistant, HomeAssistantClient],
    AbstractAsyncContextManager[HomeAssistantClient],
]


def _revoke(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """Revoke the client's token, so Core refuses its next connection."""
    refresh_token = hass.auth.async_validate_access_token(client.token)
    assert refresh_token is not None
    hass.auth.async_remove_refresh_token(refresh_token)


async def _pooled(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


@asynccontextmanager
async def _rejected_token(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    """Core refuses the token on connect, before any command is sent."""
    _revoke(hass, client)
    yield client


@asynccontextmanager
async def _core_down(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    """Nothing listens where the client connects, as while Core restarts."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    down = HomeAssistantClient(
        base_url=f"http://127.0.0.1:{port}", token=client.token, verify_ssl=False
    )
    try:
        yield down
    finally:
        await down.close()


@asynccontextmanager
async def _socket_not_ready(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    pooled = await _pooled(client)
    with patch.object(
        pooled,
        "send_command",
        side_effect=HomeAssistantCommandNotSent("WebSocket not authenticated"),
    ):
        yield client


@asynccontextmanager
async def _rejected_after_a_sent_attempt(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    """A 403 retry after an attempt that got a client, whose reconnect Core
    refuses because the token was revoked in between."""
    pooled = await _pooled(client)
    with patch.object(
        pooled,
        "send_command",
        side_effect=HomeAssistantConnectionError("403 Forbidden"),
    ) as send:

        async def revoke_during_the_retry_delay() -> None:
            # The call first yields in the retry delay, after its first attempt.
            assert send.await_count == 1
            _revoke(hass, client)
            await websocket_manager.disconnect()

        revoke = asyncio.create_task(revoke_during_the_retry_delay())
        yield client
        await revoke
    assert send.await_count == 1


@asynccontextmanager
async def _timeout(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    pooled = await _pooled(client)
    with patch.object(
        pooled,
        "send_command",
        side_effect=HomeAssistantCommandTimeout("Response deadline exceeded"),
    ):
        yield client


@asynccontextmanager
async def _connection_drop(
    hass: HomeAssistant, client: HomeAssistantClient
) -> AsyncIterator[HomeAssistantClient]:
    pooled = await _pooled(client)
    with patch.object(
        pooled,
        "send_command",
        side_effect=HomeAssistantConnectionError("connection dropped"),
    ):
        yield client


async def _call(
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    client: HomeAssistantClient,
    scenario: Scenario,
    method: str = "POST",
) -> dict[str, Any]:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    endpoint = (
        "/addons/core_mosquitto/restart"
        if method == "POST"
        else "/addons/core_mosquitto/info"
    )
    async with scenario(hass, client) as failing:
        with pytest.raises(ToolError) as exc_info:
            await _supervisor_api_call(failing, endpoint, method=method)
    return json.loads(str(exc_info.value))


# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels, so the cases whose connect Core refuses expect a lingering timer.
@pytest.mark.parametrize(
    ("scenario", "method", "code", "outcome", "expected_lingering_timers"),
    [
        pytest.param(
            _rejected_token,
            "POST",
            "AUTH_INVALID_TOKEN",
            None,
            True,
            id="unsent-rejected",
        ),
        pytest.param(
            _core_down, "POST", "CONNECTION_FAILED", None, False, id="unsent-core-down"
        ),
        pytest.param(
            _socket_not_ready,
            "POST",
            "CONNECTION_FAILED",
            None,
            False,
            id="unsent-socket-not-ready",
        ),
        # A 403 retry after an attempt that got a client keeps a plain
        # connection error even when the retry's connect is rejected: the
        # first attempt may have sent.
        pytest.param(
            _rejected_after_a_sent_attempt,
            "POST",
            "CONNECTION_FAILED",
            "unknown",
            True,
            id="rejected-after-a-sent-attempt",
        ),
        pytest.param(
            _timeout,
            "POST",
            "TIMEOUT_OPERATION",
            "unknown",
            False,
            id="timeout",
        ),
        pytest.param(
            _connection_drop,
            "GET",
            "CONNECTION_FAILED",
            None,
            False,
            id="read-connection-drop",
        ),
    ],
)
async def test_unknown_outcome_only_when_a_write_may_have_been_sent(
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    scenario: Scenario,
    method: str,
    code: str,
    outcome: str | None,
) -> None:
    """A write that never left keeps its own error code and no unknown
    outcome, so the agent need not check durable state before retrying; a
    write that may have left says its outcome is unknown, and a read never
    does."""
    payload = await _call(monkeypatch, hass, core_client, scenario, method)

    assert payload["error"]["code"] == code
    assert payload.get("outcome") == outcome
    if outcome is not None:
        assert payload["method"] == method


# The refused socket's heartbeat timer, as in the table above.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_rejected_token_does_not_suggest_checking_app_support(
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    """A refused token says nothing about whether the installation has apps."""
    payload = await _call(monkeypatch, hass, core_client, _rejected_token)

    assert _APP_SUPPORT not in payload["error"]["suggestions"]


async def test_connection_failure_suggests_app_support_beside_its_own_advice(
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    payload = await _call(monkeypatch, hass, core_client, _core_down)

    suggestions = payload["error"]["suggestions"]
    assert _APP_SUPPORT in suggestions
    assert set(DEFAULT_SUGGESTIONS[ErrorCode.CONNECTION_FAILED]) <= set(suggestions)
