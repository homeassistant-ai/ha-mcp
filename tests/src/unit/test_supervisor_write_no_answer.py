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

import json
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
from ha_mcp.tools.tools_addons import AddOnTools, get_addon_info

from ._core_client import refused_url

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
    down = HomeAssistantClient(
        base_url=refused_url(), token=client.token, verify_ssl=False
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


async def _restart(client: HomeAssistantClient) -> None:
    """Restart the app through ha_manage_app: a Supervisor POST."""
    await AddOnTools(client).manage_addon(
        slug="core_mosquitto",
        path=None,
        method="GET",
        body=None,
        debug=False,
        port=None,
        offset=0,
        limit=None,
        websocket=False,
        wait_for_close=True,
        message_limit=None,
        message_offset=0,
        summarize=True,
        python_transform=None,
        options=None,
        network=None,
        boot=None,
        auto_update=None,
        watchdog=None,
        array_patch=None,
        request_headers=None,
        action="restart",
    )


async def _call(
    monkeypatch: pytest.MonkeyPatch,
    hass: HomeAssistant,
    client: HomeAssistantClient,
    scenario: Scenario,
    method: str = "POST",
) -> dict[str, Any]:
    """The error a Supervisor write (restart) or read (app info) reports."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    async with scenario(hass, client) as failing:
        with pytest.raises(ToolError) as exc_info:
            if method == "POST":
                await _restart(failing)
            else:
                await get_addon_info(failing, "core_mosquitto")
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
