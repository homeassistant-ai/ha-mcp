"""Settings-panel editor for the embedded server's OAuth callback allowlist (#2427)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.settings_ui import _handlers_oauth_callbacks as oc
from ha_mcp.settings_ui import build_settings_handlers

CALLBACK = "https://chatgpt.example/cb"
STATE = {
    "entry_id": "srv1",
    "allowlist": [CALLBACK],
    "default_allowlist": ["https://claude.ai/api/mcp/auth_callback"],
    "customized": True,
    "applies": True,
}


@pytest.fixture
def embedded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")


@pytest.fixture
def component(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record the component commands the handlers send; answer like the component."""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def command(server: Any, name: str, **fields: Any) -> dict[str, Any]:
        calls.append((name, fields))
        if fields.get("allowlist") == ["http://not-loopback/cb"]:
            return {**STATE, "saved": False, "invalid": fields["allowlist"]}
        return {**STATE, "saved": True, "invalid": []}

    monkeypatch.setattr(oc, "_component_command", command)
    return calls


def _request(body: Any) -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(return_value=body)
    return request


async def _call(name: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    handlers = build_settings_handlers(server=MagicMock())
    response = await handlers[name](_request(body))
    return response.status_code, json.loads(response.body)


async def test_other_install_types_do_not_offer_the_editor(component) -> None:
    status, body = await _call("get_oauth_callbacks")
    assert (status, body["available"]) == (200, False)
    assert component == []


async def test_the_editor_shows_the_list_in_force(embedded, component) -> None:
    _, body = await _call("get_oauth_callbacks")
    assert body["available"] is True
    assert body["allowlist"] == [CALLBACK]


async def test_a_component_without_the_commands_explains_why(
    embedded, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def too_old(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise oc._Unavailable("Update the HA-MCP integration in HACS.")

    monkeypatch.setattr(oc, "_component_command", too_old)
    _, body = await _call("get_oauth_callbacks")
    assert body["available"] is False
    assert "Update" in body["reason"]


async def test_a_save_sends_the_list_to_the_component(embedded, component) -> None:
    status, body = await _call("save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 200
    assert component == [(oc.WS_OAUTH_CALLBACKS_UPDATE, {"allowlist": [CALLBACK]})]


async def test_restoring_the_default_asks_the_component_to_reset(
    embedded, component
) -> None:
    await _call("save_oauth_callbacks", {"reset": True})
    assert component == [(oc.WS_OAUTH_CALLBACKS_UPDATE, {"reset": True})]


async def test_unusable_callbacks_are_named_back(embedded, component) -> None:
    status, body = await _call(
        "save_oauth_callbacks", {"allowlist": ["http://not-loopback/cb"]}
    )
    assert status == 400
    assert body["invalid"] == ["http://not-loopback/cb"]


@pytest.mark.parametrize(
    "body",
    [
        {"allowlist": "x"},
        {"allowlist": [1]},
        {"allowlist": ["x"] * 51},
        {"allowlist": ["https://example.com/" + "a" * 2048]},
        [],
    ],
)
async def test_a_malformed_save_is_refused_before_the_component(
    embedded, component, body: Any
) -> None:
    status, _ = await _call("save_oauth_callbacks", body)
    assert status == 400
    assert component == []


async def test_a_save_outside_the_embedded_server_is_refused(component) -> None:
    status, _ = await _call("save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 409
    assert component == []


async def test_a_refusal_by_the_component_is_a_400_not_a_502(
    embedded, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The component ran the command and rejected it: the panel was reached.
    from ha_mcp.client.rest_client import HomeAssistantCommandError

    async def refuse(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise HomeAssistantCommandError("Command failed: bad value")

    monkeypatch.setattr(oc, "_component_command", refuse)
    status, body = await _call("save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 400
    assert "bad value" in body["error"]["message"]


async def test_an_unreachable_component_is_a_502(
    embedded, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ha_mcp.client.rest_client import HomeAssistantConnectionError

    async def down(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise HomeAssistantConnectionError("WebSocket not connected")

    monkeypatch.setattr(oc, "_component_command", down)
    status, _ = await _call("save_oauth_callbacks", {"allowlist": [CALLBACK]})
    assert status == 502
