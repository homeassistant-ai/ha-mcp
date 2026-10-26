"""Preserve a service operation's outcome across the actual client transports."""

import asyncio
import json
from collections.abc import AsyncIterator
from functools import partial
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client import websocket_client
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.client.service_errors import (
    HomeAssistantCommandOutcomeUnknown,
    HomeAssistantServiceAuthOutcomeUnknown,
)
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.helpers import exception_to_structured_error
from ha_mcp.tools.tools_service import ServiceTools


async def _reply_on_wire(state: SimpleNamespace, message: dict[str, Any]) -> None:
    assert message["type"] == "call_service"
    state.frames.append(message)
    if state.mode != "router_refusal":
        state.effects.append(("ws", message["service_data"]["entity_id"]))
    frame: dict[str, Any] = {"id": message["id"], "type": "result"}
    if state.mode == "malformed":
        frame["type"] = "unexpected_reply"
    elif state.mode == "drop":
        raise ConnectionResetError("403 Forbidden: response lost after effect")
    elif state.mode in {"handler_error", "router_refusal"}:
        frame.update(
            success=False, error={"code": state.code, "message": "Core diagnostic"}
        )
    else:
        frame.update(
            success=True, result={"context": {"id": "test"}, "response": state.response}
        )
    await state.ws._process_message(frame)


