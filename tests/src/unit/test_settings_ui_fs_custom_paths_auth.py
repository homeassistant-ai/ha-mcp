"""Settings UI custom-paths editor when Home Assistant refuses the call.

Home Assistant refuses a rejected token before the component answers, and a
non-admin one too because the ha_mcp_tools services are registered admin-only.
The editor must name that cause rather than report the component as unreachable, and the
refusal must survive a proxy that replaces the body of a 5xx response.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_mcp.tools.tools_filesystem as tf
from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAuthError,
    HomeAssistantCommandNotSent,
)
from ha_mcp.config import OAUTH_MODE_TOKEN
from ha_mcp.settings_ui import build_settings_handlers

# The token bootstrap is the first call Home Assistant refuses.
_ADMIN_REFUSAL = "Service ha_mcp_tools.get_caller_token is admin-only in Home Assistant"
_REJECTED_TOKEN = "Invalid authentication token"


def _not_sent_after_rejection() -> HomeAssistantCommandNotSent:
    """The shape a REST 401 takes once the WebSocket retry is refused as well."""
    error = HomeAssistantCommandNotSent("WebSocket command was not sent")
    error.__cause__ = HomeAssistantAuthError(_REJECTED_TOKEN)
    return error


@pytest.fixture(
    params=[
        (HomeAssistantAdminRequiredError(_ADMIN_REFUSAL), _ADMIN_REFUSAL),
        (HomeAssistantAuthError(_REJECTED_TOKEN), _REJECTED_TOKEN),
        (_not_sent_after_rejection(), _REJECTED_TOKEN),
    ],
    ids=["admin-refusal", "rejected-token", "rejected-on-retry"],
)
def refused_with(request, monkeypatch):
    """Make the component call fail; return the message the editor must show."""
    error, message = request.param
    settings = MagicMock(homeassistant_token="a-long-lived-token")
    monkeypatch.setattr("ha_mcp.config.get_global_settings", lambda: settings)
    monkeypatch.setattr(tf, "is_filesystem_tools_enabled", lambda: True)
    monkeypatch.setattr(tf, "call_mcp_tools_service", AsyncMock(side_effect=error))
    return message


@pytest.fixture
def handlers():
    return build_settings_handlers(server=MagicMock())


def _assert_names_the_refusal(reason: str, message: str) -> None:
    assert reason.startswith(f"{message}. ")
    assert "Could not reach" not in reason


async def test_get_names_a_refusal_instead_of_an_unreachable_component(
    handlers, refused_with
):
    resp = await handlers["get_fs_custom_paths"](MagicMock())
    body = json.loads(resp.body)
    assert body["available"] is False
    _assert_names_the_refusal(body["reason"], refused_with)


async def test_save_refusal_reaches_the_page_through_a_proxy(handlers, refused_with):
    req = MagicMock()
    req.json = AsyncMock(return_value={"paths": ["pyscript"]})
    resp = await handlers["save_fs_custom_paths"](req)
    assert resp.status_code < 500
    _assert_names_the_refusal(json.loads(resp.body)["error"]["message"], refused_with)


async def test_oauth_mode_is_not_told_to_fix_the_server_token(handlers, monkeypatch):
    """The settings routes have no request token there: Home Assistant was not asked."""
    settings = MagicMock(homeassistant_token=OAUTH_MODE_TOKEN)
    no_token = HomeAssistantAuthError("No OAuth token in request context")
    monkeypatch.setattr("ha_mcp.config.get_global_settings", lambda: settings)
    monkeypatch.setattr(tf, "is_filesystem_tools_enabled", lambda: True)
    monkeypatch.setattr(tf, "call_mcp_tools_service", AsyncMock(side_effect=no_token))
    resp = await handlers["get_fs_custom_paths"](MagicMock())
    assert "HOMEASSISTANT_TOKEN" not in json.loads(resp.body)["reason"]


async def test_save_component_rejection_reaches_the_page_through_a_proxy(
    handlers, monkeypatch
):
    rejection = {"service_response": {"success": False, "error": "Bad caller token"}}
    monkeypatch.setattr(tf, "is_filesystem_tools_enabled", lambda: True)
    monkeypatch.setattr(tf, "call_mcp_tools_service", AsyncMock(return_value=rejection))
    req = MagicMock()
    req.json = AsyncMock(return_value={"paths": ["pyscript"]})
    resp = await handlers["save_fs_custom_paths"](req)
    assert resp.status_code < 500
    assert json.loads(resp.body)["error"]["message"] == "Bad caller token"
