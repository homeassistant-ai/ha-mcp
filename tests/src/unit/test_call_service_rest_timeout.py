"""A missing REST response is not evidence that a service succeeded."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_service
from ha_mcp.tools.tools_service import ServiceTools


@pytest.fixture
async def rest_call(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    state = SimpleNamespace(
        effects=[],
        requests=[],
        timeout=True,
        response=None,
        timeout_type=httpx.ReadTimeout,
        pre_dispatch_timeout=None,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        state.requests.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == "/api/states/light.test"
            return httpx.Response(200, json={"entity_id": "light.test", "state": "off"})
        assert (request.method, request.url.path) == (
            "POST",
            "/api/services/light/turn_on",
        )
        if state.pre_dispatch_timeout is not None:
            raise state.pre_dispatch_timeout("request not sent", request=request)
        state.effects.append(json.loads(request.content))
        if state.timeout:
            raise state.timeout_type(
                "reply lost after the simulated effect", request=request
            )
        return httpx.Response(200, json=state.response)

    async with HomeAssistantClient(
        "http://rest.test",
        "test-token",
        verify_ssl=True,
        is_admin=True,
    ) as client:
        await client.httpx_client.aclose()
        client.httpx_client = httpx.AsyncClient(
            base_url="http://rest.test/api",
            transport=httpx.MockTransport(handle),
        )
        state.ws_call = AsyncMock(side_effect=AssertionError("No WS fallback expected"))
        monkeypatch.setattr(client, "send_websocket_message", state.ws_call)
        monkeypatch.setattr(
            tools_service, "get_component_caps", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            tools_service, "wait_for_state_change", AsyncMock(return_value=None)
        )
        state.call = ServiceTools(client, MagicMock()).ha_call_service
        yield state


@pytest.mark.parametrize(
    "timeout_type,return_response",
    [
        (httpx.ReadTimeout, False),
        (httpx.ReadTimeout, True),
        (httpx.WriteTimeout, False),
    ],
)
async def test_effect_then_read_timeout_is_unknown_without_retry(
    rest_call: SimpleNamespace,
    timeout_type: type[httpx.TimeoutException],
    return_response: bool,
) -> None:
    rest_call.timeout_type = timeout_type
    with pytest.raises(ToolError) as caught:
        await rest_call.call(
            domain="light",
            service="turn_on",
            entity_id="light.test",
            data={"brightness": 45},
            wait=False,
            return_response=return_response,
        )
    result = json.loads(str(caught.value))
    assert result["success"] is False
    assert result["write_outcome_unknown"] is True
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "reply lost after the simulated effect" in result["error"]["details"]
    assert (result["domain"], result["service"], result["entity_id"]) == (
        "light",
        "turn_on",
        "light.test",
    )
    assert "before retrying" in result["error"]["suggestion"]
    assert "still running" not in str(caught.value)
    assert "service_response" not in result
    assert rest_call.effects == [{"brightness": 45, "entity_id": "light.test"}]
    assert rest_call.requests == [("POST", "/api/services/light/turn_on")]
    rest_call.ws_call.assert_not_awaited()


@pytest.mark.parametrize("timeout_type", [httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_pre_dispatch_timeout_is_a_connection_error(
    rest_call: SimpleNamespace,
    timeout_type: type[httpx.TimeoutException],
) -> None:
    rest_call.pre_dispatch_timeout = timeout_type
    with pytest.raises(ToolError) as caught:
        await rest_call.call(
            domain="light", service="turn_on", entity_id="light.test", wait=False
        )
    result = json.loads(str(caught.value))
    assert result["success"] is False
    assert result.get("write_outcome_unknown") is not True
    assert rest_call.effects == []
    assert rest_call.requests == [("POST", "/api/services/light/turn_on")]
    rest_call.ws_call.assert_not_awaited()


@pytest.mark.parametrize("payload", [False, 0, {}, [], None, {"success": False}])
async def test_received_rest_payload_is_preserved(
    rest_call: SimpleNamespace,
    payload: Any,
) -> None:
    rest_call.timeout = False
    rest_call.response = {"changed_states": [], "service_response": payload}
    result = await rest_call.call(
        domain="light",
        service="turn_on",
        entity_id="light.test",
        wait=False,
        return_response=True,
    )
    assert result["success"] is True
    assert result["service_response"] == payload
    assert type(result["service_response"]) is type(payload)
    assert "write_outcome_unknown" not in result
    assert len(rest_call.effects) == 1
    rest_call.ws_call.assert_not_awaited()


async def test_received_acceptance_with_unconfirmed_state_stays_successful(
    rest_call: SimpleNamespace,
) -> None:
    rest_call.timeout = False
    rest_call.response = []
    result = await rest_call.call(
        domain="light",
        service="turn_on",
        entity_id="light.test",
        wait=True,
    )
    assert result["success"] is True
    assert result["warnings"]
    assert "verified_state" not in result
    assert "write_outcome_unknown" not in result
    assert len(rest_call.effects) == 1
    assert rest_call.requests == [
        ("GET", "/api/states/light.test"),
        ("POST", "/api/services/light/turn_on"),
    ]
    rest_call.ws_call.assert_not_awaited()
