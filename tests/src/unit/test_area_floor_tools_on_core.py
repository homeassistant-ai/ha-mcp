"""The area and floor tools on real Core.

Every row, list and refusal here is Core's: the tools talk to a real ``hass``
through the server's own client, and the tests read the registries back from
Core.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from homeassistant.components.config import floor_registry as config_floors
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.json import json_bytes, json_fragment
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_areas import AreaTools

from ._tool_errors import rejected_call_body


@pytest.fixture(autouse=True)
async def _config_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` commands, which the registries' WebSocket API is part of."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> AreaTools:
    """The area tools on an install without the ``ha_mcp_tools`` component."""
    return AreaTools(core_client)


@pytest.fixture
def component_tools(component_core_client: HomeAssistantClient) -> AreaTools:
    """The area tools on an install whose ``hass`` also answers ``ha_mcp_tools/*``."""
    return AreaTools(component_core_client)


async def _floors_in_reordered_registry(hass: HomeAssistant, tools: AreaTools) -> None:
    """The floors come back in the order the user set with Core's reorder
    command, not re-sorted by level."""
    floors = fr.async_get(hass)
    cellar = floors.async_create("Cellar", level=-1)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)
    floors.async_reorder([attic.floor_id, cellar.floor_id, ground.floor_id])

    listed = await tools.ha_list_floors_areas()

    assert [f["floor_id"] for f in listed["floors"]] == [
        attic.floor_id,
        cellar.floor_id,
        ground.floor_id,
    ]


async def test_floors_are_listed_in_the_order_core_keeps(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    await _floors_in_reordered_registry(hass, tools)


async def test_floors_are_listed_in_the_order_core_keeps_with_the_component(
    hass: HomeAssistant, component_tools: AreaTools
) -> None:
    await _floors_in_reordered_registry(hass, component_tools)


async def test_areas_and_floors_are_listed_as_core_lists_them(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """A listed row is the row Core's own list command sends, field for field."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0, aliases={"downstairs", "first"})
    ar.async_get(hass).async_create(
        "Kitchen",
        floor_id=ground.floor_id,
        aliases={"cooking", "galley"},
        icon="mdi:stove",
    )

    listed = await component_tools.ha_list_floors_areas()

    core_floors = (
        await component_core_client.send_websocket_message(
            {"type": "config/floor_registry/list"}
        )
    )["result"]
    core_areas = (
        await component_core_client.send_websocket_message(
            {"type": "config/area_registry/list"}
        )
    )["result"]
    assert [_unordered(f) for f in _without_areas(listed["floors"])] == [
        _unordered(f) for f in core_floors
    ]
    assert [_unordered(a) for a in listed["floors"][0]["areas"]] == [
        _unordered(a) for a in core_areas
    ]


async def test_a_field_core_adds_to_a_row_reaches_the_listing(
    hass: HomeAssistant,
    component_tools: AreaTools,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the component, a field Core adds to an area or floor row is listed
    without the component naming it."""
    area_row = vars(ar.AreaEntry)["json_fragment"]

    def with_future_field(entry: ar.AreaEntry) -> json_fragment:
        row = json.loads(json_bytes(area_row.wrapped(entry)))
        return json_fragment(json_bytes({**row, "future_field": 1}))

    monkeypatch.setattr(ar.AreaEntry, "json_fragment", property(with_future_field))
    floor_row = "homeassistant.components.config.floor_registry._entry_dict"
    original = vars(config_floors)["_entry_dict"]
    monkeypatch.setattr(floor_row, lambda entry: {**original(entry), "future_field": 1})
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    ar.async_get(hass).async_create("Kitchen", floor_id=ground.floor_id)

    listed = await component_tools.ha_list_floors_areas()

    assert listed["floors"][0]["future_field"] == 1
    assert listed["floors"][0]["areas"][0]["future_field"] == 1


async def test_a_floor_level_is_cleared_by_null_and_kept_when_omitted(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """Core's floor update takes ``level=None`` to clear the level; omitting it
    leaves the level alone."""
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground", level=2)

    await tools.ha_set_area_or_floor(kind="floor", id=floor.floor_id, name="Main")
    assert _level_of(floors, floor.floor_id) == 2

    await tools.ha_set_area_or_floor(kind="floor", id=floor.floor_id, level=None)
    assert _level_of(floors, floor.floor_id) is None


async def test_level_is_still_refused_for_an_area(
    tools: AreaTools,
) -> None:
    """A level, even a null one, belongs to a floor."""
    for level in (1, None):
        error = await rejected_call_body(
            tools.ha_set_area_or_floor(kind="area", name="Kitchen", level=level)
        )
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_a_refused_write_keeps_cores_code_and_text(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """Core's reason and its error code reach the caller when it refuses a
    write, here a name that is already taken."""
    fr.async_get(hass).async_create("Ground", level=0)

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="floor", name="Ground", level=1)
    )

    assert "already in use" in error["error"]["message"]
    assert error["ha_error_code"] == "invalid_info"


@pytest.mark.parametrize("kind", ["area", "floor"])
async def test_removing_an_unknown_id_is_reported_as_not_found(
    tools: AreaTools, kind: Literal["area", "floor"]
) -> None:
    """Core answers an unknown id with its generic ``invalid_info`` code; the
    tool says the area or floor was not found and keeps Core's code."""
    error = await rejected_call_body(
        tools.ha_remove_area_or_floor(kind=kind, id="missing")
    )

    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error["ha_error_code"] == "invalid_info"


async def test_removing_a_floor_keeps_the_areas_it_carried(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """Removing a floor takes the floor off its areas and leaves them."""
    ground = fr.async_get(hass).async_create("Ground", level=0)
    kitchen = ar.async_get(hass).async_create("Kitchen", floor_id=ground.floor_id)

    result = await tools.ha_remove_area_or_floor(kind="floor", id=ground.floor_id)

    assert result["success"] is True
    stored = ar.async_get(hass).async_get_area(kitchen.id)
    assert stored is not None
    assert stored.floor_id is None


def _level_of(floors: fr.FloorRegistry, floor_id: str) -> int | None:
    floor = floors.async_get_floor(floor_id)
    assert floor is not None
    return floor.level


def _name_of(floors: fr.FloorRegistry, floor_id: str) -> str:
    floor = floors.async_get_floor(floor_id)
    assert floor is not None
    return floor.name


def _without_areas(floors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in f.items() if k != "areas"} for f in floors]


def _unordered(row: dict[str, Any]) -> dict[str, Any]:
    """A row with its set-valued lists in a fixed order: Core lists a set in hash
    order, which differs between two reads of it."""
    return {k: sorted(v) if k in ("aliases", "labels") else v for k, v in row.items()}


@pytest.mark.parametrize("kind", ["area", "floor"])
async def test_updating_an_unknown_id_is_reported_as_not_found(
    tools: AreaTools, kind: Literal["area", "floor"]
) -> None:
    """Core answers an update of an id it lacks with "Unknown error"; the tool
    says the area or floor was not found and how to create one."""
    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind=kind, id="missing", name="Attic")
    )

    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error[f"{kind}_id"] == "missing"


async def test_floors_are_reordered_with_cores_reorder(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """``order`` is Core's reorder: the registry lists the floors as sent."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    result = await tools.ha_set_area_or_floor(
        kind="floor", order=[attic.floor_id, ground.floor_id]
    )

    assert result["success"] is True
    assert [f.floor_id for f in floors.async_list_floors()] == [
        attic.floor_id,
        ground.floor_id,
    ]


async def test_areas_are_reordered_with_cores_reorder(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    hall = areas.async_create("Hall")

    await tools.ha_set_area_or_floor(kind="area", order=[hall.id, kitchen.id])

    assert [a.id for a in areas.async_list_areas()] == [hall.id, kitchen.id]


async def test_an_incomplete_order_is_refused_with_cores_reason(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """Core wants every existing id exactly once; its refusal reaches the caller."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    floors.async_create("Attic", level=2)

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="floor", order=[ground.floor_id])
    )

    assert "exactly once" in error["error"]["message"]
    assert error["ha_error_code"] == "invalid_format"


async def test_order_is_refused_next_to_other_parameters(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(
            kind="floor", order=[ground.floor_id], name="Main", level=None
        )
    )

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["invalid_parameters"] == ["name", "level"]
    assert _name_of(floors, ground.floor_id) == "Ground"
