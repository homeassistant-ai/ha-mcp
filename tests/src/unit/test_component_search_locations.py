"""Component location resolution and complete result windows for ha_search."""

from typing import Any

import pytest

from .test_component_ws_search import (
    _REAL_VOL,
    FakeArea,
    FakeChildDevice,
    FakeDevice,
    FakeFloor,
    FakeHass,
    FakeRegEntry,
    FakeState,
    make_view,
    wsapi,
)


@pytest.fixture
def location_search(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    view = make_view(
        entity={
            "light.kitchen": FakeRegEntry("light.kitchen", area_id="kitchen"),
            "light.study": FakeRegEntry("light.study", area_id="study"),
            "light.bedroom": FakeRegEntry("light.bedroom", area_id="bedroom"),
        },
        areas=[
            FakeArea("kitchen", "Kitchen", "ground", aliases=["Cooking"]),
            FakeArea("study", "Study", "ground"),
            FakeArea("bedroom", "Bedroom", "upper"),
        ],
        floors=[
            FakeFloor("ground", "Ground floor", aliases=["Downstairs"]),
            FakeFloor("upper", "Upper floor"),
        ],
    )
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    hass = FakeHass(
        states=[
            FakeState("light.kitchen", "on"),
            FakeState("light.study", "off"),
            FakeState("light.bedroom", "on"),
        ]
    )
    return hass, view


@pytest.mark.parametrize(
    "location", ["ground", "Ground floor", "DOWNSTAIRS", "Ground flor"]
)
@pytest.mark.parametrize("query", [None, "light"])
@pytest.mark.parametrize("exact", [True, False])
def test_floor_expansion_before_state_and_pagination(
    location_search: Any, location: str, query: str | None, exact: bool
) -> None:
    hass, _ = location_search
    result = wsapi._do_search(
        hass,
        {
            "search_types": ["entity"],
            "area_filter": location,
            "state_filter": "ON",
            "limit": 1,
            "query": query,
            "exact": exact,
        },
    )
    assert [item["entity_id"] for item in result["entities"]] == ["light.kitchen"]
    assert result["entity_total_matches"] == 1
    assert result["entity_has_more"] is False
    assert result["area_names"] == ["Kitchen", "Study"]
    assert "expanded" in " ".join(result["warnings"])


@pytest.mark.parametrize("location", [" Cooking ", "Kitchenn"])
def test_area_alias_and_close_spelling(location_search: Any, location: str) -> None:
    hass, _ = location_search
    result = wsapi._do_search(
        hass, {"area_filter": location, "search_types": ["entity"]}
    )
    assert [item["entity_id"] for item in result["entities"]] == ["light.kitchen"]
    assert result["area_names"] == ["Kitchen"]


def test_exact_area_wins_floor_collision(location_search: Any) -> None:
    hass, view = location_search
    view.floor._floors["ground"].aliases.add("Kitchen")
    result = wsapi._do_search(
        hass, {"area_filter": "Kitchen", "search_types": ["entity"]}
    )
    assert result["entity_total_matches"] == 1
    assert "both an area and a floor" in " ".join(result["warnings"])


def test_floor_search_inherits_child_device_area(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = make_view(
        entity={"light.child": FakeRegEntry("light.child", device_id="child")},
        areas=[FakeArea("study", "Study", "ground")],
        floors=[FakeFloor("ground", "Ground floor")],
        devices=[FakeDevice("parent", area_id="study")],
        child_devices=[FakeChildDevice("child", "parent")],
    )
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: view)
    result = wsapi._do_search(
        FakeHass(states=[FakeState("light.child", "on")]),
        {
            "search_types": ["entity"],
            "area_filter": "Ground floor",
        },
    )
    assert result["entity_total_matches"] == 1
    assert result["entities"][0]["area"] == "Study"


def test_ambiguous_close_floor_does_not_select_one(location_search: Any) -> None:
    hass, view = location_search
    view.floor._floors["upper"].name = "Ground floar"
    result = wsapi._do_search(
        hass, {"area_filter": "Ground flor", "search_types": ["entity"]}
    )
    assert result["entities"] == []
    assert "ambiguously matches multiple floors" in " ".join(result["warnings"])


def test_broken_registry_enumeration_is_reported(
    location_search: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    hass, view = location_search

    def broken() -> None:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(view.floor, "async_list_floors", broken)
    result = wsapi._do_search(
        hass, {"area_filter": "Downstairs", "search_types": ["entity"]}
    )
    assert result["partial"] is True
    assert "floor" in result["partial_reason"]


@pytest.mark.parametrize("registry", ["area", "floor", "entity", "device"])
def test_unavailable_location_registry_is_not_authoritative_empty(
    location_search: Any, registry: str
) -> None:
    hass, view = location_search
    setattr(view, registry, None)
    result = wsapi._do_search(
        hass, {"area_filter": "Downstairs", "search_types": ["entity"]}
    )
    assert result["partial"] is True
    assert registry in result["partial_reason"]
    assert result["warnings"]


def test_search_window_has_no_500_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    schema = _REAL_VOL.Schema(wsapi._search_schema())
    params = schema(
        {
            "type": wsapi.WS_SEARCH,
            "limit": 1001,
            "offset": 1,
            "search_types": ["entity"],
        }
    )
    monkeypatch.setattr(wsapi, "_resolve_registries", lambda hass: make_view())
    hass = FakeHass(
        states=[FakeState(f"light.fixture_{i:04d}", "on") for i in range(1003)]
    )
    result = wsapi._do_search(hass, params)
    assert len(result["entities"]) == 1001
    assert result["entity_total_matches"] == 1003
    assert result["entity_has_more"] is True
    assert result["entities"][0]["entity_id"] == "light.fixture_0001"


@pytest.mark.parametrize(
    "query",
    [
        "kitchen",
        "COOKING",
        " Downstairs ",
        "Ground flor",
        "Kitchenn",
        "Missing",
        "Study",
    ],
)
@pytest.mark.parametrize("floor_registry_available", [True, False])
def test_location_resolver_matches_legacy_contract(
    query: str, floor_registry_available: bool
) -> None:
    from custom_components.ha_mcp_tools.search_locations import _resolve_area_query
    from ha_mcp.tools.smart_search._entities import EntitySearchMixin

    areas = {
        "kitchen": {
            "area_id": "kitchen",
            "name": "Kitchen",
            "aliases": ["Cooking"],
            "floor_id": "ground",
        },
        "study": {"area_id": "study", "name": "Study", "floor_id": "ground"},
    }
    floors = (
        {
            "ground": {
                "floor_id": "ground",
                "name": "Ground floor",
                "aliases": ["Downstairs", "Study"],
            }
        }
        if floor_registry_available
        else {}
    )
    assert _resolve_area_query(
        areas, floors, query, floor_registry_available=floor_registry_available
    ) == EntitySearchMixin._resolve_area_query(
        areas, floors, query, floor_registry_available=floor_registry_available
    )


def test_location_resolution_observes_live_registry_changes(
    location_search: Any,
) -> None:
    hass, view = location_search
    params = {"search_types": ["entity"], "area_filter": "Downstairs"}
    assert wsapi._do_search(hass, params)["entity_total_matches"] == 2
    view.area._areas["bedroom"].floor_id = "ground"
    assert wsapi._do_search(hass, params)["entity_total_matches"] == 3
