"""Unit tests for ha_config_set_yaml MCP tool wrapper."""

import os
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def enable_flag(monkeypatch):
    monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "true")
    yield


async def _make_tool():
    """Build a minimal mcp + client harness around register_yaml_config_tools."""
    from ha_mcp.tools.tools_yaml_config import register_yaml_config_tools

    captured: dict = {}

    class FakeMCP:
        def tool(self, **_kwargs):
            def decorator(fn):
                captured.setdefault("fns", []).append(fn)
                return fn
            return decorator

    client = MagicMock()
    client.get_services = AsyncMock(return_value=[{"domain": "ha_mcp_tools"}])
    client.send_websocket_message = AsyncMock()
    client.call_service = AsyncMock(
        return_value={"success": True, "file": "configuration.yaml"}
    )

    mcp = FakeMCP()
    register_yaml_config_tools(mcp, client)
    # Find the ha_config_set_yaml fn — only one tool registered in this module
    return captured["fns"][0], client


async def test_storage_collision_blocks_dispatch(monkeypatch):
    """If WS list shows a storage-mode dashboard with same url_path, reject."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [
                {"url_path": "energy-dash", "mode": "storage", "id": "abc"}
            ]
        }
    )

    # ToolError is raised — capture it
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await fn(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
        )
    # call_service must NOT have been called
    client.call_service.assert_not_called()


async def test_no_collision_dispatches(monkeypatch):
    """No matching storage-mode entry — dispatch proceeds."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [
                {"url_path": "other-dash", "mode": "storage", "id": "abc"}
            ]
        }
    )
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    client.call_service.assert_called_once()


async def test_non_dashboard_path_skips_ws_check(monkeypatch):
    """Single-key yaml_paths must not trigger the WS lookup."""
    fn, client = await _make_tool()
    await fn(
        yaml_path="template",
        action="add",
        content="- sensor: []\n",
    )
    client.send_websocket_message.assert_not_called()
    client.call_service.assert_called_once()
