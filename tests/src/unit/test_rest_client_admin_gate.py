"""Unit tests for the REST client's non-admin guard (#2546).

Home Assistant answers a non-admin user's request to an admin-only REST route
with 401, and ``http.ban`` counts it as a failed login. The client asks
``auth/current_user`` once per token, refuses admin-only routes locally for a
non-admin answer, and calls services over WebSocket for a non-admin token.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.admin_routes import is_admin_only_route
from ha_mcp.client.rest_client import (
    NON_ADMIN_TOKEN_WARNING,
    HomeAssistantAdminRequiredError,
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandError,
)
from ha_mcp.errors import ErrorCode
from ha_mcp.tools.helpers import exception_to_structured_error


def _current_user(is_admin):
    return {"id": "u1", "is_admin": is_admin}


def _response(status_code, json_body=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.reason_phrase = "Unauthorized" if status_code == 401 else "OK"
    resp.json = MagicMock(return_value=json_body if json_body is not None else {})
    return resp


@pytest.fixture
def client():
    """``HomeAssistantClient`` with stubbed internals — no real network."""
    with patch.object(HomeAssistantClient, "__init__", lambda self, **kwargs: None):
        c = HomeAssistantClient()
        c.base_url = "http://test.local:8123"
        c.token = "test-token"
        c.timeout = 30
        c.verify_ssl = True
        c.httpx_client = MagicMock()
        c.httpx_client.request = AsyncMock(return_value=_response(200))
        c._supervised_detected = None
        c._is_admin = None
        c._admin_route_refused = False
        c._admin_route_lock = asyncio.Lock()
        c._current_user = AsyncMock(return_value=_current_user(False))
        return c


@pytest.mark.parametrize(
    ("method", "endpoint"),
    [
        ("GET", "/config/automation/config/123"),
        ("GET", "config/script/config/morning"),
        ("DELETE", "config/scene/config/456"),
        ("POST", "/config/automation/config/123"),
        ("POST", "/states/sensor.x"),
        ("DELETE", "/states/sensor.x"),
        ("POST", "/events/my_event"),
        ("POST", "/template"),
        ("GET", "/error_log"),
        ("GET", "/error_log?lines=50"),
        ("GET", "/stream"),
        ("POST", "/config/core/check_config"),
        ("GET", "/diagnostics/config_entry/abc"),
        ("GET", "/diagnostics/config_entry/abc/device/dev1"),
        ("DELETE", "/config/config_entries/entry/abc"),
        ("POST", "/config/config_entries/entry/abc/reload"),
        ("POST", "/config/config_entries/flow"),
        ("POST", "/config/config_entries/flow/f1"),
        ("GET", "/config/config_entries/flow/f1?step=user"),
        ("POST", "/config/config_entries/options/flow"),
        ("POST", "/config/config_entries/options/flow/f1"),
        ("POST", "/config/config_entries/subentries/flow"),
        ("GET", "/config/config_entries/subentries/flow/f1"),
    ],
)
def test_admin_only_routes_match(method, endpoint):
    assert is_admin_only_route(method, endpoint)


@pytest.mark.parametrize(
    ("method", "endpoint"),
    [
        ("GET", "/config"),
        ("GET", "/states"),
        ("GET", "/states/sensor.x"),
        ("GET", "/services"),
        ("POST", "/services/light/turn_on"),
        ("GET", "/logbook/2026-01-01T00:00:00"),
        ("GET", "/calendars/calendar.home"),
        ("POST", "/conversation/process"),
        # Listing config entries is open to every authenticated user in core.
        ("GET", "/config/config_entries/entry"),
        # Aborting a flow has no admin gate in core.
        ("DELETE", "/config/config_entries/flow/f1"),
        ("DELETE", "/config/config_entries/options/flow/f1"),
        # The flow index answers GET with 405, not an admin refusal.
        ("GET", "/config/config_entries/flow"),
        # The hassio proxy returns its 401 instead of raising, so it is not
        # counted; the logo/icon paths are open to everyone.
        ("GET", "/hassio/core/logs"),
        ("GET", "/hassio/addons/core_ssh/logo"),
    ],
)
def test_routes_open_to_non_admin_do_not_match(method, endpoint):
    assert not is_admin_only_route(method, endpoint)


@pytest.mark.asyncio
async def test_non_admin_token_refuses_admin_route_without_sending(client):
    with pytest.raises(HomeAssistantAdminRequiredError) as exc:
        await client._raw_request("GET", "/config/automation/config/123")
    assert "/api/config/automation/config/123" in str(exc.value)
    client.httpx_client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_required_error_is_an_auth_error(client):
    with pytest.raises(HomeAssistantAuthError):
        await client._raw_request("POST", "/events/test_event")


@pytest.mark.asyncio
async def test_non_admin_token_still_sends_open_routes_without_probing(client):
    await client._raw_request("GET", "/states")
    client.httpx_client.request.assert_awaited_once()
    client._current_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_admin_token_sends_admin_route(client):
    client._current_user = AsyncMock(return_value=_current_user(True))
    await client._raw_request("GET", "/config/automation/config/123")
    client.httpx_client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_probe_answer_is_cached_per_client(client):
    for _ in range(3):
        with pytest.raises(HomeAssistantAdminRequiredError):
            await client._raw_request("GET", "/config/script/config/s")
    client._current_user.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [None, {"id": "u1"}, {"id": "u1", "is_admin": "yes"}],
    ids=["no-answer", "no-is_admin-field", "non-bool-is_admin"],
)
async def test_first_admin_route_answer_settles_an_unanswered_probe(client, user):
    """A non-401 below 500 from an admin-only route proves the token is an admin's."""
    client._current_user = AsyncMock(return_value=user)
    await client._raw_request("GET", "/config/automation/config/123")
    await client._raw_request("GET", "/config/automation/config/123")
    assert client.httpx_client.request.await_count == 2
    client._current_user.assert_awaited_once()
    assert client.known_is_admin is True


