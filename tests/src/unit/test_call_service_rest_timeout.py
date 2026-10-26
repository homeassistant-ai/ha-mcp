"""REST service failures preserve uncertain effects without replay."""

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.service_errors import HomeAssistantServiceConnectionOutcomeUnknown
from ha_mcp.tools import tools_service
from ha_mcp.tools.helpers import exception_to_structured_error
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
        read_failure=False,
        response_status=200,
        read_status=200,
    )

    def handle(request: httpx.Request) -> httpx.Response:
        state.requests.append((request.method, request.url.path))
        if request.method == "GET":
            assert request.url.path == "/api/states/light.test"
            if state.read_failure:
                raise state.timeout_type("ordinary read failed", request=request)
            if state.read_status != 200:
                return httpx.Response(
                    state.read_status, json={"message": "read diagnostic"}
                )
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
        return httpx.Response(state.response_status, json=state.response)

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
        state.client = client
        yield state


@pytest.mark.parametrize(
    "timeout_type,return_response",
    [
        (httpx.ReadTimeout, False),
        (httpx.ReadTimeout, True),
        (httpx.WriteTimeout, False),
        (httpx.ReadError, False),
        (httpx.WriteError, False),
        (httpx.RemoteProtocolError, False),
    ],
)
async def test_effect_then_transport_failure_is_unknown_without_retry(
    rest_call: SimpleNamespace,
    timeout_type: type[httpx.TransportError],
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


@pytest.mark.parametrize(
    "timeout_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout]
)
async def test_pre_dispatch_timeout_is_a_connection_error(
    rest_call: SimpleNamespace,
    timeout_type: type[httpx.TransportError],
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


@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.ReadError])
async def test_rest_service_loss_reaches_shared_consumers_as_unknown(
    rest_call: SimpleNamespace,
    failure_type: type[httpx.TransportError],
) -> None:
    rest_call.timeout_type = failure_type
    with pytest.raises(HomeAssistantConnectionError) as captured:
        await rest_call.client.call_service(
            "light", "turn_on", {"entity_id": "light.test"}
        )
    result = exception_to_structured_error(
        captured.value,
        raise_error=False,
        context={"entity_id": "light.test"},
        suggestions=["Check the service arguments"],
    )
    assert rest_call.effects == [{"entity_id": "light.test"}]
    assert rest_call.requests == [("POST", "/api/services/light/turn_on")]
    rest_call.ws_call.assert_not_awaited()
    assert result["write_outcome_unknown"] is True
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "before retrying" in result["error"]["suggestion"]
    assert "reply lost after the simulated effect" in result["error"]["details"]
    assert isinstance(captured.value, HomeAssistantServiceConnectionOutcomeUnknown)
    assert isinstance(captured.value.__cause__, HomeAssistantConnectionError)
    assert isinstance(captured.value.__cause__.__cause__, failure_type)


@pytest.mark.parametrize("failure_type", [httpx.ReadTimeout, httpx.ReadError])
async def test_ordinary_rest_read_failure_remains_a_read_error(
    rest_call: SimpleNamespace,
    failure_type: type[httpx.TransportError],
) -> None:
    rest_call.timeout_type = failure_type
    rest_call.read_failure = True
    with pytest.raises(HomeAssistantConnectionError) as captured:
        await rest_call.client.get_entity_state("light.test")
    result = exception_to_structured_error(captured.value, raise_error=False)
    assert not result.get("write_outcome_unknown")
    assert not isinstance(captured.value, HomeAssistantServiceConnectionOutcomeUnknown)
    assert rest_call.effects == []
    rest_call.ws_call.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 500])
async def test_http_error_after_service_effect_preserves_unknown_and_diagnostics(
    rest_call: SimpleNamespace,
    status: int,
) -> None:
    rest_call.timeout = False
    rest_call.response_status = status
    rest_call.response = {
        "message": "Core handler rejected after effect",
        "write_outcome_unknown": False,
        "error_code": "unknown_command",
    }
    with pytest.raises(HomeAssistantAPIError) as captured:
        await rest_call.client.call_service(
            "light", "turn_on", {"entity_id": "light.test"}
        )
    assert not isinstance(captured.value, HomeAssistantConnectionError)
    assert captured.value.status_code == status
    assert captured.value.response_data == rest_call.response
    with pytest.raises(ToolError) as caught:
        exception_to_structured_error(
            captured.value, context={"entity_id": "light.test"}
        )
    result = json.loads(str(caught.value))
    assert result["write_outcome_unknown"] is True
    assert result["http_status"] == status
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert result["ha_error_code"] is None
    assert "Core handler rejected after effect" in result["error"]["details"]
    assert "before retrying" in result["error"]["suggestion"]
    assert rest_call.effects == [{"entity_id": "light.test"}]
    assert rest_call.requests == [("POST", "/api/services/light/turn_on")]
    rest_call.ws_call.assert_not_awaited()


@pytest.mark.parametrize("status", [400, 500])
async def test_ordinary_http_read_error_does_not_become_an_unknown_write(
    rest_call: SimpleNamespace,
    status: int,
) -> None:
    rest_call.read_status = status
    with pytest.raises(HomeAssistantAPIError) as captured:
        await rest_call.client.get_entity_state("light.test")
    result = exception_to_structured_error(captured.value, raise_error=False)
    assert not result.get("write_outcome_unknown")
    assert captured.value.status_code == status
    assert rest_call.effects == []
    rest_call.ws_call.assert_not_awaited()


@pytest.mark.parametrize(
    "body,diagnostic", [([], "[]"), (123, "123"), ({"message": 123}, "123")]
)
async def test_malformed_http_error_body_still_preserves_service_uncertainty(
    rest_call: SimpleNamespace,
    body: Any,
    diagnostic: str,
) -> None:
    rest_call.timeout = False
    rest_call.response_status = 500
    rest_call.response = body
    with pytest.raises(ToolError) as caught:
        await rest_call.call(
            domain="light", service="turn_on", entity_id="light.test", wait=False
        )
    result = json.loads(str(caught.value))
    assert rest_call.effects == [{"entity_id": "light.test"}]
    assert rest_call.requests == [("POST", "/api/services/light/turn_on")]
    rest_call.ws_call.assert_not_awaited()
    assert result["write_outcome_unknown"] is True
    assert result["http_status"] == 500
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert diagnostic in result["error"]["details"]
    assert "before retrying" in result["error"]["suggestion"]
    error = caught.value.__context__
    assert isinstance(error, HomeAssistantAPIError)
    assert error.status_code == 500
    assert isinstance(error.__cause__, HomeAssistantAPIError)
    if isinstance(body, dict):
        assert error.response_data == body
    else:
        assert error.response_data["response_body"] == body


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
