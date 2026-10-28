"""Credential and user-management WS commands stay out of the raw escape hatches.

``auth/long_lived_access_token`` answers with a new long-lived token in plain
text, ``auth/refresh_tokens`` lists the connected user's sessions, and
``config/auth/list`` enumerates every user. Through ``supervisor/api``,
Supervisor's ``/auth`` endpoints list users and reset passwords, and
``/os/ssh/authorized_keys`` adds a host SSH key. Both raw WebSocket surfaces
(ha_call_service's ``ws_command`` and the Code Mode ``ws_send`` bridge) refuse
them before dispatch, as a policy refusal rather than an input error, while the
``auth/current_user`` whoami read and other ``supervisor/api`` calls stay
available.
"""

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools.util_helpers import is_credential_ws_command

from .test_call_service_snapshot_guard import _error, _make_bridge
from .test_call_service_ws_command import _make_tools


def _supervisor(endpoint: str, method: str = "post") -> dict[str, Any]:
    return {"endpoint": endpoint, "method": method}


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("auth/long_lived_access_token", None),
        ("config/auth/list", None),
        ("config/auth_provider/homeassistant/admin_change_password", None),
        ("AUTH/LONG_LIVED_ACCESS_TOKEN", None),
        ("  auth/long_lived_access_token  ", None),
        # Reproduced against a live HAOS instance in the #2850 review.
        ("supervisor/api", _supervisor("/auth/reset")),
        ("supervisor/api", _supervisor("/auth/list", "get")),
        ("supervisor/api", _supervisor("/os/ssh/authorized_keys")),
        ("supervisor/api", _supervisor("/auth")),
        ("supervisor/api", _supervisor("/addons/../%61uth/list", "get")),
        # Supervisor's /v2 mount, reachable once supervisor_v2_api is switched on
        # (reproduced live in the second #2850 review).
        ("supervisor/api", _supervisor("/v2/auth/reset")),
        ("supervisor/api", _supervisor("/v2/auth/list", "get")),
        ("supervisor/api", _supervisor("/v2/os/ssh/authorized_keys")),
        ("supervisor/api", _supervisor("//V2/%61uth/reset")),
    ],
)
def test_token_minting_and_user_admin_commands_are_refused(
    command: str, params: dict[str, Any] | None
) -> None:
    assert is_credential_ws_command(command, params)


@pytest.mark.parametrize(
    ("command", "params"),
    [
        ("auth/current_user", None),
        ("config/area_registry/list", None),
        ("authority/list", None),
        ("supervisor/api", _supervisor("/core/info", "get")),
        ("supervisor/api", _supervisor("/authentication", "get")),
        ("supervisor/api", _supervisor("/v2/core/info", "get")),
        ("supervisor/api", _supervisor("/v2auth/list", "get")),
    ],
)
def test_whoami_and_unrelated_commands_stay_usable(
    command: str, params: dict[str, Any] | None
) -> None:
    assert not is_credential_ws_command(command, params)


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("auth/long_lived_access_token", None),
        ("config/auth/list", None),
        ("config/auth_provider/homeassistant/admin_change_password", None),
        ("supervisor/api", _supervisor("/auth/reset")),
        ("supervisor/api", _supervisor("/os/ssh/authorized_keys")),
        ("supervisor/api", _supervisor("/v2/auth/reset")),
        ("supervisor/api", _supervisor("/v2/os/ssh/authorized_keys")),
    ],
)
async def test_call_service_cannot_mint_tokens_or_manage_users(
    command: str, data: dict[str, Any] | None
) -> None:
    tools = _make_tools({"success": True, "result": "should-never-be-returned"})

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_call_service(ws_command=command, data=data)

    error = _error(excinfo)
    assert "refused by policy" in error["message"]
    suggestions = " ".join(error["suggestions"])
    assert "Home Assistant UI" in suggestions
    assert "Check the parameter values" not in suggestions
    tools._client.send_websocket_message.assert_not_awaited()


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("auth/current_user", None),
        ("supervisor/api", _supervisor("/core/info", "get")),
    ],
)
async def test_call_service_still_sends_whoami_and_other_supervisor_calls(
    command: str, data: dict[str, Any] | None
) -> None:
    tools = _make_tools({"success": True, "result": {"id": "u1"}})

    result = await tools.ha_call_service(ws_command=command, data=data)

    tools._client.send_websocket_message.assert_awaited_once_with(
        {"type": command, **(data or {})}
    )
    assert result["success"] is True


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            {"type": "auth/long_lived_access_token", "client_name": "x"},
            "refused by policy",
        ),
        ({"type": "config/auth/list"}, "refused by policy"),
        (
            {"type": "supervisor/api", **_supervisor("/auth/list", "get")},
            "refused by policy",
        ),
        (
            {"type": "supervisor/api", **_supervisor("/os/ssh/authorized_keys")},
            "refused by policy",
        ),
        (
            {"type": "supervisor/api", **_supervisor("/v2/os/ssh/authorized_keys")},
            "refused by policy",
        ),
        # The blocked-write check matches case-insensitively, like ws_command.
        ({"type": "LOVELACE/CONFIG/SAVE", "config": {}}, "blocked from the sandbox"),
    ],
)
async def test_sandbox_ws_send_cannot_mint_tokens_or_bypass_wrapping_tools(
    message: dict[str, Any], expected: str
) -> None:
    bridge = _make_bridge()

    result = await bridge.ws_send(message)

    assert expected in result.get("error", "")
    bridge.client.send_websocket_message.assert_not_awaited()


@pytest.mark.parametrize(
    "message",
    [
        {"type": "auth/current_user"},
        {"type": "supervisor/api", **_supervisor("/core/info", "get")},
    ],
)
async def test_sandbox_ws_send_still_sends_whoami_and_other_supervisor_calls(
    message: dict[str, Any],
) -> None:
    bridge = _make_bridge()

    await bridge.ws_send(message)

    bridge.client.send_websocket_message.assert_awaited_once_with(message)
