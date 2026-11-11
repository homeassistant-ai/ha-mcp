"""A dashboard edit that sent nothing reports a rejected token (raised directly or
as the cause) as AUTH_*, anything else under the code of the reason it failed at.

The edits run on the server's own client against a real ``hass`` holding the
dashboard. A rejected token is Core refusing a revoked one; a failed connect is
the client getting no answer at all."""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantClient,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)
from ha_mcp.tools import component_dashboard_edit
from ha_mcp.tools.component_api import get_component_caps
from ha_mcp.tools.dashboard_edit_errors import raise_dashboard_edit_unsent
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

_DOCUMENT = {"views": [{"cards": [{"type": "button", "icon": "mdi:lamp"}]}]}


async def _dashboard(hass: HomeAssistant, client: HomeAssistantClient) -> str:
    """Store ``test-dashboard`` in Core and return its config hash."""
    assert await async_setup_component(hass, "lovelace", {})
    created = await client.send_websocket_message(
        {
            "type": "lovelace/dashboards/create",
            "url_path": "test-dashboard",
            "title": "Test",
        }
    )
    assert created["success"] is True
    saved = await client.send_websocket_message(
        {
            "type": "lovelace/config/save",
            "url_path": "test-dashboard",
            "config": _DOCUMENT,
        }
    )
    assert saved["success"] is True
    return compute_config_hash(_DOCUMENT)


async def _revoke_token(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """Revoke the client's token and drop its connections: Core refuses the next."""
    refresh_token = hass.auth.async_validate_access_token(client.token)
    assert refresh_token is not None
    hass.auth.async_remove_refresh_token(refresh_token)
    await websocket_manager.disconnect()


@pytest.mark.parametrize(
    ("cause", "code", "reusable_hint"),
    [
        (None, "CONNECTION_FAILED", True),
        (HomeAssistantConnectionError("Connection refused"), "CONNECTION_FAILED", True),
        (HomeAssistantAuthError("Invalid token"), "AUTH_INVALID_TOKEN", False),
    ],
    ids=["not_authenticated", "connection_refused", "token_rejected_on_connect"],
)
def test_unsent_save_is_reported_by_its_cause(
    cause: BaseException | None, code: str, reusable_hint: bool
) -> None:
    unsent = HomeAssistantCommandNotSent("WebSocket not authenticated")
    unsent.__cause__ = cause

    with pytest.raises(ToolError) as caught:
        raise_dashboard_edit_unsent(unsent, "test-dashboard", "set", "write_not_sent")

    error = json.loads(str(caught.value))
    assert error["error"]["code"] == code
    assert (
        "The config write was not sent; its edit parameters can be reused"
        in error["error"]["suggestions"]
    ) is reusable_hint
    assert error["reason"] == "write_not_sent"
    assert error["url_path"] == "test-dashboard"
    assert error["write_committed"] is False


# Core closes a socket whose token it refused; the client's close reply then
# re-arms aiohttp's heartbeat timer on that closed server socket, which nothing
# cancels.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_legacy_save_with_a_rejected_token_is_an_auth_error(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without the component the save is the legacy command. The token is
    revoked after the dashboard was checked, so the save's connect is refused
    before anything is sent."""
    config_hash = await _dashboard(hass, core_client)
    tools = DashboardConfigTools(core_client)

    async def revoke_before_the_save(*_args: Any) -> None:
        await _revoke_token(hass, core_client)

    monkeypatch.setattr(tools, "_check_dashboard_replace_hash", revoke_before_the_save)
    with pytest.raises(ToolError) as caught:
        await tools.ha_config_set_dashboard(
            "test-dashboard",
            config_hash=config_hash,
            MandatoryBPS=False,
            config={"views": []},
        )
    error = json.loads(str(caught.value))
    assert error["error"]["code"] == "AUTH_INVALID_TOKEN"
    assert error["reason"] == "write_not_sent"


async def test_component_edit_connect_failure_is_reported_by_its_type(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component reports dashboard_edit; the edit's own connect gets no answer."""
    config_hash = await _dashboard(hass, component_core_client)

    with (
        patch.object(
            component_dashboard_edit,
            "get_websocket_client",
            AsyncMock(
                side_effect=HomeAssistantConnectionError(
                    "Failed to connect to Home Assistant WebSocket"
                )
            ),
        ),
        patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as messages,
        pytest.raises(ToolError) as caught,
    ):
        await DashboardConfigTools(component_core_client).ha_config_set_dashboard(
            "test-dashboard",
            patch=[],
            config_hash=config_hash,
            MandatoryBPS=False,
        )
    result = json.loads(str(caught.value))
    assert result["error"]["code"] == "SERVICE_CALL_FAILED"
    assert result["reason"] == "load_failed"
    assert result["write_committed"] is False
    messages.assert_not_called()


# The refused socket's heartbeat timer, as above.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_component_edit_token_rejected_on_connect_is_an_auth_error(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component's capabilities were read; the edit's own connect is refused."""
    config_hash = await _dashboard(hass, component_core_client)
    caps = await get_component_caps(component_core_client)
    assert caps is not None
    await _revoke_token(hass, component_core_client)

    with (
        patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as messages,
        pytest.raises(ToolError) as caught,
    ):
        await DashboardConfigTools(component_core_client).ha_config_set_dashboard(
            "test-dashboard",
            patch=[],
            config_hash=config_hash,
            MandatoryBPS=False,
        )
    result = json.loads(str(caught.value))
    assert result["error"]["code"] == "AUTH_INVALID_TOKEN"
    assert result["reason"] == "load_failed"
    assert result["write_committed"] is False
    messages.assert_not_called()


# The refused socket's heartbeat timer, as above.
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_component_edit_token_rejected_on_the_probe_is_an_auth_error(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The real probe runs on a cold cache; the rejection it raised itself is the
    cause, while the same failure read back from the cache keeps load_failed."""
    config_hash = await _dashboard(hass, component_core_client)
    await _revoke_token(hass, component_core_client)

    codes: list[str] = []
    with (
        patch.object(
            HomeAssistantWebSocketClient,
            "connect",
            autospec=True,
            side_effect=HomeAssistantWebSocketClient.connect,
        ) as connect,
        patch.object(
            component_core_client,
            "send_websocket_message",
            wraps=component_core_client.send_websocket_message,
        ) as messages,
    ):
        for _ in range(2):
            with pytest.raises(ToolError) as caught:
                await DashboardConfigTools(
                    component_core_client
                ).ha_config_set_dashboard(
                    "test-dashboard",
                    patch=[],
                    config_hash=config_hash,
                    MandatoryBPS=False,
                )
            result = json.loads(str(caught.value))
            assert result["reason"] == "load_failed"
            assert result["write_committed"] is False
            codes.append(result["error"]["code"])
    assert codes == ["AUTH_INVALID_TOKEN", "SERVICE_CALL_FAILED"]
    connect.assert_awaited_once()
    messages.assert_not_called()
