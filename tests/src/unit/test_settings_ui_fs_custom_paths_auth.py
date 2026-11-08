"""Settings UI custom-paths editor with a non-admin Home Assistant token.

The ha_mcp_tools services are registered admin-only, so Home Assistant refuses
a non-admin token before the component answers. The editor must name that
cause rather than report the component as unreachable.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import ha_mcp.tools.tools_filesystem as tf
from ha_mcp.client.rest_client import (
    HomeAssistantAdminRequiredError,
    HomeAssistantAuthError,
)
from ha_mcp.settings_ui import build_settings_handlers

# The token bootstrap is the first call Home Assistant refuses.
_ADMIN_REFUSAL = "Service ha_mcp_tools.get_caller_token is admin-only in Home Assistant"


@pytest.fixture(
    params=[
        (HomeAssistantAdminRequiredError(_ADMIN_REFUSAL), _ADMIN_REFUSAL),
        (
            HomeAssistantAuthError("Invalid token"),
            "Could not reach the ha_mcp_tools component: Invalid token",
        ),
    ],
    ids=["admin-refusal", "invalid-token"],
)
def expected_reason(request, monkeypatch):
    error, reason = request.param
    monkeypatch.setattr(tf, "is_filesystem_tools_enabled", lambda: True)
    monkeypatch.setattr(tf, "call_mcp_tools_service", AsyncMock(side_effect=error))
    return reason


@pytest.fixture
def handlers():
    return build_settings_handlers(server=MagicMock())


async def test_get_tells_an_admin_refusal_from_other_auth_errors(
    handlers, expected_reason
):
    resp = await handlers["get_fs_custom_paths"](MagicMock())
    body = json.loads(resp.body)
    assert body["available"] is False
    assert body["reason"] == expected_reason


async def test_save_tells_an_admin_refusal_from_other_auth_errors(
    handlers, expected_reason
):
    req = MagicMock()
    req.json = AsyncMock(return_value={"paths": ["pyscript"]})
    resp = await handlers["save_fs_custom_paths"](req)
    assert resp.status_code == 502
    assert json.loads(resp.body)["error"]["message"] == expected_reason
