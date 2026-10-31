"""A dashboard edit that sent nothing reports a rejected token (raised directly or
as the cause) as AUTH_*, anything else under the code of the reason it failed at."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_dashboard_edit
from ha_mcp.tools.dashboard_edit_errors import raise_dashboard_edit_unsent
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash

from .test_dashboard_edit_routing import (  # noqa: F401
    legacy_dashboard,
    native_socket,
)


@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (None, "CONNECTION_FAILED"),
        (HomeAssistantAuthError("Invalid token"), "AUTH_INVALID_TOKEN"),
    ],
    ids=["not_authenticated", "token_rejected_on_connect"],
)
def test_unsent_save_is_reported_by_its_cause(
    cause: BaseException | None, code: str
) -> None:
    unsent = HomeAssistantCommandNotSent("WebSocket not authenticated")
    unsent.__cause__ = cause

    with pytest.raises(ToolError) as caught:
        raise_dashboard_edit_unsent(unsent, "test-dashboard", "set", "write_not_sent")

    error = json.loads(str(caught.value))
    assert error["error"]["code"] == code
    assert error["reason"] == "write_not_sent"
    assert error["url_path"] == "test-dashboard"
    assert error["write_committed"] is False


async def test_legacy_save_with_a_rejected_token_is_an_auth_error(
    legacy_dashboard: tuple[MagicMock, dict[str, Any], list[dict[str, Any]]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
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

    async def send(message: dict[str, Any]) -> dict[str, Any]:
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


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (
            HomeAssistantConnectionError(
                "Failed to connect to Home Assistant WebSocket"
            ),
            "SERVICE_CALL_FAILED",
        ),
        (
            HomeAssistantAuthError("WebSocket authentication failed: Invalid token"),
            "AUTH_INVALID_TOKEN",
        ),
    ],
    ids=["connect_failed", "token_rejected_on_connect"],
)
async def test_component_edit_connect_failure_is_reported_by_its_type(
    legacy_dashboard: tuple[MagicMock, dict[str, Any], list[dict[str, Any]]],  # noqa: F811
    native_socket: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    code: str,
) -> None:
    client, document, messages = legacy_dashboard
    monkeypatch.setattr(
        component_dashboard_edit,
        "get_websocket_client",
        AsyncMock(side_effect=error),
    )
    with pytest.raises(ToolError) as caught:
        await DashboardConfigTools(client).ha_config_set_dashboard(
            "test-dashboard",
            patch=[],
            config_hash=compute_config_hash(document),
            MandatoryBPS=False,
        )
    result = json.loads(str(caught.value))
    assert result["error"]["code"] == code
    assert result["reason"] == "load_failed"
    assert result["write_committed"] is False
    assert messages == []
