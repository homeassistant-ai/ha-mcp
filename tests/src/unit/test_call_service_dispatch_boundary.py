"""Registered component writes through the real WS parser and service consumer."""

import json
from collections.abc import Iterator
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools import component_api, tools_service

from .test_component_call_service_context import _registered
from .test_component_call_service_contract import _build_call_service
from .test_component_ws_phase2_async import _call_hass, _FakeBus, _FakeCallServices
from .test_component_ws_search import FakeState, _FakeConnection, _FakeWSApi, wsapi
from .test_ha_call_service_component_routing import RoutingClient


async def _send_component_reply(
    state: SimpleNamespace, hass: Any, handler: Any, message: dict[str, Any]
) -> None:
    frame: dict[str, Any] = {"id": message["id"], "type": "result"}
    if message["type"] == "ha_mcp_tools/info":
        frame.update(success=True, result=wsapi._do_info())
    else:
        assert message["type"] == wsapi.WS_CALL_SERVICE
        state.frames.append(message)
        if not state.registered:
            # The router has no handler to run: an authoritative refusal.
            frame.update(
                success=False,
                error={"code": "unknown_command", "message": "Unknown command."},
            )
        else:
            try:
                await handler(hass, state.connection, message)
            except RuntimeError as exc:
                # Simulated Core error encoding; no direct CommandError or
                # prebuilt bridge outcome bypasses _process_message/send_command.
                frame.update(
                    success=False,
                    error={"code": state.error_code, "message": str(exc)},
                )
            else:
                frame.update(
                    success=True, result=state.connection.results.pop(message["id"])
                )
            if state.lose_reply:
                raise ConnectionResetError("connection lost after the service effect")
    await state.ws._process_message(frame)


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """Simulate HA services and wire framing, retaining both current owners."""
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    # The existing fake drives synchronous tests with asyncio.run. Here the real
    # registered handler runs on the client's loop; HA error framing is below.
    monkeypatch.setattr(_FakeWSApi, "async_response", lambda self, fn: fn)
    handler = _registered(monkeypatch, wsapi.WS_CALL_SERVICE)
    connection = _FakeConnection(user_id="service-boundary-user")
    bus = _FakeBus()
    services = _FakeCallServices(known={("light", "turn_on")})
    hass = _call_hass([FakeState("light.a", state="off")], services, bus)
    client = RoutingClient()
    ws = HomeAssistantWebSocketClient("http://service.test", "test-token")
    ws._state.mark_connected()
    ws._state.mark_authenticated()
    state = SimpleNamespace(
        ws=ws,
        services=services,
        connection=connection,
        client=client,
        bus=bus,
        effects=[],
        frames=[],
        error_code=None,
        lose_reply=False,
        registered=True,
        confirm=True,
        call=_build_call_service(client),
    )

    def mutate() -> None:
        state.effects.append("component")
        if state.error_code is not None:
            raise RuntimeError(
                "403 Forbidden: handler failed after changing the device"
            )
        if state.confirm:
            new = FakeState("light.a", state="on")
            hass.states._by_id["light.a"] = new
            bus.fire("light.a", new)

    services._on_call = mutate
    legacy_call = client.call_service

    async def legacy(*args: Any, **kwargs: Any) -> Any:
        state.effects.append("legacy_rest")
        return await legacy_call(*args, **kwargs)

    monkeypatch.setattr(client, "call_service", legacy)
    monkeypatch.setattr(
        tools_service,
        "wait_for_state_change",
        AsyncMock(return_value={"entity_id": "light.a", "state": "on"}),
    )

    monkeypatch.setattr(
        ws,
        "send_json_message",
        AsyncMock(side_effect=partial(_send_component_reply, state, hass, handler)),
    )
    factory = AsyncMock(return_value=ws)
    monkeypatch.setattr(component_api, "get_websocket_client", factory)
    monkeypatch.setattr(tools_service, "get_websocket_client", factory)
    try:
        yield state
    finally:
        component_api._CAPS_CACHE.clear()
        component_api._CAPS_LOCKS.clear()


