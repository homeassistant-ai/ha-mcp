"""How an off-host Supervisor call reports a WebSocket that gave no answer.

Off-host, Supervisor calls go through Core's ``supervisor/api`` WebSocket
command. ``send_websocket_message`` raises ``HomeAssistantCommandNotSent`` when
the command provably never left: no attempt obtained a client (raised from the
acquisition error), or the socket was not ready to send. That error keeps its
own classification. Once a command may have left, a no-answer failure is a
``HomeAssistantConnectionError``, raised from the original when it was another
type (a command timeout, say). A write in that case has an unknown outcome.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.errors import DEFAULT_SUGGESTIONS, ErrorCode
from ha_mcp.tools.tools_addons import _supervisor_api_call

_APP_SUPPORT = "Check that this installation supports apps (add-ons)."


def _wrapped(
    cause: Exception,
    wrapper: type[HomeAssistantConnectionError] = HomeAssistantConnectionError,
) -> HomeAssistantConnectionError:
    error = wrapper(str(cause))
    error.__cause__ = cause
    return error


def _rejected_token() -> HomeAssistantConnectionError:
    return _wrapped(
        HomeAssistantAuthError("WebSocket authentication failed: Invalid token"),
        HomeAssistantCommandNotSent,
    )


def _core_down() -> HomeAssistantConnectionError:
    # What a restarting Core gives the WebSocket manager's connect.
    return _wrapped(
        HomeAssistantConnectionError(
            "Failed to connect to Home Assistant WebSocket: InvalidMessage: "
            "did not receive a valid HTTP response"
        ),
        HomeAssistantCommandNotSent,
    )


async def _call(
    monkeypatch: pytest.MonkeyPatch, error: Exception, method: str = "POST"
) -> dict:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=error)
    endpoint = (
        "/addons/core_mosquitto/restart"
        if method == "POST"
        else "/addons/core_mosquitto/info"
    )
    with pytest.raises(ToolError) as exc_info:
        await _supervisor_api_call(client, endpoint, method=method)
    return json.loads(str(exc_info.value))


@pytest.mark.parametrize(
    ("error", "method", "code", "outcome"),
    [
        pytest.param(
            _rejected_token(), "POST", "AUTH_INVALID_TOKEN", None, id="unsent-rejected"
        ),
        pytest.param(
            _core_down(), "POST", "CONNECTION_FAILED", None, id="unsent-core-down"
        ),
        pytest.param(
            HomeAssistantCommandNotSent("WebSocket not authenticated"),
            "POST",
            "CONNECTION_FAILED",
            None,
            id="unsent-socket-not-ready",
        ),
        # A 403 retry after an attempt that got a client keeps a plain
        # connection error even when the retry's connect is rejected: the
        # first attempt may have sent.
        pytest.param(
            _wrapped(HomeAssistantAuthError("WebSocket authentication failed")),
            "POST",
            "CONNECTION_FAILED",
            "unknown",
            id="rejected-after-a-sent-attempt",
        ),
        pytest.param(
            _wrapped(HomeAssistantCommandTimeout("Response deadline exceeded")),
            "POST",
            "TIMEOUT_OPERATION",
            "unknown",
            id="timeout",
        ),
        pytest.param(
            HomeAssistantConnectionError("connection dropped"),
            "GET",
            "CONNECTION_FAILED",
            None,
            id="read-connection-drop",
        ),
    ],
)
async def test_unknown_outcome_only_when_a_write_may_have_been_sent(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    method: str,
    code: str,
    outcome: str | None,
) -> None:
    """A write that never left keeps its own error code and no unknown
    outcome, so the agent need not check durable state before retrying; a
    write that may have left says its outcome is unknown, and a read never
    does."""
    payload = await _call(monkeypatch, error, method)

    assert payload["error"]["code"] == code
    assert payload.get("outcome") == outcome
    if outcome is not None:
        assert payload["method"] == method


async def test_rejected_token_does_not_suggest_checking_app_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused token says nothing about whether the installation has apps."""
    payload = await _call(monkeypatch, _rejected_token())

    assert _APP_SUPPORT not in payload["error"]["suggestions"]


async def test_connection_failure_suggests_app_support_beside_its_own_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = await _call(monkeypatch, _core_down())

    suggestions = payload["error"]["suggestions"]
    assert _APP_SUPPORT in suggestions
    assert set(DEFAULT_SUGGESTIONS[ErrorCode.CONNECTION_FAILED]) <= set(suggestions)
