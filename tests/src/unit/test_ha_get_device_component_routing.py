"""Routing tests for ``ha_get_device`` over the ``ha_mcp_tools`` component gate.

``ha_get_device`` pulled the ENTIRE device registry AND the entire entity registry
for a single lookup. When the component advertises ``device_get`` / ``device_list``,
a single lookup fetches just the target device and its entities in ONE
``ha_mcp_tools/device_get(include_entities=True)`` frame, an ``entity_id`` lookup
resolves the device via a single native ``config/entity_registry/get``, and a list
read comes from ``ha_mcp_tools/device_list`` — none of these dumps a whole
registry. Full-detail LIST mode is the only path that still reads the whole entity
registry (one dump beats N per-device joins). These tests pin that: the
component-served single lookup never sends ``config/device_registry/list`` NOR
``config/entity_registry/list``, list mode uses ``device_list``, a summary list
skips the entity fetch, and every backend degradation (no caps, ``unknown_command``
→ invalidate + fall back, an additive-param-blind component that omits the entities
half) still returns the byte-identical legacy result.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, component_devices
from ha_mcp.tools.tools_registry import (
    _resolve_device_id_for_entity,
    register_registry_tools,
)

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

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
        self.entity_get_calls = 0

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
        if msg_type == "config/entity_registry/get":
            # Single native entity read (entity_id -> device_id); no dump.
            self.entity_get_calls += 1
            row = next(
                (
                    e
                    for e in self._entities
                    if e.get("entity_id") == msg.get("entity_id")
                ),
                None,
            )
            if row is None:
                return {"success": False, "error": "not found"}
            return {"success": True, "result": row}
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
    """A device_id lookup fetches the device AND its entities in one
    device_get(include_entities) frame; NEITHER registry is dumped."""
    dev = _raw_device("dev-1", name_by_user="Kitchen")
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={
            "device": dev,
            "entities": [_entity_row("sensor.kitchen", "dev-1")],
        },
    )
    client = RoutingClient()
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
    # include_entities was requested so the entities ride the same frame.
    assert _device_get_calls(ws)[0].kwargs["include_entities"] is True
    # The device's entity list came from the join — the whole entity registry
    # was never dumped.
    assert resp["entity_count"] == 1
    assert resp["entities"][0]["entity_id"] == "sensor.kitchen"
    assert client.entity_list_calls == 0


@pytest.mark.asyncio
async def test_entity_id_lookup_resolves_then_device_get() -> None:
    """entity_id mode resolves the device via a single native
    config/entity_registry/get, then reads the device (and its entities) via
    device_get — no whole device- OR entity-registry dump."""
    dev = _raw_device("dev-1")
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": dev, "entities": [_entity_row("light.lr", "dev-1")]},
    )
    client = RoutingClient(entities=[_entity_row("light.lr", "dev-1")])
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(entity_id="light.lr")

    assert resp["success"] is True
    assert resp["device"]["device_id"] == "dev-1"
    assert resp["queried_by"] == "entity_id"
    # Device resolved via one targeted entity read; no registry was dumped.
    assert client.entity_get_calls == 1
    assert client.device_list_calls == 0
    assert client.entity_list_calls == 0
    assert len(_device_get_calls(ws)) == 1


@pytest.mark.asyncio
async def test_missing_device_via_component_raises_not_found() -> None:
    """A component-reported missing device falls back to the LEGACY device list for
    the not-found error's suggestions (device_get authoritatively said None)."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": None, "entities": []},
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
async def test_entities_half_absent_falls_back_to_legacy() -> None:
    """A device_get-capable component that omits the additive entities half (an
    older build predating include_entities) degrades the WHOLE single lookup to
    the legacy registries rather than reporting a device with zero entities."""
    dev = _raw_device("dev-1")
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"device": dev},  # no entities key
    )
    client = RoutingClient(
        devices=[_raw_device("dev-1")],
        entities=[_entity_row("sensor.x", "dev-1")],
    )
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(device_id="dev-1")

    assert resp["device"]["device_id"] == "dev-1"
    # Both registries were read from the legacy path so the entity list survives.
    assert client.device_list_calls == 1
    assert client.entity_list_calls == 1
    assert resp["entity_count"] == 1


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


