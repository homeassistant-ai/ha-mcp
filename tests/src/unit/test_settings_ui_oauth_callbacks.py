"""Settings-panel callback editing against the real component's WebSocket API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from starlette.requests import Request
from starlette.types import Message

from custom_components.ha_mcp_tools.const import OPT_OAUTH_REDIRECT_ALLOWLIST
from custom_components.ha_mcp_tools.const import DEFAULT_OAUTH_REDIRECT_ALLOWLIST
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.settings_ui import build_settings_handlers

from ._client_spies import sent_commands
from ._server_entry import add_server_entry

CALLBACK = "https://chatgpt.example/cb"


@pytest.fixture(autouse=True)
def embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    return add_server_entry(hass, options={OPT_OAUTH_REDIRECT_ALLOWLIST: [CALLBACK]})


async def _call(client: HomeAssistantClient, name: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    handlers = build_settings_handlers(server=MagicMock(client=client))
    async def receive() -> Message:
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}

    request = Request({"type": "http"}, receive=receive)
    response = await handlers[name](request)
    return response.status_code, json.loads(response.body)


@pytest.fixture
async def component_commands(
    core_client: HomeAssistantClient,
) -> AsyncIterator[AsyncMock]:
    """Observe commands while keeping every reply on the real Core transport."""
    ws = await get_websocket_client(
        url=core_client.base_url, token=core_client.token, verify_ssl=core_client.verify_ssl
    )
    with sent_commands(ws) as commands:
        yield commands


async def test_other_install_types_do_not_offer_the_editor(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch, component_commands: AsyncMock
) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    status, body = await _call(core_client, "get_oauth_callbacks")
    assert (status, body["available"]) == (200, False)
    component_commands.assert_not_awaited()


async def test_the_editor_shows_the_list_in_force(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    _, body = await _call(component_core_client, "get_oauth_callbacks")
    assert body["available"] is True
    assert body["allowlist"] == [CALLBACK]


async def test_a_component_without_the_commands_explains_why(
    core_client: HomeAssistantClient,
) -> None:
    _, body = await _call(core_client, "get_oauth_callbacks")
    assert body["available"] is False
    assert "Update" in body["reason"]


async def test_a_save_sends_the_list_to_the_component(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    callback = "https://another.example/cb"
    status, body = await _call(component_core_client, "save_oauth_callbacks", {"allowlist": [callback]})
    assert status == 200
    assert body["saved"] is True
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [callback]


async def test_restoring_the_default_asks_the_component_to_reset(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    status, body = await _call(component_core_client, "save_oauth_callbacks", {"reset": True})
    assert status == 200
    assert body["allowlist"] == list(DEFAULT_OAUTH_REDIRECT_ALLOWLIST)
    assert OPT_OAUTH_REDIRECT_ALLOWLIST not in entry.options


async def test_unusable_callbacks_are_named_back(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    status, body = await _call(component_core_client, "save_oauth_callbacks", {"allowlist": ["http://not-loopback/cb"]})
    assert status == 400
    assert body["invalid"] == ["http://not-loopback/cb"]
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [CALLBACK]


@pytest.mark.parametrize("body", [{"allowlist": "x"}, {"allowlist": [1]}, [], {"reset": True, "allowlist": [CALLBACK]}])
async def test_a_malformed_save_is_refused_before_the_component(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry, body: Any, component_commands: AsyncMock
) -> None:
    status, _ = await _call(component_core_client, "save_oauth_callbacks", body)
    assert status == 400
    component_commands.assert_not_awaited()
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [CALLBACK]


async def test_the_list_is_bounded_by_the_component_not_the_panel(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry
) -> None:
    lines = [f"https://ex{i}.example/cb" for i in range(50)]
    status, _ = await _call(component_core_client, "save_oauth_callbacks", {"allowlist": [*lines, lines[0]]})
    assert status == 200
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == lines


@pytest.mark.parametrize("operation,status", [("get_oauth_callbacks", 200), ("save_oauth_callbacks", 502)])
async def test_a_failed_probe_is_a_reachability_error(
    unreachable_client: HomeAssistantClient, operation: str, status: int
) -> None:
    actual, body = await _call(unreachable_client, operation, {"allowlist": [CALLBACK]})
    assert actual == status
    if operation == "get_oauth_callbacks":
        assert body["available"] is False
    message = body["reason"] if operation == "get_oauth_callbacks" else body["error"]["message"]
    assert "Could not reach" in message


async def test_a_save_outside_the_embedded_server_is_refused(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch, component_commands: AsyncMock
) -> None:
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    status, _ = await _call(component_core_client, "save_oauth_callbacks", {"allowlist": []})
    assert status == 409
    component_commands.assert_not_awaited()
    assert entry.options[OPT_OAUTH_REDIRECT_ALLOWLIST] == [CALLBACK]


async def test_a_refusal_by_the_component_is_a_400_not_a_502(
    component_core_client: HomeAssistantClient,
) -> None:
    status, body = await _call(component_core_client, "save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 400
    assert "no ha_mcp_tools in-process server" in body["error"]["message"]


async def test_an_unreachable_component_is_a_502(
    component_core_client: HomeAssistantClient, entry: MockConfigEntry, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantConnectionError
    from ha_mcp.tools.component_api import get_component_caps

    await get_component_caps(component_core_client, strict=True)
    ws = await get_websocket_client(url=component_core_client.base_url, token=component_core_client.token, verify_ssl=False)
    monkeypatch.setattr(ws, "send_command", AsyncMock(side_effect=HomeAssistantConnectionError("WebSocket not connected")))
    status, _ = await _call(component_core_client, "save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 502
