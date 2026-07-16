"""Routing tests for ``resolve_update_entity`` over the ``ha_mcp_tools`` gate.

``ha_manage_radio``'s firmware actions (zha / zwave_js / matter update installs)
resolve a device's ``update.*`` entity through ``resolve_update_entity``, which
dumped the ENTIRE entity registry (``config/entity_registry/list``) to filter one
device's rows. When the component advertises ``device_get`` those per-device rows
ride one ``device_get(include_entities=True)`` frame instead — the ``update.*`` /
platform filtering stays client-side. These tests pin that seam: component-hit (no
whole-registry dump), legacy fallback on no-caps, ``unknown_command`` → invalidate
caps + legacy list, and an additive-param-blind component (device served, entities
half omitted) degrading to the legacy list rather than reporting no update entity.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api, component_devices
from ha_mcp.tools.radio.base import resolve_update_entity

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_DEVICES = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["device_get", "device_list"],
    "limits": {},
}


def _entity_row(
    entity_id: str, device_id: str = "dev-1", platform: str = "zha"
) -> dict[str, Any]:
    """A ``config/entity_registry/list`` element (also a device_get entities row)."""
    return {
        "entity_id": entity_id,
        "device_id": device_id,
        "platform": platform,
        "name": None,
        "original_name": entity_id,
        "disabled_by": None,
    }


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy entity-registry-list dumps."""

    def __init__(self, entities: list[dict[str, Any]] | None = None) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._entities = list(entities or [])
        self.entity_list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "config/entity_registry/list":
            self.entity_list_calls += 1
            return {"success": True, "result": list(self._entities)}
        raise AssertionError(f"unexpected ws message {msg.get('type')!r}")


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
async def test_update_entity_served_by_component() -> None:
    """The device's rows ride one device_get(include_entities) frame; the whole
    entity registry is never dumped, and the update.* filter still runs."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={
            "device": {"id": "dev-1"},
            "entities": [
                _entity_row("sensor.temp"),
                _entity_row("update.dev_firmware"),
            ],
        },
    )
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        eid = await resolve_update_entity(client, "dev-1", platform="zha")

    assert eid == "update.dev_firmware"
    assert client.entity_list_calls == 0
    assert len(_device_get_calls(ws)) == 1
    assert _device_get_calls(ws)[0].kwargs["device_id"] == "dev-1"
    assert _device_get_calls(ws)[0].kwargs["include_entities"] is True


@pytest.mark.asyncio
async def test_update_entity_capsless_uses_legacy_list() -> None:
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(
        entities=[_entity_row("sensor.temp"), _entity_row("update.dev_firmware")]
    )

    with patch_ws(ws, component_devices):
        eid = await resolve_update_entity(client, "dev-1", platform="zha")

    assert eid == "update.dev_firmware"
    assert client.entity_list_calls == 1
    assert not _device_get_calls(ws)


@pytest.mark.asyncio
async def test_update_entity_unknown_command_invalidates_and_falls_back() -> None:
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(entities=[_entity_row("update.dev_firmware")])

    with patch_ws(ws, component_devices):
        eid = await resolve_update_entity(client, "dev-1", platform="zha")

    assert eid == "update.dev_firmware"
    assert client.entity_list_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_update_entity_entities_half_absent_falls_back_to_legacy() -> None:
    """A device_get-capable component that omits the additive entities half (an
    older build predating include_entities) degrades to the legacy entity list
    rather than reporting no update entity."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": {"id": "dev-1"}},  # no entities key
    )
    client = RoutingClient(entities=[_entity_row("update.dev_firmware")])

    with patch_ws(ws, component_devices):
        eid = await resolve_update_entity(client, "dev-1", platform="zha")

    assert eid == "update.dev_firmware"
    assert client.entity_list_calls == 1
