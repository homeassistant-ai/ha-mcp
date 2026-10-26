"""Restart outcomes through the real REST/WebSocket client conversion paths."""

import json
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandNotSent
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_system import SystemTools

type RestartClient = tuple[
    HomeAssistantClient, HomeAssistantWebSocketClient, list[dict[str, Any]]
]


@pytest.fixture
async def restart_client(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[RestartClient]:
    """Exercise WS directly, or one REST service attempt when parametrized."""
    monkeypatch.delenv("HA_MCP_EMBEDDED", raising=False)
    requests: list[str] = []
    effects: list[dict[str, Any]] = []
    response = getattr(request, "param", None)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/config/core/check_config":
            return httpx.Response(200, json={"result": "valid"})
        assert request.url.path == "/api/services/homeassistant/restart"
        assert response is not None
        effects.append(json.loads(request.content))
        status_code, body = response
        return httpx.Response(status_code, json=body)

    async with HomeAssistantClient(
        "http://restart.test",
        "test-token",
        verify_ssl=True,
        is_admin=response is not None,
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
        if response is None:
            # The config gate is covered separately; select WS without first
            # dispatching the service over REST, which must never replay a write.
            monkeypatch.setattr(
                client, "check_config", AsyncMock(return_value={"result": "valid"})
            )
        yield client, ws, effects
        assert requests == (
            [
                "/api/config/core/check_config",
                "/api/services/homeassistant/restart",
            ]
            if response is not None
            else []
        )


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
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A synthetic API 400 does not prove Core refused a dispatched restart."""
    client, ws, _ = restart_client
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
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core also uses HTTP 400 for ServiceValidationError raised inside a service."""
    client, ws, _ = restart_client
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
    restart_client: RestartClient,
) -> None:
    """Do not hide Core's actionable refusal behind the lost-acknowledgement message."""
    client, _, _ = restart_client
    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "outcome_unknown"
    assert "database upgrade" in result["error"]["details"]


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("unauthorized", "outcome_unknown"),
        ("unknown_command", "not_started"),
        ("unknown_error", "outcome_unknown"),
        ("home_assistant_error", "outcome_unknown"),
        ([], "outcome_unknown"),
    ],
)
async def test_ws_native_error_distinguishes_preflight_from_handler_failure(
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
    code: object,
    expected_status: str,
) -> None:
    """Core's structured rejection survives send_command and the REST adapter."""
    client, ws, _ = restart_client

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


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("invalid_format", "Stop callback validation failed after work began"),
        ("not_found", "Service homeassistant.restart not found."),
        ("home_assistant_error", "Unauthorized"),
    ],
)
async def test_native_service_error_after_effect_cannot_prove_restart_not_started(
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    message: str,
) -> None:
    """Core catches these exceptions around async_call, including stop-handler work."""
    client, ws, _ = restart_client
    effects = 0

    async def respond(command: dict[str, Any]) -> None:
        nonlocal effects
        effects += 1
        await ws._process_message(
            {
                "id": command["id"],
                "type": "result",
                "success": False,
                "error": {"code": code, "message": message},
            }
        )

    monkeypatch.setattr(ws, "send_json_message", AsyncMock(side_effect=respond))
    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "outcome_unknown"
    assert result["write_outcome_unknown"] is True
    assert result["error"]["details"]
    assert effects == 1


async def test_restart_success_payload_cannot_replace_its_protocol_envelope(
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Error-looking payload fields do not override Core's successful result frame."""
    client, ws, _ = restart_client
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
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized remote frame must not supply an authoritative refusal code."""
    client, ws, _ = restart_client

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


@pytest.mark.parametrize(
    "restart_client", [(401, {"message": "Unauthorized"})], indirect=True
)
async def test_rest_effect_then_401_stays_unknown_without_ws_replay(
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A potential WS pre-send refusal must not erase an earlier REST effect."""
    client, ws, effects = restart_client
    acquire = AsyncMock(side_effect=HomeAssistantCommandNotSent("WS unavailable"))
    monkeypatch.setattr(websocket_client, "get_websocket_client", acquire)
    send = AsyncMock()
    monkeypatch.setattr(ws, "send_json_message", send)

    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "outcome_unknown"
    assert result["write_outcome_unknown"] is True
    assert "401" in result["error"]["details"]
    assert effects == [{"safe_mode": False}]
    acquire.assert_not_awaited()
    send.assert_not_awaited()


async def test_direct_ws_not_sent_proves_restart_never_started(
    restart_client: RestartClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real WS readiness guard can prove no service frame was dispatched."""
    client, ws, effects = restart_client
    ws._state.authenticated = False
    send = AsyncMock()
    monkeypatch.setattr(ws, "send_json_message", send)

    with pytest.raises(ToolError) as caught:
        await SystemTools(client).ha_restart(confirm=True)

    result = json.loads(str(caught.value))
    assert result["execution_status"] == "not_started"
    assert not result.get("write_outcome_unknown")
    assert effects == []
    send.assert_not_awaited()
    assert not ws._state._pending_requests
