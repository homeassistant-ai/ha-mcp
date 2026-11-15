"""The area and floor tools on real Core.

Every row, list and refusal here is Core's: the tools talk to a real ``hass``
through the server's own client, and the tests read the registries back from
Core.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import pytest
from homeassistant.components.config import floor_registry as config_floors
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.json import json_bytes, json_fragment
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import get_websocket_client
from ha_mcp.tools.component_registries import WS_REGISTRIES
from ha_mcp.tools.tools_areas import AreaTools

from ._client_spies import sent_commands
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


async def _sent(client: HomeAssistantClient) -> Any:
    """A spy on every command the tools send for ``client``; Core answers each."""
    return sent_commands(
        await get_websocket_client(
            url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
        )
    )


def _types(spy: Any) -> list[str]:
    return [str(call.args[0]) for call in spy.call_args_list]


@contextmanager
def _floor_list_failing_on_read(
    monkeypatch: pytest.MonkeyPatch, read: int
) -> Iterator[None]:
    """Core refuses its ``read``-th floor list from here on and answers the others."""
    list_floors = fr.FloorRegistry.async_list_floors
    reads = 0

    def listed(self: fr.FloorRegistry) -> Any:
        nonlocal reads
        reads += 1
        if reads == read:
            raise HomeAssistantError("floor registry is not readable")
        return list_floors(self)

    with monkeypatch.context() as patched:
        patched.setattr(fr.FloorRegistry, "async_list_floors", listed)
        yield


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
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
) -> None:
    with await _sent(component_core_client) as sent:
        await _floors_in_reordered_registry(hass, component_tools)

    assert WS_REGISTRIES in _types(sent)
    assert "config/area_registry/list" not in _types(sent)
    assert "config/floor_registry/list" not in _types(sent)


async def test_a_field_core_adds_to_a_row_reaches_the_listing(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
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

    with await _sent(component_core_client) as sent:
        listed = await component_tools.ha_list_floors_areas()

    assert WS_REGISTRIES in _types(sent)
    assert "config/area_registry/list" not in _types(sent)
    assert "config/floor_registry/list" not in _types(sent)
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
    assert _stored_floor(floors, floor.floor_id).level == 2

    await tools.ha_set_area_or_floor(kind="floor", id=floor.floor_id, level=None)
    assert _stored_floor(floors, floor.floor_id).level is None


@pytest.mark.parametrize("level", [1, None])
async def test_level_is_still_refused_for_an_area(
    tools: AreaTools, level: int | None
) -> None:
    """A level, even a null one, belongs to a floor."""
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


@pytest.mark.parametrize("kind", ["area", "floor"])
async def test_updating_an_unknown_id_is_reported_as_not_found(
    tools: AreaTools, kind: Literal["area", "floor"]
) -> None:
    """Core answers an update of an id it lacks with "Unknown error"; the tool
    reads the registry then and says the area or floor was not found."""
    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind=kind, id="missing", name="Attic")
    )

    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert error[f"{kind}_id"] == "missing"
    assert error["ha_error_code"] == "unknown_error"


async def test_an_unknown_id_is_looked_up_through_the_component(
    component_tools: AreaTools, component_core_client: HomeAssistantClient
) -> None:
    """With the component, the tool asks the component for the registry that
    tells an unknown id from another "Unknown error"."""
    with await _sent(component_core_client) as sent:
        error = await rejected_call_body(
            component_tools.ha_set_area_or_floor(
                kind="area", id="missing", name="Attic"
            )
        )

    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert WS_REGISTRIES in _types(sent)


async def test_labels_core_dropped_are_read_back_through_the_component(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core drops a label that was removed after the tool checked it and still
    answers success; the read that confirms the loss asks the component."""
    label = lr.async_get(hass).async_create("Guests")
    area = ar.async_get(hass).async_create("Hall")
    # Core's answer once the label is gone: every label asked for is missing.
    monkeypatch.setattr(
        lr, "async_get_missing_label_ids", lambda hass, label_ids: set(label_ids)
    )

    with await _sent(component_core_client) as sent:
        error = await rejected_call_body(
            component_tools.ha_set_area_or_floor(
                kind="area", id=area.id, labels=[label.label_id]
            )
        )

    assert error["write_committed"] is True
    assert "config/area_registry/list" not in _types(sent)