@pytest.mark.asyncio
async def test_server_error_does_not_settle_an_unanswered_probe(client):
    client._current_user = AsyncMock(return_value=None)
    client.httpx_client.request = AsyncMock(return_value=_response(500))
    for _ in range(2):
        with pytest.raises(HomeAssistantAPIError):
            await client._raw_request("POST", "/config/core/check_config")
    assert client._current_user.await_count == 2
    assert client.known_is_admin is None


def _fake_ws(*, connected=True, reply=None, error=None):
    ws = MagicMock()
    ws.connect = AsyncMock(return_value=connected)
    ws.send_command = AsyncMock(return_value=reply, side_effect=error)
    ws.disconnect = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_current_user_asks_on_its_own_connection(client):
    """The pooled connection may be running the caller's own bus handler."""
    client.send_websocket_message = AsyncMock()
    ws = _fake_ws(reply={"success": True, "result": _current_user(False)})
    with patch(
        "ha_mcp.client.websocket_client.HomeAssistantWebSocketClient",
        return_value=ws,
    ) as ws_class:
        user = await HomeAssistantClient._current_user(client)
    assert user == _current_user(False)
    ws_class.assert_called_once_with(
        client.base_url, client.token, verify_ssl=client.verify_ssl
    )
    ws.send_command.assert_awaited_once_with("auth/current_user")
    ws.disconnect.assert_awaited_once()
    client.send_websocket_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ws",
    [
        _fake_ws(connected=False),
        _fake_ws(error=HomeAssistantCommandError("Command failed: unknown")),
        _fake_ws(error=ConnectionResetError("peer reset")),
    ],
    ids=["connect-failed", "command-failed", "connection-reset"],
)
async def test_current_user_returns_none_when_unanswered(client, ws):
    with patch(
        "ha_mcp.client.websocket_client.HomeAssistantWebSocketClient",
        return_value=ws,
    ):
        assert await HomeAssistantClient._current_user(client) is None
    ws.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_401_on_admin_route_names_the_admin_requirement(client):
    client._current_user = AsyncMock(return_value=None)
    client.httpx_client.request = AsyncMock(return_value=_response(401))
    with pytest.raises(HomeAssistantAuthError) as exc:
        await client._raw_request("GET", "/config/automation/config/123")
    assert "administrator" in str(exc.value)


