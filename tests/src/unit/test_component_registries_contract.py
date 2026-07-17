"""Cross-seam contract test for the ``ha_mcp_tools`` ``registries`` capability.

Wires the REAL component ``_do_registries`` (driven against ``FakeHass`` /
``make_view`` fixtures from ``test_component_ws_search.py``) underneath a
mocked WS transport and invokes the REAL server consumers —
``ha_list_floors_areas`` (via ``AreaTools``) and the auto-backup capture
fetchers ``_fetch_label`` / ``_fetch_category`` / ``_fetch_area_or_floor``
(``backup_manager.py``) — so a vocabulary or shape drift on either side of the
seam fails here rather than shipping a component-served response the consumer
mis-shapes. Full-field row parity is the key assertion: icon / aliases /
picture / labels must survive the round trip unchanged (dropping them is the
audit's explicit anti-goal, CONSUMER-MAP §10).

Kept as its own file rather than appended to ``test_component_readapi_contract.py``
— several Phase 2 tasks touch that file's shared ``_REAL_FNS``/import block
concurrently, and it is off-limits for this task.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp import backup_manager
from ha_mcp.tools import component_registries
from ha_mcp.tools.tools_areas import register_area_tools

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FakeArea,
    FakeCategory,
    FakeCategoryReg,
    FakeFloor,
    FakeHass,
    FakeLabel,
    make_view,
    wsapi,
)

_REAL_FNS = {
    "ha_mcp_tools/registries": wsapi._do_registries,
}


def _real_registries_ws(hass: FakeHass) -> AsyncMock:
    """A WS mock whose ``info``/``registries`` commands are served by the REAL
    component functions against ``hass``. Mirrors
    ``test_component_readapi_contract._real_component_ws``, scoped to just the
    ``registries`` seam (kept local rather than imported — that file is
    off-limits and its ``_REAL_FNS``/``_PREP_FNS`` dicts don't cover this
    command)."""
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        fn = _REAL_FNS[command_type]
        return {"success": True, "result": fn(hass, dict(kwargs))}

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


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


class _AreaFloorClient:
    """Credentialed client whose legacy WS path must never fire once the
    component serves the snapshot — any call raises loudly."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.verify_ssl = False
        self.legacy_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        self.legacy_calls += 1
        raise AssertionError(f"legacy WS call should not fire: {msg}")


