"""Unit tests for ha_config_set_yaml MCP tool wrapper."""

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture(autouse=True)
def enable_flag(monkeypatch):
    """Enable the yaml-config tool flag and bust the cached global settings.

    `get_global_settings()` memoizes a `Settings` object the first time it's called.
    If anything in the test process imported the module before this fixture ran,
    the cached settings have ENABLE_YAML_CONFIG_EDITING=False and our env var is
    ignored. Reset the cache before AND after to keep tests hermetic.
    """
    from ha_mcp import config as ha_mcp_config

    monkeypatch.setenv("ENABLE_YAML_CONFIG_EDITING", "true")
    monkeypatch.setattr(ha_mcp_config, "_settings", None)
    yield
    # Reset the cache so other tests don't see our enabled flag.
    ha_mcp_config._settings = None


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


async def test_ws_failure_skips_check_and_dispatches(monkeypatch):
    """WS query failure must warn-and-skip, not block dispatch."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(side_effect=ConnectionError("boom"))
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    client.call_service.assert_called_once()


async def test_ws_returns_bare_list_blocks_collision(monkeypatch):
    """WS may return a bare list (no 'result' wrapper); collision still detected."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value=[{"url_path": "energy-dash", "mode": "storage", "id": "abc"}]
    )

    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        await fn(
            yaml_path="lovelace.dashboards.energy-dash",
            action="add",
            content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
        )
    client.call_service.assert_not_called()


async def test_yaml_mode_existing_does_not_block(monkeypatch):
    """Existing yaml-mode entry with same url_path is NOT a collision; dispatch proceeds.
    (HA itself surfaces dup errors at config_check time.)"""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [
                {"url_path": "energy-dash", "mode": "yaml", "id": "abc"}
            ]
        }
    )
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    client.call_service.assert_called_once()


async def test_ws_returns_unexpected_shape_warns_and_dispatches(monkeypatch):
    """Unexpected WS response shape (non-dict, non-list) skips collision check."""
    fn, client = await _make_tool()
    client.send_websocket_message = AsyncMock(return_value="weird")
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="add",
        content="mode: yaml\ntitle: x\nfilename: dashboards/x.yaml\n",
    )
    client.call_service.assert_called_once()


async def test_remove_action_skips_collision_check(monkeypatch):
    """`remove` must NOT pay the WS round-trip — users need to be able to
    clean up YAML entries even when a storage-mode dashboard owns the same
    url_path (migration scenario)."""
    fn, client = await _make_tool()
    # Set up the collision return so we'd notice if the check ran.
    client.send_websocket_message = AsyncMock(
        return_value={
            "result": [
                {"url_path": "energy-dash", "mode": "storage", "id": "abc"}
            ]
        }
    )
    await fn(
        yaml_path="lovelace.dashboards.energy-dash",
        action="remove",
    )
    client.send_websocket_message.assert_not_called()
    client.call_service.assert_called_once()
