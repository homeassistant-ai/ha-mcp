"""A dashboard save that never left the process reports an auth cause as AUTH_*,
anything else as a connection failure."""

import json
from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
)
from ha_mcp.tools import component_dashboard_edit
from ha_mcp.tools.dashboard_edit_errors import raise_dashboard_write_not_sent
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

from .test_dashboard_edit_routing import legacy_dashboard  # noqa: F401


@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (None, "CONNECTION_FAILED"),
        (HomeAssistantAuthError("Invalid token"), "AUTH_INVALID_TOKEN"),
    ],
    ids=["not_authenticated", "token_rejected_on_connect"],
)
def test_unsent_save_is_reported_by_its_cause(cause, code) -> None:
    unsent = HomeAssistantCommandNotSent("WebSocket not authenticated")
    unsent.__cause__ = cause

    with pytest.raises(ToolError) as caught:
        raise_dashboard_write_not_sent(unsent, "test-dashboard", "set")

    error = json.loads(str(caught.value))
    assert error["error"]["code"] == code
    assert error["reason"] == "write_not_sent"
    assert error["url_path"] == "test-dashboard"
    assert error["write_committed"] is False


async def test_legacy_save_with_a_rejected_token_is_an_auth_error(
    legacy_dashboard,  # noqa: F811
    monkeypatch,
) -> None:
    client, document, _messages = legacy_dashboard
    tools = DashboardConfigTools(client)
    monkeypatch.setattr(
        tools,
        "_ensure_dashboard_exists",
        AsyncMock(return_value=(True, "id", False, None)),
    )
    monkeypatch.setattr(
        component_dashboard_edit, "get_component_caps", AsyncMock(return_value=None)
    )
    original_send = client.send_websocket_message.side_effect
    rejected = HomeAssistantCommandNotSent("WebSocket authentication failed")
    rejected.__cause__ = HomeAssistantAuthError("Invalid token")

    async def send(message):
        if message["type"] == "lovelace/config/save":
            raise rejected
        return await original_send(message)

    client.send_websocket_message.side_effect = send
    with pytest.raises(ToolError) as caught:
        await tools.ha_config_set_dashboard(
            "test-dashboard",
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
            config={"views": []},
        )
    error = json.loads(str(caught.value))
    assert error["error"]["code"] == "AUTH_INVALID_TOKEN"
    assert error["reason"] == "write_not_sent"
