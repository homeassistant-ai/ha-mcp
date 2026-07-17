"""Routing tests for ``_resolve_ieee`` over the ``ha_mcp_tools`` component gate.

``ha_manage_radio``'s ZHA actions resolve a ``device_id`` to its IEEE address, and
every one of those 7 call sites went through ``_resolve_ieee``, which dumped the
ENTIRE device registry to read one device's identifiers. When the component
advertises ``device_get`` that becomes a single in-process read. These tests pin
the shared ``_resolve_ieee`` seam: component-hit (no whole-registry dump), legacy
fallback on no-caps, and ``unknown_command`` → invalidate caps + legacy list.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api, component_devices
from ha_mcp.tools.radio.zigbee import _resolve_ieee

from ._component_routing_helpers import make_ws, patch_ws

_IEEE = "00:11:22:33:44:55:66:77"

_CAPS_DEVICES = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["device_get", "device_list"],
    "limits": {},
}


def _zha_device(device_id: str = "dev-1") -> dict[str, Any]:
    return {
        "id": device_id,
        "name": "Zigbee Sensor",
        "identifiers": [["zha", _IEEE]],
        "connections": [],
    }


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy device-registry-list dumps."""

    def __init__(self, devices: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._devices = list(devices or [])
        self.device_list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/device_registry/list":
            self.device_list_calls += 1
            return {"success": True, "result": list(self._devices)}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


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
async def test_resolve_ieee_served_by_component() -> None:
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": _zha_device()},
    )
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        ieee = await _resolve_ieee(client, "dev-1")

    assert ieee == _IEEE
    assert client.device_list_calls == 0
    assert len(_device_get_calls(ws)) == 1
    assert _device_get_calls(ws)[0].kwargs["device_id"] == "dev-1"


@pytest.mark.asyncio
async def test_resolve_ieee_capsless_uses_legacy_list() -> None:
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(devices=[_zha_device()])

    with patch_ws(ws, component_devices):
        ieee = await _resolve_ieee(client, "dev-1")

    assert ieee == _IEEE
    assert client.device_list_calls == 1
    assert not _device_get_calls(ws)


@pytest.mark.asyncio
async def test_resolve_ieee_unknown_command_invalidates_and_falls_back() -> None:
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(devices=[_zha_device()])

    with patch_ws(ws, component_devices):
        ieee = await _resolve_ieee(client, "dev-1")

    assert ieee == _IEEE
    assert client.device_list_calls == 1
    assert client not in component_api._CAPS_CACHE
