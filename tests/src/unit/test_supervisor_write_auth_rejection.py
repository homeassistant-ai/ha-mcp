"""A Supervisor write whose token is rejected on connect is an auth error.

Off-host, Supervisor calls go through Core's ``supervisor/api`` WebSocket
command. A write with no answer is reported as an unknown outcome, but a token
rejected while obtaining a client means the command was never sent, so it is
classified as the auth failure it is.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_addons import _supervisor_api_call


async def test_offhost_write_with_rejected_token_is_auth_not_unknown_outcome(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    client = MagicMock()
    rejected = HomeAssistantConnectionError("WebSocket authentication failed")
    rejected.__cause__ = HomeAssistantAuthError("WebSocket authentication failed")
    client.send_websocket_message = AsyncMock(side_effect=rejected)

    with pytest.raises(ToolError) as exc_info:
        await _supervisor_api_call(
            client, "/addons/core_mosquitto/restart", method="POST"
        )

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "AUTH_INVALID_TOKEN"
    assert payload.get("outcome") != "unknown"
