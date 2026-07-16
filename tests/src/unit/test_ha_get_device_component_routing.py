"""Routing tests for ``ha_get_device`` over the ``ha_mcp_tools`` component gate.

``ha_get_device`` pulled the ENTIRE device registry (and the entity registry) for
a single lookup. When the component advertises ``device_get`` / ``device_list``, a
single lookup fetches just the target device in one ``ha_mcp_tools/device_get``
frame and a list read comes from ``ha_mcp_tools/device_list`` — neither dumps the
whole device registry. The entity registry is still read (a device's entity list
has no per-device capability), except in summary list mode where nothing needs it,
so it is skipped. These tests pin that: the component-served device path never
sends ``config/device_registry/list``, list mode uses ``device_list``, a summary
list skips the entity fetch, and every backend degradation (no caps,
``unknown_command`` → invalidate + fall back) still returns the byte-identical
legacy result.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

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
    """A ``config/device_registry/list`` element (also the ``device_get`` body)."""
    base: dict[str, Any] = {
        "id": device_id,
        "name": f"Device {device_id}",
        "name_by_user": None,
        "area_id": None,
        "labels": [],
        "manufacturer": "Acme",
        "model": "M1",
        "sw_version": None,
        "hw_version": None,
        "serial_number": None,
        "via_device_id": None,
        "disabled_by": None,
        "config_entries": ["cfg-1"],
        "connections": [],
        # A non-radio integration so the single-device enrichers (zha/zwave/matter)
        # do not fire — those dedicated paths are out of the device_get seam.
        "identifiers": [["hue", "0xAABB"]],
    }
    base.update(overrides)
    return base


def _entity_row(entity_id: str, device_id: str) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "device_id": device_id,
        "platform": "zha",
        "name": None,
        "original_name": entity_id,
    }


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy registry-list fetches."""

    def __init__(
        self,
        devices: list[dict[str, Any]] | None = None,
        entities: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._devices = list(devices or [])
        self._entities = list(entities or [])
        self.device_list_calls = 0
        self.entity_list_calls = 0

    async def get_config(self) -> dict[str, Any]:
        return {"time_zone": "UTC"}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/device_registry/list":
            self.device_list_calls += 1
            return {"success": True, "result": list(self._devices)}
        if msg_type == "config/entity_registry/list":
            self.entity_list_calls += 1
            return {"success": True, "result": list(self._entities)}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


def _build_get_device(client: Any) -> Any:
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
    return registered["ha_get_device"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _device_get_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/device_get"
    ]


@pytest.mark.asyncio
async def test_single_lookup_served_by_component() -> None:
    """A device_id lookup fetches only that device via device_get; the whole
    device registry is never dumped (the entity registry is still read once)."""
    dev = _raw_device("dev-1", name_by_user="Kitchen")
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": dev},
    )
    client = RoutingClient(entities=[_entity_row("sensor.kitchen", "dev-1")])
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(device_id="dev-1")

    assert resp["success"] is True
    assert resp["device"]["device_id"] == "dev-1"
    assert resp["device"]["name"] == "Kitchen"
    # The device came from the component; the legacy device dump never ran.
    assert client.device_list_calls == 0
    assert len(_device_get_calls(ws)) == 1
    assert _device_get_calls(ws)[0].kwargs["device_id"] == "dev-1"
    # The entity registry is still needed for the device's entity list.
    assert resp["entity_count"] == 1
    assert client.entity_list_calls == 1


@pytest.mark.asyncio
async def test_entity_id_lookup_resolves_then_device_get() -> None:
    """entity_id mode resolves the device from the entity registry, then reads the
    device via device_get (still no whole device-registry dump)."""
    dev = _raw_device("dev-1")
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": dev},
    )
    client = RoutingClient(entities=[_entity_row("light.lr", "dev-1")])
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(entity_id="light.lr")

    assert resp["success"] is True
    assert resp["device"]["device_id"] == "dev-1"
    assert resp["queried_by"] == "entity_id"
    assert client.device_list_calls == 0
    assert len(_device_get_calls(ws)) == 1


@pytest.mark.asyncio
async def test_missing_device_via_component_raises_not_found() -> None:
    """A component-reported missing device falls back to the LEGACY device list for
    the not-found error's suggestions (device_get authoritatively said None)."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": None},
    )
    client = RoutingClient(devices=[_raw_device("other")])
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices), pytest.raises(ToolError) as excinfo:
        await get_device(device_id="ghost")

    assert "Device not found" in str(excinfo.value) or "RESOURCE_NOT_FOUND" in str(
        excinfo.value
    )
    # The legacy device list served the not-found suggestion.
    assert client.device_list_calls == 1


@pytest.mark.asyncio
async def test_summary_list_uses_device_list_and_skips_entities() -> None:
    """A summary list read is served by device_list and skips the entity registry
    entirely (nothing in a summary list needs it)."""
    ws = make_ws(
        "ha_mcp_tools/device_list",
        info_result=_CAPS_DEVICES,
        cmd_result={"devices": [_raw_device("d1"), _raw_device("d2")]},
    )
    client = RoutingClient()
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device()

    assert resp["success"] is True
    assert resp["total_devices"] == 2
    assert client.device_list_calls == 0
    assert client.entity_list_calls == 0


@pytest.mark.asyncio
async def test_capsless_component_uses_legacy_registries() -> None:
    """Old component (info unknown_command) → legacy device + entity registry."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(
        devices=[_raw_device("dev-1")],
        entities=[_entity_row("sensor.x", "dev-1")],
    )
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(device_id="dev-1")

    assert resp["device"]["device_id"] == "dev-1"
    # Both registries came from the legacy WS path.
    assert client.device_list_calls == 1
    assert not _device_get_calls(ws)


@pytest.mark.asyncio
async def test_unknown_command_falls_back_and_invalidates_caps() -> None:
    """unknown_command on device_get → invalidate caps + legacy full list."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(
        devices=[_raw_device("dev-1")],
        entities=[_entity_row("sensor.x", "dev-1")],
    )
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(device_id="dev-1")

    assert resp["device"]["device_id"] == "dev-1"
    # The component command was tried, failed unknown_command → legacy full list.
    assert client.device_list_calls == 1
    # Caps were invalidated (dropped from the cache) so the next call re-probes.
    assert client not in component_api._CAPS_CACHE