async def test_an_update_core_refuses_with_a_code_of_its_own_reads_no_registry(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """Only "Unknown error" leaves open whether the id exists; a refusal with
    Core's own code, here a name that is taken, is passed on without a read."""
    areas = ar.async_get(hass)
    areas.async_create("Kitchen")
    hall = areas.async_create("Hall")

    with await _sent(component_core_client) as sent:
        error = await rejected_call_body(
            component_tools.ha_set_area_or_floor(
                kind="area", id=hall.id, name="Kitchen"
            )
        )

    assert error["ha_error_code"] == "invalid_info"
    assert WS_REGISTRIES not in _types(sent)


async def test_an_unknown_error_for_an_area_core_holds_stays_cores_refusal(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core answers "Unknown error" for whatever its update does not catch, a
    ``KeyError`` among them. When the area exists, the refusal is not turned
    into "not found"."""
    kitchen = ar.async_get(hass).async_create("Kitchen")

    def fail(self: ar.AreaRegistry, area_id: str, **fields: Any) -> ar.AreaEntry:
        raise KeyError("not the area id")

    monkeypatch.setattr(ar.AreaRegistry, "async_update", fail)

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="area", id=kitchen.id, name="Galley")
    )

    assert error["error"]["code"] != "RESOURCE_NOT_FOUND"
    assert error["ha_error_code"] == "unknown_error"


async def test_a_registry_that_cannot_be_read_after_unknown_error_is_what_is_reported(
    tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the read that tells an unknown id from another "Unknown error"
    fails, the caller gets that failure, not a "not found" nobody established."""

    def refuse(self: ar.AreaRegistry) -> Any:
        raise HomeAssistantError("area registry is not readable")

    monkeypatch.setattr(ar.AreaRegistry, "async_list_areas", refuse)

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="area", id="missing", name="Attic")
    )

    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "area registry is not readable" in error["error"]["message"]


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


async def test_a_reorder_reply_names_the_order_it_replaced(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """No backup keeps the order a reorder replaces: the reply carries it and
    says so."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    result = await tools.ha_set_area_or_floor(
        kind="floor", order=[attic.floor_id, ground.floor_id]
    )

    assert result["previous_order"] == [ground.floor_id, attic.floor_id]
    assert result["order"] == [attic.floor_id, ground.floor_id]
    (warning,) = result["warnings"]
    assert "previous_order" in warning


async def test_a_reorder_reads_both_orders_through_the_component(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """The order is read before and after Core's reorder command, and with the
    component the tool asks the component both times."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    with await _sent(component_core_client) as sent:
        await component_tools.ha_set_area_or_floor(
            kind="floor", order=[attic.floor_id, ground.floor_id]
        )

    reorder = "config/floor_registry/reorder"
    assert [t for t in _types(sent) if t in (WS_REGISTRIES, reorder)] == [
        WS_REGISTRIES,
        reorder,
        WS_REGISTRIES,
    ]


async def test_a_reorder_core_applied_succeeds_when_the_order_cannot_be_read_back(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Core applied the reorder, so a failed read of the new order is a warning,
    not a failed call, and the reply's order is None, not a guess."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    with _floor_list_failing_on_read(monkeypatch, read=2):
        result = await tools.ha_set_area_or_floor(
            kind="floor", order=[attic.floor_id, ground.floor_id]
        )

    assert result["success"] is True
    assert result["previous_order"] == [ground.floor_id, attic.floor_id]
    assert result["order"] is None
    assert len(result["warnings"]) == 2
    assert [f.floor_id for f in floors.async_list_floors()] == [
        attic.floor_id,
        ground.floor_id,
    ]


async def test_a_reorder_is_not_sent_when_the_order_it_replaces_cannot_be_read(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order before the call is the only record of what a reorder replaces;
    without it nothing is reordered."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    with _floor_list_failing_on_read(monkeypatch, read=1):
        error = await rejected_call_body(
            tools.ha_set_area_or_floor(
                kind="floor", order=[attic.floor_id, ground.floor_id]
            )
        )

    assert error["success"] is False
    assert [f.floor_id for f in floors.async_list_floors()] == [
        ground.floor_id,
        attic.floor_id,
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
    """Core refuses a list that misses an existing id; its refusal reaches the
    caller, next to the ids Core holds."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="floor", order=[ground.floor_id])
    )

    assert "exactly once" in error["error"]["message"]
    assert error["ha_error_code"] == "invalid_format"
    assert error["previous_order"] == [ground.floor_id, attic.floor_id]


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
    assert _stored_floor(floors, ground.floor_id).name == "Ground"


async def test_an_update_core_accepts_reads_no_registry(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """The registry is read only when Core refuses an update with "Unknown
    error"; an update it accepts reads it neither way."""
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")

    with await _sent(component_core_client) as sent:
        result = await component_tools.ha_set_area_or_floor(
            kind="area", id=kitchen.id, name="Galley"
        )

    assert result["success"] is True
    assert WS_REGISTRIES not in _types(sent)
    assert "config/area_registry/list" not in _types(sent)
    stored = areas.async_get_area(kitchen.id)
    assert stored is not None
    assert stored.name == "Galley"


@pytest.mark.parametrize(
    "param",
    [
        "floor_id",
        "level",
        "icon",
        "picture",
        "temperature_entity_id",
        "humidity_entity_id",
    ],
)
async def test_an_explicit_null_next_to_order_is_a_clash(
    tools: AreaTools, param: str
) -> None:
    """A null clears a field, so it counts as passed next to ``order``."""
    error = await rejected_call_body(
        tools.ha_set_area_or_floor(kind="area", order=["x"], **{param: None})
    )

    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert error["invalid_parameters"] == [param]


def _stored_floor(floors: fr.FloorRegistry, floor_id: str) -> fr.FloorEntry:
    floor = floors.async_get_floor(floor_id)
    assert floor is not None
    return floor
