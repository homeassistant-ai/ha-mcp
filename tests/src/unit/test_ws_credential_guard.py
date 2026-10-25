"""Credential and user-management WS commands stay out of the raw escape hatches.

``auth/long_lived_access_token`` answers with a new long-lived token in
plain text, and ``auth/refresh_tokens`` / ``config/auth/list`` enumerate every
session and user. Both raw WebSocket surfaces (ha_call_service's ``ws_command``
and the Code Mode ``ws_send`` bridge) refuse them before dispatch, while the
``auth/current_user`` whoami read stays available.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.tools_code import _SandboxBridge
from ha_mcp.tools.util_helpers import is_credential_ws_command

from .test_call_service_ws_command import _make_tools

CREDENTIAL_COMMANDS = [
    "auth/long_lived_access_token",
    "auth/refresh_tokens",
    "auth/delete_refresh_token",
    "auth/delete_all_refresh_tokens",
    "auth/refresh_token_set_expiry",
    "auth/sign_path",
    "config/auth/list",
    "config/auth/create",
    "config/auth/delete",
    "config/auth/update",
    "config/auth_provider/homeassistant/create",
    "config/auth_provider/homeassistant/admin_change_password",
    "AUTH/LONG_LIVED_ACCESS_TOKEN",
    "  auth/long_lived_access_token  ",
]


@pytest.mark.parametrize("command", CREDENTIAL_COMMANDS)
def test_credential_commands_are_classified(command: str) -> None:
    assert is_credential_ws_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "auth/current_user",
        "AUTH/CURRENT_USER",
        "config/area_registry/list",
        "config_entries/get",
        "supervisor/api",
        "authority/list",
    ],
)
def test_other_commands_are_not_classified(command: str) -> None:
    assert not is_credential_ws_command(command)


@pytest.mark.parametrize("command", CREDENTIAL_COMMANDS)
async def test_call_service_rejects_credential_command(command: str) -> None:
    tools = _make_tools({"success": True, "result": "should-never-be-returned"})

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_call_service(ws_command=command)

    assert "credentials or manages users" in str(excinfo.value)
    tools._client.send_websocket_message.assert_not_awaited()


async def test_call_service_still_allows_current_user() -> None:
    tools = _make_tools({"success": True, "result": {"id": "u1", "is_admin": True}})

    result = await tools.ha_call_service(ws_command="auth/current_user")

    tools._client.send_websocket_message.assert_awaited_once_with(
        {"type": "auth/current_user"}
    )
    assert result["success"] is True


def _bridge() -> tuple[_SandboxBridge, MagicMock]:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": "should-never-be-returned"}
    )
    return (
        _SandboxBridge(
            MagicMock(), client, SimpleNamespace(code_mode_max_invocations=10)
        ),
        client,
    )


@pytest.mark.parametrize(
    "command",
    ["auth/long_lived_access_token", "config/auth/list", "AUTH/REFRESH_TOKENS"],
)
async def test_sandbox_ws_send_rejects_credential_command(command: str) -> None:
    bridge, client = _bridge()

    result = await bridge.ws_send(
        {"type": command, "client_name": "x", "lifespan": 3650}
    )

    assert "credentials" in result.get("error", "")
    client.send_websocket_message.assert_not_awaited()


async def test_sandbox_ws_send_still_allows_current_user() -> None:
    bridge, client = _bridge()

    await bridge.ws_send({"type": "auth/current_user"})

    client.send_websocket_message.assert_awaited_once()
