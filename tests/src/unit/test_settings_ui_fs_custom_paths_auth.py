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
from ha_mcp.settings_ui._handlers_fs import _component_error_reason


@pytest.fixture
def handlers(monkeypatch):
    monkeypatch.setattr(tf, "is_filesystem_tools_enabled", lambda: True)

    async def refused(client, service, data):
        # The token bootstrap is the first call Home Assistant refuses.
        raise HomeAssistantAdminRequiredError(
            "Service ha_mcp_tools.get_caller_token is admin-only in Home Assistant"
        )

    monkeypatch.setattr(tf, "call_mcp_tools_service", refused)
    return build_settings_handlers(server=MagicMock())


@pytest.mark.asyncio
async def test_get_names_the_admin_refusal(handlers):
    resp = await handlers["get_fs_custom_paths"](MagicMock())
    body = json.loads(resp.body)
    assert body["available"] is False
    assert body["reason"].startswith("Service ha_mcp_tools.get_caller_token")


@pytest.mark.asyncio
async def test_save_names_the_admin_refusal(handlers):
    req = MagicMock()
    req.json = AsyncMock(return_value={"paths": ["pyscript"]})
    resp = await handlers["save_fs_custom_paths"](req)
    assert resp.status_code == 502
    message = json.loads(resp.body)["error"]["message"]
    assert message.startswith("Service ha_mcp_tools.get_caller_token")


def test_other_auth_errors_are_not_reported_as_admin_refusals():
    assert _component_error_reason(HomeAssistantAuthError("Invalid token")) is None