@pytest.fixture
async def attempt(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    state = SimpleNamespace(
        effects=[],
        posts=[],
        frames=[],
        status=401,
        rest_mutates=True,
        mode="success",
        code=None,
        response=None,
    )

    async def rest(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == (
            "POST",
            "/api/services/counter/increment",
        )
        payload = json.loads(request.content)
        state.posts.append(payload)
        if state.rest_mutates:
            state.effects.append(("rest", payload["entity_id"]))
        await asyncio.sleep(0)
        if state.status == 401:
            return httpx.Response(401, text="401: Unauthorized")
        return httpx.Response(
            200,
            json={
                "changed_states": [{"entity_id": payload["entity_id"], "state": "1"}],
                "service_response": state.response,
            },
        )

    async with HomeAssistantClient(
        "http://attempt.test",
        "test-token",
        verify_ssl=True,
        is_admin=True,
    ) as client:
        await client.httpx_client.aclose()
        client.httpx_client = httpx.AsyncClient(
            base_url="http://attempt.test/api",
            transport=httpx.MockTransport(rest),
        )
        state.client = client
        state.call = ServiceTools(client, MagicMock()).ha_call_service
        state.ws = HomeAssistantWebSocketClient(
            "http://attempt.test", "test-token", verify_ssl=True
        )
        state.ws._state.mark_connected()
        state.ws._state.mark_authenticated()
        monkeypatch.setattr(
            state.ws,
            "send_json_message",
            AsyncMock(side_effect=partial(_reply_on_wire, state)),
        )
        state.acquire = AsyncMock(return_value=state.ws)
        monkeypatch.setattr(websocket_client, "get_websocket_client", state.acquire)
        monkeypatch.setattr(client, "_current_user", AsyncMock(return_value=None))
        yield state
        assert not state.ws._state._pending_requests


@pytest.mark.parametrize(
    "known_admin,tool", [(True, False), (True, True), (None, True)]
)
async def test_rest_401_after_effect_is_unknown_without_ws_replay(
    attempt: SimpleNamespace,
    known_admin: bool | None,
    tool: bool,
) -> None:
    attempt.client._is_admin = known_admin
    error: Exception | None = None
    try:
        if tool:
            await attempt.call(
                domain="counter",
                service="increment",
                entity_id="counter.test",
                wait=False,
            )
        else:
            await attempt.client.call_service(
                "counter", "increment", {"entity_id": "counter.test"}
            )
    except (ToolError, HomeAssistantAuthError) as exc:
        error = exc
    assert attempt.effects == [("rest", "counter.test")]
    assert len(attempt.posts) == 1
    assert attempt.frames == []
    attempt.acquire.assert_not_awaited()
    assert error is not None
    if tool:
        result = json.loads(str(error))
        assert result["success"] is False
        assert result["write_outcome_unknown"] is True
        assert result["http_status"] == 401
        assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    else:
        assert isinstance(error, HomeAssistantAuthError)
        assert isinstance(error, HomeAssistantServiceAuthOutcomeUnknown)
        assert not isinstance(error, HomeAssistantAPIError)
        assert error.status_code == 401
    assert "401" in str(error)
    assert attempt.client.admin_route_refused is True


async def test_refused_rest_call_latches_only_for_a_new_operation(
    attempt: SimpleNamespace,
) -> None:
    attempt.rest_mutates = False
    with pytest.raises(ToolError) as caught:
        await attempt.call(
            domain="counter", service="increment", entity_id="counter.first", wait=False
        )
    assert json.loads(str(caught.value))["write_outcome_unknown"] is True
    assert attempt.effects == []
    assert attempt.frames == []
    assert attempt.client.admin_route_refused is True

    result = await attempt.call(
        domain="counter", service="increment", entity_id="counter.next", wait=False
    )
    assert result["success"] is True
    assert attempt.effects == [("ws", "counter.next")]
    assert attempt.posts == [{"entity_id": "counter.first"}]


async def test_concurrent_unknown_admin_calls_do_not_replay_the_rest_attempt(
    attempt: SimpleNamespace,
) -> None:
    attempt.client._is_admin = None
    results = await asyncio.gather(
        *(
            attempt.client.call_service(
                "counter", "increment", {"entity_id": f"counter.op{i}"}
            )
            for i in range(4)
        ),
        return_exceptions=True,
    )
    assert len(attempt.posts) == 1
    rest_entity = attempt.posts[0]["entity_id"]
    assert attempt.effects.count(("rest", rest_entity)) == 1
    assert ("ws", rest_entity) not in attempt.effects
    assert len(attempt.frames) == 3
    assert sum(isinstance(result, HomeAssistantAuthError) for result in results) == 1
    assert sum(result == [] for result in results) == 3


@pytest.mark.parametrize("mode", ["malformed", "drop"])
async def test_native_ws_failure_after_effect_is_unknown(
    attempt: SimpleNamespace, mode: str
) -> None:
    attempt.client._is_admin = False
    attempt.mode = mode
    with pytest.raises(ToolError) as caught:
        await attempt.call(
            domain="counter", service="increment", entity_id="counter.test", wait=False
        )
    result = json.loads(str(caught.value))
    assert result["write_outcome_unknown"] is True
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert isinstance(caught.value.__context__, HomeAssistantCommandOutcomeUnknown)
    assert isinstance(caught.value.__context__, HomeAssistantConnectionError)
    assert attempt.effects == [("ws", "counter.test")]
    assert len(attempt.frames) == 1
    assert attempt.posts == []
    attempt.acquire.assert_awaited_once()


async def test_native_ws_acquisition_failure_stays_connection_error(
    attempt: SimpleNamespace,
) -> None:
    attempt.client._is_admin = False
    attempt.acquire.side_effect = HomeAssistantConnectionError(
        "refused before dispatch"
    )
    with pytest.raises(ToolError) as caught:
        await attempt.call(
            domain="counter", service="increment", entity_id="counter.test", wait=False
        )
    result = json.loads(str(caught.value))
    assert result["error"]["code"] == "CONNECTION_FAILED"
    assert not result.get("write_outcome_unknown")
    assert attempt.effects == attempt.frames == attempt.posts == []


async def test_direct_not_sent_retains_its_type_without_prior_rest(
    attempt: SimpleNamespace,
) -> None:
    attempt.client._is_admin = False
    attempt.ws._state.authenticated = False
    with pytest.raises(HomeAssistantCommandNotSent):
        await attempt.client.call_service(
            "counter", "increment", {"entity_id": "counter.test"}
        )
    assert attempt.effects == attempt.frames == attempt.posts == []


async def test_direct_router_refusal_retains_its_code_without_prior_rest(
    attempt: SimpleNamespace,
) -> None:
    attempt.client._is_admin = False
    attempt.mode = "router_refusal"
    attempt.code = "unknown_command"
    with pytest.raises(HomeAssistantAPIError) as caught:
        await attempt.client.call_service(
            "counter", "increment", {"entity_id": "counter.test"}
        )
    assert caught.value.code == "unknown_command"
    assert attempt.effects == attempt.posts == []
    assert len(attempt.frames) == 1


@pytest.mark.parametrize(
    "code", ["invalid_format", "not_found", "unauthorized", "home_assistant_error", []]
)
async def test_native_handler_error_does_not_prove_no_effect(
    attempt: SimpleNamespace, code: Any
) -> None:
    attempt.client._is_admin = False
    attempt.mode = "handler_error"
    attempt.code = code
    with pytest.raises(ToolError) as caught:
        await attempt.call(
            domain="counter", service="increment", entity_id="counter.test", wait=False
        )
    result = json.loads(str(caught.value))
    assert result["write_outcome_unknown"] is True
    assert result["ha_error_code"] == (code if isinstance(code, str) else None)
    assert "Core diagnostic" in result["error"]["details"]
    assert attempt.effects == [("ws", "counter.test")]
    assert attempt.posts == []
    assert len(attempt.frames) == 1


@pytest.mark.parametrize("transport", ["rest", "ws"])
@pytest.mark.parametrize("payload", [False, 0, {}, [], None, {"success": False}])
async def test_accepted_payload_and_rest_changed_states_survive(
    attempt: SimpleNamespace,
    transport: str,
    payload: Any,
) -> None:
    attempt.client._is_admin = transport == "rest"
    attempt.status = 200
    attempt.response = payload
    result = await attempt.call(
        domain="counter",
        service="increment",
        entity_id="counter.test",
        wait=False,
        return_response=True,
    )
    assert result["success"] is True
    assert result["service_response"] == payload
    assert type(result["service_response"]) is type(payload)
    assert attempt.effects == [(transport, "counter.test")]
    assert result["result"] == (
        [{"entity_id": "counter.test", "state": "1"}] if transport == "rest" else []
    )


@pytest.mark.parametrize("route", ["rest_401", "ws_auth", "ws_handler", "ws_drop"])
async def test_shared_auth_and_api_mapping_preserves_unknown_service_effect(
    attempt: SimpleNamespace,
    route: str,
) -> None:
    if route != "rest_401":
        attempt.client._is_admin = False
        attempt.mode = "drop" if route == "ws_drop" else "handler_error"
        attempt.code = "unauthorized" if route == "ws_auth" else "invalid_format"
    with pytest.raises(
        (HomeAssistantAuthError, HomeAssistantAPIError, HomeAssistantConnectionError)
    ) as captured:
        await attempt.client.call_service(
            "counter", "increment", {"entity_id": "counter.test"}
        )
    with pytest.raises(ToolError) as caught:
        exception_to_structured_error(
            captured.value,
            context={"entity_id": "counter.test"},
            suggestions=["Check the service arguments"],
        )
    result = json.loads(str(caught.value))
    assert result["write_outcome_unknown"] is True
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert len(attempt.effects) == 1
    assert "before retrying" in result["error"]["suggestion"]
    assert "Check the service arguments" in result["error"]["suggestions"]


async def test_generic_read_transport_failure_is_not_a_service_write(
    attempt: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        attempt.ws,
        "send_json_message",
        AsyncMock(side_effect=ConnectionResetError("read reply lost")),
    )
    with pytest.raises(HomeAssistantConnectionError) as captured:
        await attempt.client.send_websocket_message({"type": "get_states"})
    with pytest.raises(ToolError) as caught:
        exception_to_structured_error(captured.value)
    result = json.loads(str(caught.value))
    assert result["error"]["code"] == "CONNECTION_FAILED"
    assert not result.get("write_outcome_unknown")
    assert attempt.effects == []
