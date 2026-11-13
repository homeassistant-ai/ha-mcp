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

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_service import ServiceTools
from ha_mcp.tools.util_helpers import is_credential_ws_command

from ._client_spies import sent_messages
from ._code_mode import run_custom_tool
from ._supervisor_core import load_supervisor_api, supervisor_requests


def _supervisor(endpoint: str, method: str = "post") -> dict[str, Any]:
    return {"endpoint": endpoint, "method": method}


@pytest.fixture
async def supervisor_calls(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> AiohttpClientMocker:
    """Core answering ``auth/current_user``, and Core's own ``supervisor/api``
    in front of a Supervisor answering ``/core/info`` (``_supervisor_core``)."""
    assert await async_setup_component(hass, "auth", {})
    load_supervisor_api(
        hass,
        aioclient_mock,
        monkeypatch,
        {
            ("get", path): {"version": "2026.11.1"}
            for path in ("/core/info", "/v2/core/info")
        },
    )
    return aioclient_mock


@pytest.fixture
def sent(core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on the client's WebSocket send; Core still answers."""
    with sent_messages(core_client) as spy:
        yield spy


def _tools(client: HomeAssistantClient) -> ServiceTools:
    return ServiceTools(client, MagicMock())


async def _sandbox_ws_send(
    client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
) -> Any:
    """Send ``message`` with ``ws_send`` from custom-tool code; return its answer."""
    return await run_custom_tool(client, monkeypatch, f"await ws_send({message!r})")


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
    core_client: HomeAssistantClient,
    supervisor_calls: AiohttpClientMocker,
    sent: AsyncMock,
    command: str,
    data: dict[str, Any] | None,
) -> None:
    tools = _tools(core_client)

    with pytest.raises(ToolError) as excinfo:
        await tools.ha_call_service(ws_command=command, data=data)

    error = json.loads(str(excinfo.value))["error"]
    assert "refused by policy" in error["message"]
    suggestions = " ".join(error["suggestions"])
    assert "Home Assistant UI" in suggestions
    assert "Check the parameter values" not in suggestions
    sent.assert_not_awaited()
    assert supervisor_requests(supervisor_calls) == []


@pytest.mark.parametrize(
    ("command", "data"),
    [
        ("auth/current_user", None),
        ("supervisor/api", _supervisor("/core/info", "get")),
    ],
)
async def test_call_service_still_sends_whoami_and_other_supervisor_calls(
    core_client: HomeAssistantClient,
    supervisor_calls: AiohttpClientMocker,
    sent: AsyncMock,
    command: str,
    data: dict[str, Any] | None,
) -> None:
    tools = _tools(core_client)

    result = await tools.ha_call_service(ws_command=command, data=data)

    sent.assert_awaited_once_with({"type": command, **(data or {})})
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
    core_client: HomeAssistantClient,
    supervisor_calls: AiohttpClientMocker,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
    expected: str,
) -> None:
    result = await _sandbox_ws_send(core_client, monkeypatch, message)

    assert expected in result.get("error", "")
    sent.assert_not_awaited()
    assert supervisor_requests(supervisor_calls) == []


@pytest.mark.parametrize(
    "message",
    [
        {"type": "auth/current_user"},
        {"type": "supervisor/api", **_supervisor("/core/info", "get")},
    ],
)
async def test_sandbox_ws_send_still_sends_whoami_and_other_supervisor_calls(
    core_client: HomeAssistantClient,
    supervisor_calls: AiohttpClientMocker,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    message: dict[str, Any],
) -> None:
    await _sandbox_ws_send(core_client, monkeypatch, message)

    sent.assert_awaited_once_with(message)
