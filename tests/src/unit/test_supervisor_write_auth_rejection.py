"""How an off-host Supervisor call reports a WebSocket that gave no answer.

Off-host, Supervisor calls go through Core's ``supervisor/api`` WebSocket
command, and ``send_websocket_message`` wraps every no-answer failure in a
``HomeAssistantConnectionError`` raised from the original. A write with no
answer is reported as an unknown outcome, but a token rejected while obtaining
a client means the command was never sent, so it is classified as the auth
failure it is.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_addons import _supervisor_api_call


def _wrapped(cause: Exception) -> HomeAssistantConnectionError:
    error = HomeAssistantConnectionError(str(cause))
    error.__cause__ = cause
    return error


async def _call(
    monkeypatch: pytest.MonkeyPatch, error: Exception, method: str, endpoint: str
) -> dict:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=error)
    with pytest.raises(ToolError) as exc_info:
        await _supervisor_api_call(client, endpoint, method=method)
    return json.loads(str(exc_info.value))


async def test_offhost_write_with_rejected_token_is_auth_not_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = _wrapped(HomeAssistantAuthError("WebSocket authentication failed"))
    payload = await _call(
        monkeypatch, rejected, "POST", "/addons/core_mosquitto/restart"
    )
    assert payload["error"]["code"] == "AUTH_INVALID_TOKEN"
    assert "outcome" not in payload


async def test_offhost_write_timed_out_through_the_wrap_is_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timed_out = _wrapped(HomeAssistantCommandTimeout("Command timeout"))
    payload = await _call(
        monkeypatch, timed_out, "POST", "/addons/core_mosquitto/restart"
    )
    assert payload["error"]["code"] == "TIMEOUT_OPERATION"
    assert payload["outcome"] == "unknown"


async def test_offhost_read_with_no_answer_keeps_its_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dropped = HomeAssistantConnectionError("connection dropped")
    payload = await _call(monkeypatch, dropped, "GET", "/addons/core_mosquitto/info")
    assert payload["error"]["code"] == "CONNECTION_FAILED"
    assert "outcome" not in payload