@pytest.mark.asyncio
async def test_unanswered_probe_lets_one_concurrent_admin_request_out(client):
    """Ten parallel ``ha_search`` fetches must not all reach HA before the latch."""
    client._current_user = AsyncMock(return_value=None)

    async def slow_401(*_args, **_kwargs):
        await asyncio.sleep(0.01)
        return _response(401)

    client.httpx_client.request = AsyncMock(side_effect=slow_401)
    results = await asyncio.gather(
        *(
            client._raw_request("GET", f"/config/automation/config/{i}")
            for i in range(10)
        ),
        return_exceptions=True,
    )
    assert all(isinstance(r, HomeAssistantAuthError) for r in results)
    client.httpx_client.request.assert_awaited_once()
    # A re-probe per queued request would be a failed WS handshake each time
    # for an invalid token, which http.ban also counts.
    client._current_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmed_admin_requests_skip_the_lock(client):
    client._current_user = AsyncMock(return_value=_current_user(True))
    await client._raw_request("GET", "/config/automation/config/1")
    async with client._admin_route_lock:
        await asyncio.wait_for(
            client._raw_request("GET", "/config/automation/config/2"), 1
        )
    assert client.httpx_client.request.await_count == 2


@pytest.mark.asyncio
async def test_401_for_a_confirmed_admin_latches(client):
    """A token demoted mid-session must not keep sending admin-only requests."""
    client._current_user = AsyncMock(return_value=_current_user(True))
    await client._raw_request("GET", "/config/automation/config/1")
    client.httpx_client.request = AsyncMock(return_value=_response(401))
    with pytest.raises(HomeAssistantAuthError):
        await client._raw_request("GET", "/config/automation/config/2")
    with pytest.raises(HomeAssistantAdminRequiredError):
        await client._raw_request("GET", "/config/automation/config/3")
    client.httpx_client.request.assert_awaited_once()


@pytest.mark.asyncio
async def test_admin_route_refused_reflects_either_signal(client):
    assert client.admin_route_refused is False
    client._is_admin = False
    assert client.admin_route_refused is True
    client._is_admin = None
    client._admin_route_refused = True
    assert client.admin_route_refused is True


@pytest.mark.asyncio
async def test_401_on_admin_route_stops_further_admin_requests(client):
    client._current_user = AsyncMock(return_value=None)
    client.httpx_client.request = AsyncMock(return_value=_response(401))
    with pytest.raises(HomeAssistantAuthError) as exc:
        await client._raw_request("GET", "/config/automation/config/123")
    assert "returned 401" in str(exc.value)
    for _ in range(2):
        with pytest.raises(HomeAssistantAdminRequiredError) as exc:
            await client._raw_request("GET", "/config/automation/config/123")
        assert "not sent" in str(exc.value)
    client.httpx_client.request.assert_awaited_once()

    client.httpx_client.request = AsyncMock(return_value=_response(200))
    await client._raw_request("GET", "/states")
    client.httpx_client.request.assert_awaited_once()


def test_admin_required_error_classifies_as_insufficient_permissions():
    with pytest.raises(ToolError) as exc:
        exception_to_structured_error(
            HomeAssistantAdminRequiredError("GET /api/error_log is admin-only")
        )
    payload = json.loads(str(exc.value))
    assert payload["error"]["code"] == ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS.value
    assert payload["warnings"] == [NON_ADMIN_TOKEN_WARNING]


