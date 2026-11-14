"""Component location resolution and complete result windows for ha_search.

Areas, floors, devices and entities are Core's own; the component's search
runs through Core's WebSocket API (``component_ws``) and is compared with the
server's legacy ``ha_search`` on the same ``hass``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.typing import MockHAClientWebSocket

from custom_components.ha_mcp_tools import websocket_api
from custom_components.ha_mcp_tools.search_locations import (
    SearchLocation,
    add_location_metadata,
)
from custom_components.ha_mcp_tools.websocket_api.constants import WS_SEARCH
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import invalidate_caps

from ._component_core import ws_result
from ._core_setup import add_test_entry
from ._search_tool import _build_ha_search, _setup_visibility_disabled


@dataclass
class _Home:
    ground: fr.FloorEntry
    upper: fr.FloorEntry
    bedroom: ar.AreaEntry


def _light_in(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    name: str,
    area: str,
    state: str,
) -> None:
    entry = entity_registry.async_get_or_create(
        "light", "test", name, suggested_object_id=name
    )
    entity_registry.async_update_entity(entry.entity_id, area_id=area)
    hass.states.async_set(entry.entity_id, state)


@pytest.fixture
def home(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> _Home:
    """Kitchen (alias Cooking) and Study on the ground floor (alias
    Downstairs), Bedroom upstairs; a light in each, the study's off."""
    ground = floor_registry.async_create("Ground floor", aliases={"Downstairs"})
    upper = floor_registry.async_create("Upper floor")
    kitchen = area_registry.async_create(
        "Kitchen", floor_id=ground.floor_id, aliases={"Cooking"}
    )
    study = area_registry.async_create("Study", floor_id=ground.floor_id)
    bedroom = area_registry.async_create("Bedroom", floor_id=upper.floor_id)
    _light_in(hass, entity_registry, "kitchen", kitchen.id, "on")
    _light_in(hass, entity_registry, "study", study.id, "off")
    _light_in(hass, entity_registry, "bedroom", bedroom.id, "on")
    return _Home(ground, upper, bedroom)


async def _search(ws: MockHAClientWebSocket, area_filter: str, **params: Any) -> Any:
    return await ws_result(
        ws, WS_SEARCH, search_types=["entity"], area_filter=area_filter, **params
    )


def _ids(result: dict[str, Any]) -> list[str]:
    return [item["entity_id"] for item in result.get("entities", [])]


@pytest.mark.parametrize(
    "location", ["<floor id>", "Ground floor", "DOWNSTAIRS", "Ground flor"]
)
@pytest.mark.parametrize("query", [None, "light"])
@pytest.mark.parametrize("exact", [True, False])
async def test_floor_expansion_before_state_and_pagination(
    component_ws: MockHAClientWebSocket,
    home: _Home,
    location: str,
    query: str | None,
    exact: bool,
) -> None:
    area_filter = home.ground.floor_id if location == "<floor id>" else location

    result = await _search(
        component_ws, area_filter, state_filter="ON", limit=1, query=query, exact=exact
    )

    assert _ids(result) == ["light.kitchen"]
    assert result["entity_total_matches"] == 1
    assert result["entity_has_more"] is False
    assert result["area_names"] == ["Kitchen", "Study"]
    assert "expanded" in " ".join(result["warnings"])


@pytest.mark.parametrize("location", [" Cooking ", "Kitchenn"])
async def test_area_alias_and_close_spelling(
    component_ws: MockHAClientWebSocket, home: _Home, location: str
) -> None:
    result = await _search(component_ws, location)

    assert _ids(result) == ["light.kitchen"]
    assert result["area_names"] == ["Kitchen"]


async def test_an_exact_area_wins_over_a_floor_alias(
    component_ws: MockHAClientWebSocket,
    home: _Home,
    floor_registry: fr.FloorRegistry,
) -> None:
    floor_registry.async_update(home.ground.floor_id, aliases={"Downstairs", "Kitchen"})

    result = await _search(component_ws, "Kitchen")

    assert result["entity_total_matches"] == 1
    assert "both an area and a floor" in " ".join(result["warnings"])


async def test_a_floor_search_finds_a_child_device_by_its_parents_area(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
) -> None:
    ground = floor_registry.async_create("Ground floor")
    study = area_registry.async_create("Study", floor_id=ground.floor_id)
    entry = add_test_entry(hass)
    parent = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "parent")}
    )
    device_registry.async_update_device(parent.id, area_id=study.id)
    child = device_registry.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )
    entity_registry.async_get_or_create(
        "light", "test", "child", suggested_object_id="child", device_id=child.id
    )
    hass.states.async_set("light.child", "on")

    result = await _search(component_ws, "Ground floor")

    assert result["entity_total_matches"] == 1
    assert result["entities"][0]["area"] == "Study"


