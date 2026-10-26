"""Restart outcomes through the real REST/WebSocket client conversion paths."""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantAuthError, HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_system import SystemTools


@pytest.fixture
async def restart_client(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[HomeAssistantClient, HomeAssistantWebSocketClient]]:
    """A valid config check followed by REST refusal selects the real WS path."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    requests: list[str] = []
    status_code, body = getattr(
        request, "param", (401, {"message": "Service call failed"})
    )

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/config/core/check_config":
            return httpx.Response(200, json={"result": "valid"})
        assert request.url.path == "/api/services/homeassistant/restart"
        return httpx.Response(status_code, json=body)

    async with HomeAssistantClient(
        "http://restart.test", "test-token", verify_ssl=True, is_admin=True
    ) as client:
        await client.httpx_client.aclose()
        client.httpx_client = httpx.AsyncClient(
            base_url="http://restart.test/api", transport=httpx.MockTransport(respond)
        )
        ws = HomeAssistantWebSocketClient(
            "http://restart.test", "test-token", verify_ssl=True
        )
        ws._state.connected = ws._state.authenticated = True
        monkeypatch.setattr(
            websocket_client, "get_websocket_client", AsyncMock(return_value=ws)
        )
        yield client, ws
        assert requests == [
            "/api/config/core/check_config",
            "/api/services/homeassistant/restart",
        ]


class _LocalErrorWithCode(RuntimeError):
    code = "invalid_format"


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("403 Forbidden after dispatch"),
        ValueError("Invalid reply"),
        RuntimeError("Command failed: Unauthorized"),
        _LocalErrorWithCode("403 Forbidden after dispatch"),
        _LocalErrorWithCode("Invalid reply"),
    ],
)
async def test_ws_conversion_cannot_turn_ambiguous_dispatch_into_a_refusal(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A synthetic API 400 does not prove Core refused a dispatched restart."""
    client, ws = restart_client
    send = AsyncMock(side_effect=error)
    monkeypatch.setattr(ws, "send_json_message", send)

    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "outcome_unknown"
    assert result["write_outcome_unknown"] is True
    send.assert_awaited_once()
    assert not ws._state._pending_requests


@pytest.mark.parametrize(
    "restart_client", [(400, {"message": "Service validation failed"})], indirect=True
)
async def test_rest_400_after_dispatch_does_not_prove_restart_never_started(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core also uses HTTP 400 for ServiceValidationError raised inside a service."""
    client, ws = restart_client
    send = AsyncMock()
    monkeypatch.setattr(ws, "send_json_message", send)

    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "outcome_unknown"
    assert result["write_outcome_unknown"] is True
    send.assert_not_awaited()
    assert not ws._state._pending_requests


@pytest.mark.parametrize(
    "restart_client",
    [
        (
            500,
            {
                "message": "The system cannot restart while a database upgrade is in progress."
            },
        )
    ],
    indirect=True,
)
async def test_rest_refusal_diagnostic_survives_conservative_unknown_classification(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
) -> None:
    """Do not hide Core's actionable refusal behind the lost-acknowledgement message."""
    client, _ = restart_client
    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "outcome_unknown"
    assert "database upgrade" in result["error"]["details"]


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("unauthorized", "not_started"),
        ("invalid_format", "not_started"),
        ("unknown_error", "outcome_unknown"),
        ("home_assistant_error", "outcome_unknown"),
        ([], "outcome_unknown"),
    ],
)
async def test_ws_native_error_distinguishes_preflight_from_handler_failure(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
    code: object,
    expected_status: str,
) -> None:
    """Core's structured rejection survives send_command and the REST adapter."""
    client, ws = restart_client

    async def respond(message: dict[str, Any]) -> None:
        await ws._process_message(
            {
                "id": message["id"],
                "type": "result",
                "success": False,
                "error": {"code": code, "message": "Core rejected the request"},
            }
        )

    send = AsyncMock(side_effect=respond)
    monkeypatch.setattr(ws, "send_json_message", send)
    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == expected_status
    assert bool(result.get("write_outcome_unknown")) == (
        expected_status == "outcome_unknown"
    )
    send.assert_awaited_once()


async def test_restart_success_payload_cannot_replace_its_protocol_envelope(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error-looking payload fields do not override Core's successful result frame."""
    client, ws = restart_client
    effects = 0

    async def respond(message: dict[str, Any]) -> None:
        nonlocal effects
        effects += 1
        await ws._process_message(
            {
                "id": message["id"],
                "type": "result",
                "success": True,
                "result": {"success": False, "error_code": "invalid_format"},
            }
        )

    monkeypatch.setattr(ws, "send_json_message", AsyncMock(side_effect=respond))

    result = await SystemTools(client).ha_restart(confirm=True)

    assert effects == 1
    assert result["success"] is True
    assert result["requested"] is True
    assert result.get("execution_status") != "not_started"


async def test_unexpected_ws_frame_cannot_assert_native_error_provenance(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized remote frame must not supply an authoritative refusal code."""
    client, ws = restart_client

    async def respond(message: dict[str, Any]) -> None:
        await ws._process_message(
            {
                "id": message["id"],
                "type": "unexpected_proxy_reply",
                "success": False,
                "error": "Acknowledgement missing",
                "error_code": "invalid_format",
            }
        )

    send = AsyncMock(side_effect=respond)
    monkeypatch.setattr(ws, "send_json_message", send)
    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    assert json.loads(str(caught.value))["execution_status"] == "outcome_unknown"
    send.assert_awaited_once()


async def test_ws_authentication_refusal_before_dispatch_is_not_started(
    restart_client: tuple[HomeAssistantClient, HomeAssistantWebSocketClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the pool's authentication refusal when no command could be sent."""
    client, ws = restart_client
    acquire = AsyncMock(side_effect=HomeAssistantAuthError("Invalid token"))
    monkeypatch.setattr(websocket_client, "get_websocket_client", acquire)
    send = AsyncMock()
    monkeypatch.setattr(ws, "send_json_message", send)

    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    assert json.loads(str(caught.value))["execution_status"] == "not_started"
    acquire.assert_awaited_once()
    send.assert_not_awaited()
