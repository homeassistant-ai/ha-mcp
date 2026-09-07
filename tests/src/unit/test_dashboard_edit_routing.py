"""Native edit routing preserves results and never repeats an ambiguous write."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import HomeAssistantCommandTimeout
from ha_mcp.tools import auto_backup, tools_config_dashboards
from ha_mcp.tools.tools_config_dashboards import DashboardConfigTools
from ha_mcp.utils.config_hash import compute_config_hash


@pytest.fixture
def legacy_dashboard(monkeypatch):
    document = {"views": [{"cards": [{"type": "button", "icon": "mdi:lamp"}]}]}
    client = MagicMock(base_url="http://ha.local:8123", token="test", verify_ssl=True)
    messages = []

    async def send(message):
        messages.append(deepcopy(message))
        if message["type"] == "lovelace/config":
            return {"success": True, "result": deepcopy(document)}
        if message["type"] == "lovelace/config/save":
            document.clear()
            document.update(deepcopy(message["config"]))
            return {"success": True, "result": None}
        return {"success": True, "result": []}

    client.send_websocket_message = AsyncMock(side_effect=send)
    monkeypatch.setattr(tools_config_dashboards, "get_component_caps", AsyncMock(return_value=None))
    monkeypatch.setattr(auto_backup, "get_global_settings", lambda: SimpleNamespace(enable_auto_backup=False))
    return client, document, messages


async def test_structured_edit_works_without_component(legacy_dashboard):
    client, document, messages = legacy_dashboard
    result = await DashboardConfigTools(client).ha_config_set_dashboard(
        url_path="test-dashboard",
        config_hash=compute_config_hash(document),
        patch=[{"op": "replace", "path": "/views/0/cards/0/icon", "value": "mdi:music-box-multiple"}],
        MandatoryBPS=False,
    )
    assert document["views"][0]["cards"][0]["icon"] == "mdi:music-box-multiple"
    assert result["config_hash"] == compute_config_hash(document)
    assert result["write_committed"] is True
    assert result["post_write_verified"] is True
    assert sum(m["type"] == "lovelace/config/save" for m in messages) == 1


async def test_patch_conflict_never_saves(legacy_dashboard):
    client, document, messages = legacy_dashboard
    before = deepcopy(document)
    with pytest.raises(ToolError, match="conflict"):
        await DashboardConfigTools(client).ha_config_set_dashboard(
            url_path="test-dashboard", config_hash="stale",
            patch=[{"op": "remove", "path": "/views/0"}], MandatoryBPS=False,
        )
    assert document == before
    assert not any(m["type"] == "lovelace/config/save" for m in messages)


async def test_sent_native_timeout_is_not_a_fallback(monkeypatch):
    from ha_mcp.tools import component_dashboard_edit as native

    monkeypatch.setattr(native, "get_component_caps", AsyncMock(return_value=SimpleNamespace(capabilities=frozenset({"dashboard_edit"}))))
    ws = SimpleNamespace(send_command=AsyncMock(side_effect=HomeAssistantCommandTimeout("timeout")))
    monkeypatch.setattr(native, "get_websocket_client", AsyncMock(return_value=ws))
    client = SimpleNamespace(base_url="http://ha.local", token="test", verify_ssl=True)
    with pytest.raises(ToolError, match="unknown"):
        await native.edit_dashboard_via_component(client, "test-dashboard", expected_hash="old", patch=[{"op": "remove", "path": "/views/0"}])
    assert ws.send_command.await_count == 1
