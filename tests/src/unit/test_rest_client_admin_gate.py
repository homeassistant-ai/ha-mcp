"""Unit tests for the REST client's non-admin guard (#2546).

Home Assistant answers a non-admin user's request to an admin-only REST route
with 401, and ``http.ban`` counts every 401 as a failed login. A config-body
``ha_search`` fans out ten of those at once, so a non-admin token got the
ha-mcp host IP-banned. The client now asks ``auth/current_user`` once per
token and refuses admin-only routes locally when the answer is non-admin.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.admin_routes import is_admin_only_route
from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
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
        ("GET", "/hassio/core/logs"),
        ("GET", "/hassio/addons/core_ssh/logs"),
        ("POST", "/config/core/check_config"),
        ("DELETE", "/config/config_entries/entry/abc"),
        ("POST", "/config/config_entries/entry/abc/reload"),
        ("POST", "/config/config_entries/flow"),
        ("POST", "/config/config_entries/flow/f1"),
        ("GET", "/config/config_entries/flow/f1"),
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
async def test_inconclusive_probe_sends_and_reprobes(client, user):
    client._current_user = AsyncMock(return_value=user)
    await client._raw_request("GET", "/config/automation/config/123")
    await client._raw_request("GET", "/config/automation/config/123")
    assert client.httpx_client.request.await_count == 2
    assert client._current_user.await_count == 2


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


def test_admin_required_error_classifies_as_insufficient_permissions():
    with pytest.raises(ToolError) as exc:
        exception_to_structured_error(
            HomeAssistantAdminRequiredError("GET /api/error_log is admin-only")
        )
    assert ErrorCode.AUTH_INSUFFICIENT_PERMISSIONS.value in str(exc.value)