class _ResolveStubClient:
    """Minimal client for ``_resolve_device_id_for_entity``: a controllable
    ``config/entity_registry/get`` reply plus a legacy entity-registry list."""

    def __init__(
        self,
        get_response: dict[str, Any],
        entities: list[dict[str, Any]] | None = None,
        list_response: dict[str, Any] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._get_response = get_response
        self._entities = list(entities or [])
        # When set, the legacy list read returns this instead of a success dump —
        # lets a test simulate the fallback list ALSO failing.
        self._list_response = list_response
        self.entity_list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/entity_registry/get":
            return self._get_response
        if msg_type == "config/entity_registry/list":
            self.entity_list_calls += 1
            if self._list_response is not None:
                return self._list_response
            return {"success": True, "result": list(self._entities)}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


@pytest.mark.asyncio
async def test_resolve_success_returns_device_id_no_dump() -> None:
    """A successful targeted read returns the device_id without a registry dump."""
    client = _ResolveStubClient({"success": True, "result": {"device_id": "dev-9"}})
    assert await _resolve_device_id_for_entity(client, "light.x") == "dev-9"
    assert client.entity_list_calls == 0


@pytest.mark.asyncio
async def test_resolve_not_found_code_raises_without_legacy_dump() -> None:
    """HA's authoritative ``not_found`` code raises ENTITY_NOT_FOUND directly — no
    wasteful whole-registry fallback (issue #1813 F3)."""
    client = _ResolveStubClient(
        {"success": False, "error_code": "not_found", "error": "Entity not found"}
    )
    with pytest.raises(ToolError) as excinfo:
        await _resolve_device_id_for_entity(client, "sensor.ghost")
    assert "sensor.ghost" in str(excinfo.value)
    assert client.entity_list_calls == 0


@pytest.mark.asyncio
async def test_resolve_transient_failure_falls_back_to_legacy() -> None:
    """A failure WITHOUT the ``not_found`` code is a transient WS hiccup, not HA's
    'unknown entity' verdict: fall back to the legacy whole-registry read so a real
    entity is not misreported as nonexistent (issue #1813 F3)."""
    client = _ResolveStubClient(
        {"success": False, "error": "WebSocket request failed"},
        entities=[{"entity_id": "light.lr", "device_id": "dev-1"}],
    )
    assert await _resolve_device_id_for_entity(client, "light.lr") == "dev-1"
    assert client.entity_list_calls == 1


@pytest.mark.asyncio
async def test_resolve_double_transient_failure_raises_service_error() -> None:
    """When BOTH the targeted read and the legacy fallback list fail transiently, a
    real entity must NOT be misreported as ENTITY_NOT_FOUND: the fallback uses a
    strict list read, so the second failure surfaces as SERVICE_CALL_FAILED (Codex
    P2)."""
    client = _ResolveStubClient(
        {"success": False, "error": "WebSocket request failed"},
        list_response={"success": False, "error": "registry unavailable"},
    )
    with pytest.raises(ToolError) as excinfo:
        await _resolve_device_id_for_entity(client, "light.lr")
    msg = str(excinfo.value)
    assert "SERVICE_CALL_FAILED" in msg
    assert "ENTITY_NOT_FOUND" not in msg
    # The fallback list was attempted (and its failure is what surfaced).
    assert client.entity_list_calls == 1


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


@pytest.mark.asyncio
async def test_non_unknown_error_falls_back_without_invalidating_caps() -> None:
    """A non-unknown device_get error (a command timeout) falls back to the legacy
    registries for the byte-identical result WITHOUT invalidating caps — the
    capability is still advertised, only this one frame failed (issue #1813 T1)."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient(
        devices=[_raw_device("dev-1")],
        entities=[_entity_row("sensor.x", "dev-1")],
    )
    get_device = _build_get_device(client)

    with patch_ws(ws, component_devices):
        resp = await get_device(device_id="dev-1")

    # The legacy registries served the byte-identical device + entity list.
    assert resp["device"]["device_id"] == "dev-1"
    assert client.device_list_calls == 1
    assert client.entity_list_calls == 1
    assert resp["entity_count"] == 1
    # Caps stay cached: a transient failure is not a downgrade, so the next call
    # still routes through the component instead of re-probing.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_without_invalidating_caps() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) falls back to the legacy registries for the byte-identical result
    WITHOUT invalidating caps. The legacy device/entity reads ride the swallowing
    bridge, so they do not die identically on a pooled-WS drop."""
    caps_ws = make_ws("ha_mcp_tools/device_get", info_result=_CAPS_DEVICES)
    client = RoutingClient(
        devices=[_raw_device("dev-1")],
        entities=[_entity_row("sensor.x", "dev-1")],
    )
    get_device = _build_get_device(client)

    with patch_ws_establish_failure(
        caps_ws,
        component_devices,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await get_device(device_id="dev-1")

    assert resp["device"]["device_id"] == "dev-1"
    assert client.device_list_calls == 1
    assert client.entity_list_calls == 1
    assert resp["entity_count"] == 1
    assert client in component_api._CAPS_CACHE


# --- device seam error taxonomy (direct fetch_* calls; issue #1813 T2 / T4) ----
# The consumer routing tests above cover device_get through ``ha_get_device``;
# these pin the ``component_devices`` seam functions directly, mirroring the
# device_get taxonomy for ``device_list`` and the device_get shape guard.


@pytest.mark.asyncio
async def test_device_list_unknown_command_invalidates_caps() -> None:
    """unknown_command on device_list → invalidate caps, return None (→ legacy),
    mirroring device_get's downgrade branch (issue #1813 T2)."""
    ws = make_ws(
        "ha_mcp_tools/device_list",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        assert await component_devices.fetch_device_list_via_component(client) is None
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_device_list_non_unknown_error_keeps_caps() -> None:
    """A non-unknown device_list error (timeout) → None (→ legacy) WITHOUT
    invalidating the still-advertised capability (issue #1813 T2)."""
    ws = make_ws(
        "ha_mcp_tools/device_list",
        info_result=_CAPS_DEVICES,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        assert await component_devices.fetch_device_list_via_component(client) is None
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_device_list_malformed_shape_falls_back() -> None:
    """A device_list reply whose ``devices`` is not a list (shape drift) → None so
    the caller reads the legacy registry instead of trusting it (issue #1813 T2)."""
    ws = make_ws(
        "ha_mcp_tools/device_list",
        info_result=_CAPS_DEVICES,
        cmd_result={"devices": "not-a-list"},
    )
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        assert await component_devices.fetch_device_list_via_component(client) is None


@pytest.mark.asyncio
async def test_device_get_missing_device_key_falls_back() -> None:
    """A device_get reply with NO ``device`` key (shape drift) → None, so the single
    lookup degrades to the legacy registries rather than trusting the payload. This
    is distinct from ``{"device": None}``, the component's authoritative 'no such
    device' verdict (issue #1813 T4)."""
    ws = make_ws(
        "ha_mcp_tools/device_get",
        info_result=_CAPS_DEVICES,
        cmd_result={"entities": []},  # "device" key absent
    )
    client = RoutingClient()

    with patch_ws(ws, component_devices):
        assert (
            await component_devices.fetch_device_via_component(client, "dev-1") is None
        )