@pytest.mark.asyncio
async def test_first_non_admin_answer_logs_the_unsupported_warning(client, caplog):
    with caplog.at_level(logging.WARNING, logger="ha_mcp.client.rest_client"):
        assert await client.token_is_admin() is False
        assert await client.token_is_admin() is False
    warnings = [r for r in caplog.records if NON_ADMIN_TOKEN_WARNING in r.getMessage()]
    assert len(warnings) == 1


def _bridge(client):
    from types import SimpleNamespace

    from ha_mcp.tools.tools_code import _SandboxBridge

    return _SandboxBridge(
        MagicMock(), client, SimpleNamespace(code_mode_max_invocations=10)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call", "endpoint"),
    [("api_get", "/config/automation/config/1"), ("api_post", "/template")],
)
async def test_code_mode_admin_route_is_refused_with_the_warning(
    client, call, endpoint
):
    result = await getattr(_bridge(client), call)(endpoint)
    assert "admin-only" in result["error"]
    assert result["warnings"] == [NON_ADMIN_TOKEN_WARNING]
    client.httpx_client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_code_mode_open_route_still_sends(client):
    client.httpx_client.request = AsyncMock(
        return_value=_response(200, json_body=[{"entity_id": "sun.sun"}])
    )
    assert await _bridge(client).api_get("/states") == [{"entity_id": "sun.sun"}]


@pytest.mark.asyncio
async def test_non_admin_service_call_goes_over_websocket(client):
    """Over REST an admin-only service would answer 401, counted by http.ban."""
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": {"context": {"id": "c"}}}
    )
    result = await client.call_service(
        "light", "turn_on", {"entity_id": "light.kitchen"}
    )
    assert result == []
    client.httpx_client.request.assert_not_awaited()
    client.send_websocket_message.assert_awaited_once_with(
        {
            "type": "call_service",
            "domain": "light",
            "service": "turn_on",
            "service_data": {"entity_id": "light.kitchen"},
            "return_response": False,
        }
    )


@pytest.mark.asyncio
async def test_non_admin_service_call_keeps_the_response_shape(client):
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": True,
            "result": {"context": {"id": "c"}, "response": {"events": []}},
        }
    )
    result = await client.call_service(
        "calendar", "get_events", {}, return_response=True
    )
    assert result == {"changed_states": [], "service_response": {"events": []}}


@pytest.mark.asyncio
async def test_admin_only_service_refusal_raises_admin_required(client):
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": False,
            "error": "Command failed: Unauthorized",
            "error_code": "home_assistant_error",
        }
    )
    with pytest.raises(HomeAssistantAdminRequiredError) as exc:
        await client.call_service("automation", "reload")
    assert "automation.reload" in str(exc.value)
    client.httpx_client.request.assert_not_awaited()


@pytest.mark.asyncio
async def test_other_websocket_service_failures_raise_api_errors(client):
    client.send_websocket_message = AsyncMock(
        return_value={
            "success": False,
            "error": "Command failed: Service light.nope not found.",
            "error_code": "not_found",
        }
    )
    with pytest.raises(HomeAssistantAPIError) as exc:
        await client.call_service("light", "nope")
    assert exc.value.status_code == 400
    assert "not found" in str(exc.value)


@pytest.mark.asyncio
async def test_admin_service_call_stays_on_rest(client):
    client._current_user = AsyncMock(return_value=_current_user(True))
    client.httpx_client.request = AsyncMock(return_value=_response(200, json_body=[]))
    client.send_websocket_message = AsyncMock()
    await client.call_service("automation", "reload")
    client.httpx_client.request.assert_awaited_once()
    client.send_websocket_message.assert_not_awaited()


def test_constructor_seeds_a_known_admin_status():
    client = HomeAssistantClient(
        base_url="http://test.local:8123", token="t", verify_ssl=True, is_admin=True
    )
    assert client.known_is_admin is True
    assert client.admin_route_refused is False
