"""Routing tests for ``ha_list_floors_areas`` over the ``ha_mcp_tools`` component gate.

``ha_list_floors_areas`` fetched the area AND floor registries via two
CONCURRENT but independent WebSocket list calls — a TOCTOU window where a
registry change between the two reads could misclassify an area as
orphaned/unassigned. When the component advertises ``registries``, both
registries come from ONE in-process ``ha_mcp_tools/registries`` read (a single
consistent snapshot), and neither legacy list call is sent. These tests pin
that: the component-served path skips both legacy calls, and every backend
degradation still falls back to the byte-identical legacy 2-call gather — no
caps, ``unknown_command`` → invalidate + fall back, a non-unknown command
error, AND a transport failure (both a ``HomeAssistantConnectionError`` off the
frame and a plain ``Exception`` from ``get_websocket_client()`` failing to
establish the socket). The registries legacy path does NOT die identically on a
pooled-WS drop (``ha_list_floors_areas`` rides the swallowing
``send_websocket_message`` bridge; the auto-backup capture fetchers use a
dedicated one-shot socket under a best-effort warn-and-skip contract), so a
transport failure falls back rather than propagating.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, component_registries
from ha_mcp.tools.tools_areas import register_area_tools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

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
async def test_connection_error_falls_back_to_legacy() -> None:
    """A WS connection error on the component read falls back to the legacy 2-call
    gather rather than propagating — ``ha_list_floors_areas``' legacy path rides the
    swallowing bridge, so it does not die identically on a pooled-WS drop."""
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

    with patch_ws(ws, component_registries):
        resp = await list_floors_areas()

    assert resp["success"] is True
    assert client.area_list_calls == 1
    assert client.floor_list_calls == 1
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) falls back to the legacy 2-call gather."""
    caps_ws = make_ws("ha_mcp_tools/registries", info_result=_CAPS_REGISTRIES)
    client = RoutingClient(
        areas=[_raw_area("a1", "Office", floor_id="f1")],
        floors=[_raw_floor("f1", "Ground")],
    )
    list_floors_areas = _build_list_floors_areas(client)

    with patch_ws_establish_failure(
        caps_ws,
        component_registries,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await list_floors_areas()

    assert resp["success"] is True
    assert client.area_list_calls == 1
    assert client.floor_list_calls == 1
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_malformed_areas_shape_falls_back_to_legacy() -> None:
    """A component response whose ``areas`` slice isn't a list (a malformed
    dump — e.g. a version-mismatched or buggy component) must not be
    trusted: ``fetch_registries_via_component`` returns ``None`` and
    ``ha_list_floors_areas`` falls back to the legacy 2-call gather instead
    of crashing on the bad shape or silently misreporting counts."""
    ws = make_ws(
        "ha_mcp_tools/registries",
        info_result=_CAPS_REGISTRIES,
        cmd_result={"areas": "not-a-list", "floors": []},
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


@pytest.mark.parametrize(
    "cmd_result",
    [
        pytest.param({"areas": "not-a-list", "floors": []}, id="area-not-list"),
        pytest.param({"areas": [], "floors": {"oops": 1}}, id="floor-not-list"),
        pytest.param({"floors": []}, id="area-key-missing"),
    ],
)
@pytest.mark.asyncio
async def test_area_floor_shape_mismatch_returns_none(
    cmd_result: dict[str, Any],
) -> None:
    """Direct unit coverage of ``fetch_registries_via_component``'s shape
    guard for the ``area``/``floor`` slices: a non-list value, or a
    requested slice missing outright, both return ``None`` (never raise)."""
    ws = make_ws(
        "ha_mcp_tools/registries", info_result=_CAPS_REGISTRIES, cmd_result=cmd_result
    )
    client = RoutingClient()

    with patch_ws(ws, component_registries):
        result = await component_registries.fetch_registries_via_component(
            client, ["area", "floor"]
        )

    assert result is None


@pytest.mark.parametrize(
    "cmd_result",
    [
        pytest.param({"categories": "not-a-dict"}, id="categories-not-dict"),
        pytest.param({"categories": {"automation": "not-a-list"}}, id="scope-not-list"),
        pytest.param({"categories": {}}, id="scope-missing"),
    ],
)
@pytest.mark.asyncio
async def test_category_shape_mismatch_returns_none(cmd_result: dict[str, Any]) -> None:
    """Same shape guard for the scoped ``categories`` mapping: the outer
    value must be a dict AND the requested scope's value must be a list."""
    ws = make_ws(
        "ha_mcp_tools/registries", info_result=_CAPS_REGISTRIES, cmd_result=cmd_result
    )
    client = RoutingClient()

    with patch_ws(ws, component_registries):
        result = await component_registries.fetch_registries_via_component(
            client, ["category"], category_scopes=["automation"]
        )

    assert result is None


class _CredentialedClient:
    """Bare credentialed double: enough for ``get_component_caps`` to probe
    (truthy ``base_url``/``token``). No ``send_websocket_message`` — the
    legacy path for the auto-backup fetchers below routes through
    ``backup_manager._ws_send``, monkeypatched separately per test."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False


async def _call_fetch_label(client: Any) -> Any:
    return await bm._fetch_label(client, "lb1")


async def _call_fetch_category(client: Any) -> Any:
    return await bm._fetch_category(client, "automation:cat1")


async def _call_fetch_area(client: Any) -> Any:
    return await bm._fetch_area_or_floor(client, "area:a1")


async def _call_fetch_floor(client: Any) -> Any:
    return await bm._fetch_area_or_floor(client, "floor:f1")


_LEGACY_LABEL_ITEM = {"label_id": "lb1", "name": "Favorites"}
_LEGACY_CATEGORY_ITEM = {"category_id": "cat1", "name": "Lights"}
_LEGACY_AREA_ITEM = _raw_area("a1", "Office")
_LEGACY_FLOOR_ITEM = _raw_floor("f1", "Ground")

_BACKUP_FETCH_CASES = [
    pytest.param(
        _call_fetch_label,
        "config/label_registry/list",
        [_LEGACY_LABEL_ITEM],
        dict(_LEGACY_LABEL_ITEM),
        id="label",
    ),
    pytest.param(
        _call_fetch_category,
        "config/category_registry/list",
        [_LEGACY_CATEGORY_ITEM],
        {"scope": "automation", **_LEGACY_CATEGORY_ITEM},
        id="category",
    ),
    pytest.param(
        _call_fetch_area,
        "config/area_registry/list",
        [_LEGACY_AREA_ITEM],
        {"kind": "area", **_LEGACY_AREA_ITEM},
        id="area",
    ),
    pytest.param(
        _call_fetch_floor,
        "config/floor_registry/list",
        [_LEGACY_FLOOR_ITEM],
        {"kind": "floor", **_LEGACY_FLOOR_ITEM},
        id="floor",
    ),
]


class TestBackupCaptureLegacyFallback:
    """The auto-backup capture fetchers (``_fetch_label`` / ``_fetch_category``
    / ``_fetch_area_or_floor``) fall back to the legacy per-registry WS list
    call under both component-unavailable modes: caps absent (old component,
    ``info`` itself is ``unknown_command``) and a component command error
    (the ``registries`` frame times out despite caps being advertised).
    Neither degradation is allowed to surface as a missing entity — the
    legacy fetch must still run and return the matching row."""

    @pytest.mark.parametrize(
        "call_fetch, legacy_type, legacy_items, expected", _BACKUP_FETCH_CASES
    )
    @pytest.mark.asyncio
    async def test_caps_absent_falls_back_to_legacy(
        self,
        call_fetch: Any,
        legacy_type: str,
        legacy_items: list[dict[str, Any]],
        expected: dict[str, Any],
        monkeypatch: Any,
    ) -> None:
        ws = make_ws(
            "ha_mcp_tools/registries",
            info_exc=HomeAssistantCommandError("no info", "unknown_command"),
        )
        calls: list[str] = []

        async def fake_ws_send(_client: Any, message: dict[str, Any]) -> Any:
            calls.append(message["type"])
            return list(legacy_items)

        monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
        client = _CredentialedClient()

        with patch_ws(ws, component_registries):
            result = await call_fetch(client)

        assert calls == [legacy_type]
        assert result == expected

    @pytest.mark.parametrize(
        "call_fetch, legacy_type, legacy_items, expected", _BACKUP_FETCH_CASES
    )
    @pytest.mark.asyncio
    async def test_component_error_falls_back_to_legacy(
        self,
        call_fetch: Any,
        legacy_type: str,
        legacy_items: list[dict[str, Any]],
        expected: dict[str, Any],
        monkeypatch: Any,
    ) -> None:
        ws = make_ws(
            "ha_mcp_tools/registries",
            info_result=_CAPS_REGISTRIES,
            cmd_exc=HomeAssistantCommandTimeout("timeout"),
        )
        calls: list[str] = []

        async def fake_ws_send(_client: Any, message: dict[str, Any]) -> Any:
            calls.append(message["type"])
            return list(legacy_items)

        monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
        client = _CredentialedClient()

        with patch_ws(ws, component_registries):
            result = await call_fetch(client)

        assert calls == [legacy_type]
        assert result == expected

    @pytest.mark.parametrize(
        "call_fetch, legacy_type, legacy_items, expected", _BACKUP_FETCH_CASES
    )
    @pytest.mark.asyncio
    async def test_ws_establish_failure_falls_back_to_legacy(
        self,
        call_fetch: Any,
        legacy_type: str,
        legacy_items: list[dict[str, Any]],
        expected: dict[str, Any],
        monkeypatch: Any,
    ) -> None:
        """A plain establish ``Exception`` from ``get_websocket_client()`` after caps
        are cached (review-5 I-1) must NOT block the wrapped write: the capture
        fetcher catches it → legacy list, returning the matching row so the capture
        proceeds instead of the ``with_auto_backup`` decorator erroring out on a raw
        exception the transient tuple never covered."""
        caps_ws = make_ws("ha_mcp_tools/registries", info_result=_CAPS_REGISTRIES)
        calls: list[str] = []

        async def fake_ws_send(_client: Any, message: dict[str, Any]) -> Any:
            calls.append(message["type"])
            return list(legacy_items)

        monkeypatch.setattr(bm, "_ws_send", fake_ws_send)
        client = _CredentialedClient()

        with patch_ws_establish_failure(
            caps_ws,
            component_registries,
            Exception("Failed to connect to Home Assistant WebSocket"),
        ):
            result = await call_fetch(client)

        assert calls == [legacy_type]
        assert result == expected
