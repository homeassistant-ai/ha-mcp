"""Cross-seam contract test for the ``ha_mcp_tools`` ``registries`` capability.

Wires the REAL component ``_do_registries`` (driven against ``FakeHass`` /
``make_view`` fixtures from ``test_component_ws_search.py``) underneath a
mocked WS transport and invokes the REAL auto-backup capture fetchers
``_fetch_label`` / ``_fetch_category`` / ``_fetch_area_or_floor``
(``backup_manager.py``) — so a vocabulary or shape drift on either side of the
seam fails here rather than shipping a component-served response the consumer
mis-shapes. Full-field row parity is the key assertion: the label, category and
floor fields must survive the round trip unchanged (dropping them is the
audit's explicit anti-goal, CONSUMER-MAP §10). The area and floor listing is
tested against real Core in ``test_area_floor_tools_on_core.py``.

Kept as its own file rather than appended to ``test_component_readapi_contract.py``
— several Phase 2 tasks touch that file's shared ``_REAL_FNS``/import block
concurrently, and it is off-limits for this task.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager
from ha_mcp.tools import component_registries

from ._component_routing_helpers import patch_ws
from .test_component_ws_search import (
    FAKE_TIME,
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
    """One REAL ``_do_registries`` output drives the auto-backup
    label/category/floor capture fetchers through their real server code."""

    def _floor(self) -> FakeFloor:
        return FakeFloor(
            "f1", "Ground", level=0, icon="mdi:floor-plan", aliases={"downstairs"}
        )

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
            "created_at": FAKE_TIME.timestamp(),
            "description": "fav",
            "icon": "mdi:star",
            "label_id": "lb1",
            "name": "Favorites",
            "modified_at": FAKE_TIME.timestamp(),
        }

    @pytest.mark.asyncio
    async def test_category_capture_full_field_parity_from_real_component(
        self, monkeypatch
    ) -> None:
        cat = FakeCategory("cat1", "Lights", icon="mdi:lightbulb")
        monkeypatch.setattr(wsapi, "_resolve_registries", lambda h: make_view())
        monkeypatch.setattr(
            wsapi,
            "_category_registry",
            lambda h: FakeCategoryReg({"automation": [cat]}),
        )
        client = _AreaFloorClient()
        ws = _real_registries_ws(FakeHass())

        with patch_ws(ws, component_registries):
            result = await backup_manager._fetch_category(client, "automation:cat1")

        assert client.legacy_calls == 0
        assert result == {
            "scope": "automation",
            "category_id": "cat1",
            "created_at": FAKE_TIME.timestamp(),
            "icon": "mdi:lightbulb",
            "modified_at": FAKE_TIME.timestamp(),
            "name": "Lights",
        }

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