@pytest.mark.parametrize("error_code", ["unknown_error", "invalid_format"])
async def test_mutation_then_command_error_is_unknown_without_rest_replay(
    boundary: SimpleNamespace,
    error_code: str,
) -> None:
    boundary.error_code = error_code
    error: ToolError | None = None
    try:
        await boundary.call(domain="light", service="turn_on", entity_id="light.a")
    except ToolError as exc:
        error = exc

    assert boundary.effects == ["component"]
    assert boundary.client.call_service_calls == []
    assert len(boundary.frames) == 1
    assert boundary.bus.unsub_count == 1
    assert boundary.services.calls[0]["context"].user_id == "service-boundary-user"
    assert not boundary.ws._state._pending_requests
    assert error is not None, "An uncertain effect must fail the MCP call"
    result = json.loads(str(error))
    assert result["success"] is False
    assert result["write_outcome_unknown"] is True
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert result["ha_error_code"] == error_code
    assert "handler failed after changing the device" in result["error"]["details"]
    assert (result["domain"], result["service"], result["entity_id"]) == (
        "light",
        "turn_on",
        "light.a",
    )
    assert "before retrying" in result["error"]["suggestion"]
    assert "likely still running" not in str(error)


async def test_lost_reply_is_unknown_not_partial_success(
    boundary: SimpleNamespace,
) -> None:
    boundary.lose_reply = True
    with pytest.raises(ToolError) as caught:
        await boundary.call(domain="light", service="turn_on", entity_id="light.a")
    result = json.loads(str(caught.value))
    assert result["success"] is False
    assert result["write_outcome_unknown"] is True
    assert "connection lost after the service effect" in result["error"]["details"]
    assert result["ha_error_code"] is None
    assert boundary.effects == ["component"]
    assert boundary.client.call_service_calls == []


@pytest.mark.parametrize("refusal", ["unknown_command", "not_sent"])
async def test_proven_no_dispatch_falls_back_exactly_once(
    boundary: SimpleNamespace,
    refusal: str,
) -> None:
    await component_api.get_component_caps(boundary.client)
    if refusal == "unknown_command":
        boundary.registered = False
    else:
        boundary.ws._state.authenticated = False
    result = await boundary.call(domain="light", service="turn_on", entity_id="light.a")
    assert result["success"] is True
    assert boundary.effects == ["legacy_rest"]
    assert boundary.services.call_count == 0
    assert len(boundary.client.call_service_calls) == 1
    assert len(boundary.frames) == (1 if refusal == "unknown_command" else 0)
    if refusal == "unknown_command":
        assert not component_api._CAPS_CACHE


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "error_code": "invalid_format", "error": "service data"},
        False,
        0,
        {},
        [],
        None,
    ],
)
async def test_accepted_response_payload_is_preserved(
    boundary: SimpleNamespace,
    payload: Any,
) -> None:
    boundary.services._response = payload
    result = await boundary.call(
        domain="light",
        service="turn_on",
        entity_id="light.a",
        return_response=True,
    )
    assert result["success"] is True
    assert result["service_response"] == payload
    assert type(result["service_response"]) is type(payload)
    assert result["verified_state"] == "on"
    assert boundary.effects == ["component"]
    assert boundary.client.call_service_calls == []


async def test_accepted_write_without_confirmation_remains_partial(
    boundary: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boundary.confirm = False
    monkeypatch.setattr(wsapi, "_await_confirmation", AsyncMock())
    result = await boundary.call(domain="light", service="turn_on", entity_id="light.a")
    assert result["success"] is True
    assert result["partial"] is True
    assert "write_outcome_unknown" not in result
    assert boundary.effects == ["component"]
    assert boundary.client.call_service_calls == []
