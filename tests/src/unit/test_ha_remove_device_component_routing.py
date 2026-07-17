"""Routing tests for ``ha_remove_device`` over the ``ha_mcp_tools`` component gate.

``ha_remove_device`` fetched the ENTIRE device registry twice for one delete — once
in its body to read the device's ``config_entries``, and again in the
``@with_auto_backup`` capture (``backup_manager._fetch_device``, the same pre-write
snapshot ``ha_set_device`` also uses). Both single-device reads route through the
component's ``device_get`` when available. These tests pin the body routing
(component-hit, no whole-registry dump; legacy fallback on no-caps /
``unknown_command`` → invalidate; not-found falls back to the full list for the
suggestion) AND the capture read routing at the ``_fetch_device`` seam.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp import backup_manager
from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api, component_devices
from ha_mcp.tools.tools_registry import register_registry_tools

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_DEVICES = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["device_get", "device_list"],
    "limits": {},
}


def _raw_device(device_id: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": device_id,
        "name": f"Device {device_id}",
        "name_by_user": "Orphan",
        "area_id": None,
        "labels": [],
        "config_entries": ["cfg-1"],
        "connections": [],
        "identifiers": [["hue", "0xAABB"]],
        "disabled_by": None,
    }
    base.update(overrides)
    return base


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy device-registry-list dumps."""

    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._devices = list(devices or [])
        self.device_list_calls = 0
        self.remove_calls: list[str] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/device_registry/list":
            self.device_list_calls += 1
            return {"success": True, "result": list(self._devices)}
        if msg_type == "config/device_registry/remove_config_entry":
            self.remove_calls.append(msg["config_entry_id"])
            return {"success": True}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


def _build_remove_device(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_registry_tools(mcp, client)
    return registered["ha_remove_device"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.fixture(autouse=True)
def _auto_backup_off(monkeypatch) -> None:
    """Keep the ``@with_auto_backup`` gate off so the body tests exercise only the
    body's device read (the capture read is tested separately via _fetch_device)."""
    settings = MagicMock()
    settings.enable_auto_backup = False
    monkeypatch.setattr(
        "ha_mcp.tools.auto_backup.get_global_settings", lambda: settings
    )


def _device_get_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/device_get"
    ]


# --- body routing ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_body_served_by_component() -> None:
    """The body reads the device via device_get and removes its config entries;
    the whole device registry is never dumped."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": _raw_device("dev-1")},
    )
    client = RoutingClient()
    remove_device = _build_remove_device(client)

    with patch_ws(ws, component_devices):
        resp = await remove_device(device_id="dev-1")

    assert resp["success"] is True
    assert resp["config_entries_removed"] == 1
    assert client.remove_calls == ["cfg-1"]
    assert client.device_list_calls == 0
    assert len(_device_get_calls(ws)) == 1


@pytest.mark.asyncio
async def test_body_capsless_uses_legacy_list() -> None:
    """Old component → legacy config/device_registry/list for the body."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(devices=[_raw_device("dev-1")])
    remove_device = _build_remove_device(client)

    with patch_ws(ws, component_devices):
        resp = await remove_device(device_id="dev-1")

    assert resp["success"] is True
    assert client.device_list_calls == 1
    assert not _device_get_calls(ws)


@pytest.mark.asyncio
async def test_body_unknown_command_invalidates_and_falls_back() -> None:
    """unknown_command on device_get → invalidate caps + legacy list."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(devices=[_raw_device("dev-1")])
    remove_device = _build_remove_device(client)

    with patch_ws(ws, component_devices):
        resp = await remove_device(device_id="dev-1")

    assert resp["success"] is True
    assert client.device_list_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_body_not_found_falls_back_to_list_for_suggestions() -> None:
    """A component-reported missing device falls back to the LEGACY list so the
    not-found error can suggest valid ids (device_get authoritatively said None)."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": None},
    )
    client = RoutingClient(devices=[_raw_device("other")])
    remove_device = _build_remove_device(client)

    with patch_ws(ws, component_devices), pytest.raises(ToolError) as excinfo:
        await remove_device(device_id="ghost")

    assert "Device not found" in str(excinfo.value) or "RESOURCE_NOT_FOUND" in str(
        excinfo.value
    )
    # The legacy device list served the not-found suggestion.
    assert client.device_list_calls == 1


# --- auto-backup capture read routing (_fetch_device) ---------------------------
@pytest.mark.asyncio
async def test_capture_read_served_by_component(monkeypatch) -> None:
    """The pre-write snapshot capture (``_fetch_device``, shared by ha_set_device /
    ha_remove_device) reads the device via device_get and never dumps the whole
    registry through the legacy ``_ws_send``."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": _raw_device("dev-1")},
    )
    legacy = AsyncMock()
    monkeypatch.setattr(backup_manager, "_ws_send", legacy)
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        result = await backup_manager._fetch_device(client, "dev-1")

    assert result["id"] == "dev-1"
    assert len(_device_get_calls(ws)) == 1
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_capture_read_capsless_uses_legacy_ws_send(monkeypatch) -> None:
    """No device_get capability → the capture falls back to the legacy list read."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    legacy = AsyncMock(return_value=[_raw_device("dev-1")])
    monkeypatch.setattr(backup_manager, "_ws_send", legacy)
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        result = await backup_manager._fetch_device(client, "dev-1")

    assert result["id"] == "dev-1"
    legacy.assert_awaited_once()
    assert not _device_get_calls(ws)


@pytest.mark.asyncio
async def test_capture_read_unknown_command_invalidates(monkeypatch) -> None:
    """unknown_command on the capture's device_get → invalidate caps + legacy read."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    legacy = AsyncMock(return_value=[_raw_device("dev-1")])
    monkeypatch.setattr(backup_manager, "_ws_send", legacy)
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        result = await backup_manager._fetch_device(client, "dev-1")

    assert result["id"] == "dev-1"
    legacy.assert_awaited_once()
    assert client not in component_api._CAPS_CACHE