class TestRegistriesSeam:
    """One REAL ``_do_registries`` output drives ``ha_list_floors_areas`` and
    the auto-backup label/category/area/floor capture fetchers through their
    real server code."""

    def _area(self) -> FakeArea:
        return FakeArea(
            "a1",
            "Office",
            floor_id="f1",
            aliases={"studio"},
            icon="mdi:office",
            picture="/local/office.png",
            labels={"important"},
            humidity_entity_id="sensor.office_humidity",
            temperature_entity_id="sensor.office_temp",
        )

    def _unassigned_area(self) -> FakeArea:
        return FakeArea("a2", "Garage")

    def _floor(self) -> FakeFloor:
        return FakeFloor(
            "f1", "Ground", level=0, icon="mdi:floor-plan", aliases={"downstairs"}
        )

    @pytest.mark.asyncio
    async def test_list_floors_areas_full_field_parity_from_real_component(
        self, monkeypatch
    ) -> None:
        area = self._area()
        unassigned = self._unassigned_area()
        floor = self._floor()
        monkeypatch.setattr(
            wsapi,
            "_resolve_registries",
            lambda h: make_view(areas=[area, unassigned], floors=[floor]),
        )
        client = _AreaFloorClient()
        ws = _real_registries_ws(FakeHass())
        list_floors_areas = _build_list_floors_areas(client)

        with patch_ws(ws, component_registries):
            resp = await list_floors_areas()

        assert resp["success"] is True
        assert client.legacy_calls == 0
        assert resp["floor_count"] == 1
        assert resp["area_count"] == 2
        assert resp["unassigned_count"] == 1
        assert resp["orphaned_count"] == 0

        nested = resp["floors"][0]["areas"][0]
        # Full-field row parity — icon/aliases/picture/labels must survive.
        assert nested["area_id"] == "a1"
        assert nested["icon"] == "mdi:office"
        assert nested["aliases"] == ["studio"]
        assert nested["picture"] == "/local/office.png"
        assert nested["labels"] == ["important"]
        assert nested["humidity_entity_id"] == "sensor.office_humidity"
        assert nested["temperature_entity_id"] == "sensor.office_temp"

        floor_row = resp["floors"][0]
        assert floor_row["icon"] == "mdi:floor-plan"
        assert floor_row["aliases"] == ["downstairs"]
        assert floor_row["level"] == 0
        assert "labels" not in floor_row  # core's FloorEntry carries none

        assert resp["unassigned_areas"][0]["area_id"] == "a2"

        # Pin against the RAW component output directly too, so a drift in
        # either the row shape or the consumer's pass-through fails here.
        raw = wsapi._do_registries(FakeHass(), {"registries": ["area", "floor"]})
        assert nested == next(a for a in raw["areas"] if a["area_id"] == "a1")

    @pytest.mark.asyncio
    async def test_label_capture_full_field_parity_from_real_component(
        self, monkeypatch
    ) -> None:
        label = FakeLabel(
            "lb1", "Favorites", color="red", description="fav", icon="mdi:star"
        )
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(labels=[label])
        )
        client = _AreaFloorClient()
        ws = _real_registries_ws(FakeHass())

        with patch_ws(ws, component_registries):
            result = await backup_manager._fetch_label(client, "lb1")

        assert client.legacy_calls == 0
        assert result == {
            "color": "red",
            "created_at": None,
            "description": "fav",
            "icon": "mdi:star",
            "label_id": "lb1",
            "name": "Favorites",
            "modified_at": None,
        }

    @pytest.mark.asyncio
    async def test_category_capture_full_field_parity_from_real_component(
        self, monkeypatch
    ) -> None:
        cat = FakeCategory("cat1", "Lights", icon="mdi:lightbulb")
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: make_view())
        monkeypatch.setattr(
            wsapi, "_category_registry", lambda h: FakeCategoryReg({"automation": [cat]})
        )
        client = _AreaFloorClient()
        ws = _real_registries_ws(FakeHass())

        with patch_ws(ws, component_registries):
            result = await backup_manager._fetch_category(client, "automation:cat1")

        assert client.legacy_calls == 0
        assert result == {
            "scope": "automation",
            "category_id": "cat1",
            "created_at": None,
            "icon": "mdi:lightbulb",
            "modified_at": None,
            "name": "Lights",
        }

    @pytest.mark.asyncio
    async def test_area_capture_full_field_parity_from_real_component(
        self, monkeypatch
    ) -> None:
        area = self._area()
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(areas=[area])
        )
        client = _AreaFloorClient()
        ws = _real_registries_ws(FakeHass())

        with patch_ws(ws, component_registries):
            result = await backup_manager._fetch_area_or_floor(client, "area:a1")

        assert client.legacy_calls == 0
        assert result["kind"] == "area"
        assert result["icon"] == "mdi:office"
        assert result["aliases"] == ["studio"]
        assert result["picture"] == "/local/office.png"
        assert result["labels"] == ["important"]

    @pytest.mark.asyncio
    async def test_floor_capture_full_field_parity_from_real_component(
        self, monkeypatch
    ) -> None:
        floor = self._floor()
        monkeypatch.setattr(
            wsapi, "_resolve_registries", lambda h: make_view(floors=[floor])
        )
        client = _AreaFloorClient()
        ws = _real_registries_ws(FakeHass())

        with patch_ws(ws, component_registries):
            result = await backup_manager._fetch_area_or_floor(client, "floor:f1")

        assert client.legacy_calls == 0
        assert result["kind"] == "floor"
        assert result["icon"] == "mdi:floor-plan"
        assert result["aliases"] == ["downstairs"]
        assert "labels" not in result