async def test_an_ambiguous_close_floor_selects_none(
    component_ws: MockHAClientWebSocket,
    home: _Home,
    floor_registry: fr.FloorRegistry,
) -> None:
    floor_registry.async_update(home.upper.floor_id, name="Ground floar")

    result = await _search(component_ws, "Ground flor")

    assert result["entities"] == []
    assert "ambiguously matches multiple floors" in " ".join(result["warnings"])


async def test_a_failed_floor_enumeration_is_reported(
    component_ws: MockHAClientWebSocket,
    home: _Home,
    floor_registry: fr.FloorRegistry,
) -> None:
    with patch.object(
        floor_registry, "async_list_floors", side_effect=RuntimeError("unavailable")
    ):
        result = await _search(component_ws, "Downstairs")

    assert result["partial"] is True
    assert "floor" in result["partial_reason"]


@pytest.mark.parametrize(
    ("registry", "name"), [(ar, "area"), (fr, "floor"), (er, "entity"), (dr, "device")]
)
async def test_an_unavailable_location_registry_is_not_an_authoritative_empty(
    component_ws: MockHAClientWebSocket, home: _Home, registry: Any, name: str
) -> None:
    with patch.object(registry, "async_get", side_effect=RuntimeError("core drift")):
        result = await _search(component_ws, "Downstairs")

    assert result["partial"] is True
    assert name in result["partial_reason"]
    assert result["warnings"]


async def test_the_search_window_has_no_500_ceiling(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    for i in range(1003):
        hass.states.async_set(f"light.fixture_{i:04d}", "on")

    result = await ws_result(
        component_ws, WS_SEARCH, search_types=["entity"], limit=1001, offset=1
    )

    assert len(result["entities"]) == 1001
    assert result["entity_total_matches"] == 1003
    assert result["entity_has_more"] is True
    assert result["entities"][0]["entity_id"] == "light.fixture_0001"


_LEGACY_QUERIES = [
    "kitchen",
    "COOKING",
    " Downstairs ",
    "Ground flor",
    "Kitchenn",
    "Missing",
    "Study",
]


@pytest.mark.parametrize("floor_registry_available", [True, False])
async def test_location_resolution_matches_the_legacy_search(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    home: _Home,
    floor_registry: fr.FloorRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    floor_registry_available: bool,
) -> None:
    """Every query names the same lights with or without the component, the
    floor alias "Study" colliding with the area included."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    assert await async_setup_component(hass, "config", {})
    floor_registry.async_update(home.ground.floor_id, aliases={"Downstairs", "Study"})
    search = _build_ha_search(core_client)

    async def found(area_filter: str) -> list[str]:
        if floor_registry_available:
            return sorted(_ids(await search(area_filter=area_filter)))
        with patch.object(fr, "async_get", side_effect=RuntimeError("core drift")):
            return sorted(_ids(await search(area_filter=area_filter)))

    legacy = {query: await found(query) for query in _LEGACY_QUERIES}
    websocket_api.async_register_commands(hass)
    invalidate_caps(core_client)
    component = {query: await found(query) for query in _LEGACY_QUERIES}

    assert component == legacy


async def test_location_resolution_follows_live_registry_changes(
    component_ws: MockHAClientWebSocket,
    home: _Home,
    area_registry: ar.AreaRegistry,
) -> None:
    before = await _search(component_ws, "Downstairs")
    area_registry.async_update(home.bedroom.id, floor_id=home.ground.floor_id)
    after = await _search(component_ws, "Downstairs")

    assert (before["entity_total_matches"], after["entity_total_matches"]) == (2, 3)


def test_location_metadata_preserves_existing_warnings() -> None:
    location = SearchLocation(
        area_ids={"kitchen"},
        area_names=["Kitchen"],
        warnings=["Location registry incomplete"],
        unavailable=["floor"],
    )
    result = add_location_metadata(
        {
            "warnings": ["Automation scan incomplete"],
            "partial": True,
            "partial_reason": "Automation scan incomplete",
        },
        location,
    )

    assert result["warnings"] == [
        "Automation scan incomplete",
        "Location registry incomplete",
    ]
    assert "Automation scan incomplete" in result["partial_reason"]
    assert "floor" in result["partial_reason"]
