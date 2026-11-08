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


@pytest.fixture(
    params=[
        (
            # The token bootstrap is the first call Home Assistant refuses.
            HomeAssistantAdminRequiredError(
                "Service ha_mcp_tools.get_caller_token is admin-only in Home Assistant"
            ),
            "Service ha_mcp_tools.get_caller_token",
        ),
        (
            HomeAssistantAuthError("Invalid token"),
            "Could not reach the ha_mcp_tools component: Invalid token",
        ),
    ],
    ids=["admin-refusal", "invalid-token"],
)
def reason_start(request, monkeypatch):
    error, start = request.param
    monkeypatch.setattr(tf, "is_filesystem_tools_enabled", lambda: True)
    monkeypatch.setattr(tf, "call_mcp_tools_service", AsyncMock(side_effect=error))
    return start


@pytest.fixture
def handlers():
    return build_settings_handlers(server=MagicMock())


async def test_get_tells_an_admin_refusal_from_other_auth_errors(
    handlers, reason_start
):
    resp = await handlers["get_fs_custom_paths"](MagicMock())
    body = json.loads(resp.body)
    assert body["available"] is False
    assert body["reason"].startswith(reason_start)


async def test_save_tells_an_admin_refusal_from_other_auth_errors(
    handlers, reason_start
):
    req = MagicMock()
    req.json = AsyncMock(return_value={"paths": ["pyscript"]})
    resp = await handlers["save_fs_custom_paths"](req)
    assert resp.status_code == 502
    assert json.loads(resp.body)["error"]["message"].startswith(reason_start)
