"""Routing tests for ``ha_list_floors_areas`` over the ``ha_mcp_tools`` component gate.

``ha_list_floors_areas`` fetched the area AND floor registries via two
CONCURRENT but independent WebSocket list calls — a TOCTOU window where a
registry change between the two reads could misclassify an area as
orphaned/unassigned. When the component advertises ``registries``, both
registries come from ONE in-process ``ha_mcp_tools/registries`` read (a single
consistent snapshot), and neither legacy list call is sent. These tests pin
that: the component-served path skips both legacy calls, every backend
degradation (no caps, ``unknown_command`` → invalidate + fall back, a
non-unknown command error) still falls back to the byte-identical legacy
2-call gather, and a WS connection error propagates rather than silently
falling back (the legacy path shares the same socket and would fail
identically).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, component_registries
from ha_mcp.tools.tools_areas import register_area_tools

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_REGISTRIES = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["registries"],
    "limits": {},
}


def _raw_area(area_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    """A ``config/area_registry/list`` element (also the component's area row)."""
    base: dict[str, Any] = {
        "aliases": [],
        "area_id": area_id,
        "floor_id": None,
        "humidity_entity_id": None,
        "icon": None,
        "labels": [],
        "name": name,
        "picture": None,
        "temperature_entity_id": None,
        "created_at": 1700000000.0,
        "modified_at": 1700000000.0,
    }
    base.update(overrides)
    return base


def _raw_floor(floor_id: str, name: str, **overrides: Any) -> dict[str, Any]:
    """A ``config/floor_registry/list`` element (also the component's floor row)."""
    base: dict[str, Any] = {
        "aliases": [],
        "created_at": 1700000000.0,
        "floor_id": floor_id,
        "icon": None,
        "level": 0,
        "name": name,
        "modified_at": 1700000000.0,
    }
    base.update(overrides)
    return base


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy registry-list fetches."""

    def __init__(
        self,
        areas: list[dict[str, Any]] | None = None,
        floors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self._areas = list(areas or [])
        self._floors = list(floors or [])
        self.area_list_calls = 0
        self.floor_list_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        if msg_type == "config/area_registry/list":
            self.area_list_calls += 1
            return {"success": True, "result": list(self._areas)}
        if msg_type == "config/floor_registry/list":
            self.floor_list_calls += 1
            return {"success": True, "result": list(self._floors)}
        raise AssertionError(f"unexpected ws message {msg_type!r}")


def _build_list_floors_areas(client: Any) -> Any:
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
    register_area_tools(mcp, client)
    return registered["ha_list_floors_areas"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _registries_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/registries"
    ]


@pytest.mark.asyncio
async def test_component_served_single_snapshot_skips_legacy_calls() -> None:
    """A single ``registries(registries=["area","floor"])`` frame serves both
    registries; NEITHER legacy list call is sent, and downstream partitioning
    (floor nesting / unassigned / orphaned) behaves identically over the
    component-shaped rows."""
    areas = [
        _raw_area("a1", "Office", floor_id="f1"),
        _raw_area("a2", "Garage"),  # unassigned: no floor_id
        _raw_area("a3", "Attic", floor_id="ghost"),  # orphaned: unknown floor_id
    ]
    floors = [_raw_floor("f1", "Ground")]
    ws = make_ws(
        "ha_mcp_tools/registries",
        info_result=_CAPS_REGISTRIES,
        cmd_result={"areas": areas, "floors": floors},
    )
    client = RoutingClient()
    list_floors_areas = _build_list_floors_areas(client)

    with patch_ws(ws, component_registries):
        resp = await list_floors_areas()

    assert resp["success"] is True
    assert client.area_list_calls == 0
    assert client.floor_list_calls == 0
    assert len(_registries_calls(ws)) == 1
    call = _registries_calls(ws)[0]
    assert set(call.kwargs["registries"]) == {"area", "floor"}

    assert resp["floor_count"] == 1
    assert resp["area_count"] == 3
    assert resp["unassigned_count"] == 1
    assert resp["orphaned_count"] == 1
    assert resp["floors"][0]["floor_id"] == "f1"
    assert [a["area_id"] for a in resp["floors"][0]["areas"]] == ["a1"]
    assert resp["unassigned_areas"][0]["area_id"] == "a2"
    assert resp["orphaned_areas"][0]["area_id"] == "a3"


@pytest.mark.asyncio
async def test_capsless_component_uses_legacy_two_call_gather() -> None:
    """Old component (info unknown_command) → legacy 2-call gather."""
    ws = make_ws(
        "ha_mcp_tools/registries",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient(
        areas=[_raw_area("a1", "Office", floor_id="f1")],
        floors=[_raw_floor("f1", "Ground")],
    )
    list_floors_areas = _build_list_floors_areas(client)

    with patch_ws(ws, component_registries):
        resp = await list_floors_areas()

    assert resp["success"] is True
    assert client.area_list_calls == 1
    assert client.floor_list_calls == 1
    assert not _registries_calls(ws)


@pytest.mark.asyncio
async def test_unknown_command_falls_back_and_invalidates_caps() -> None:
    """unknown_command on ``registries`` → invalidate caps + legacy 2-call gather."""
    ws = make_ws(
        "ha_mcp_tools/registries",
        info_result=_CAPS_REGISTRIES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(
        areas=[_raw_area("a1", "Office", floor_id="f1")],
        floors=[_raw_floor("f1", "Ground")],
    )
    list_floors_areas = _build_list_floors_areas(client)

    with patch_ws(ws, component_registries):
        resp = await list_floors_areas()

    assert resp["success"] is True
    assert client.area_list_calls == 1
    assert client.floor_list_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_non_unknown_error_falls_back_without_invalidating_caps() -> None:
    """A non-unknown ``registries`` error (a command timeout) falls back to the
    legacy 2-call gather for the byte-identical result WITHOUT invalidating
    caps — the capability is still advertised, only this one frame failed."""
    ws = make_ws(
        "ha_mcp_tools/registries",
        info_result=_CAPS_REGISTRIES,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient(
        areas=[_raw_area("a1", "Office", floor_id="f1")],
        floors=[_raw_floor("f1", "Ground")],
    )
    list_floors_areas = _build_list_floors_areas(client)

    with patch_ws(ws, component_registries):
        resp = await list_floors_areas()

    assert resp["success"] is True
    assert client.area_list_calls == 1
    assert client.floor_list_calls == 1
    # Caps stay cached: a transient failure is not a downgrade, so the next
    # call still routes through the component instead of re-probing.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_connection_error_propagates_without_legacy_fallback() -> None:
    """A WS connection error on the component read propagates (surfaced as a
    structured tool error) rather than silently falling back — the legacy path
    shares the same socket and would fail identically."""
    ws = make_ws(
        "ha_mcp_tools/registries",
        info_result=_CAPS_REGISTRIES,
        cmd_exc=HomeAssistantConnectionError("WebSocket not authenticated"),
    )
    client = RoutingClient(
        areas=[_raw_area("a1", "Office", floor_id="f1")],
        floors=[_raw_floor("f1", "Ground")],
    )
    list_floors_areas = _build_list_floors_areas(client)

    with patch_ws(ws, component_registries), pytest.raises(ToolError):
        await list_floors_areas()

    # The connection error propagated before either legacy list call fired.
    assert client.area_list_calls == 0
    assert client.floor_list_calls == 0
