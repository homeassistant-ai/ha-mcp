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
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.config import floor_registry as config_floors
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.json import json_bytes, json_fragment
from homeassistant.setup import async_setup_component

from ha_mcp.client.exceptions import HomeAssistantCommandNotSent
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_registries import WS_REGISTRIES
from ha_mcp.tools.tools_areas import AreaTools

from ._client_spies import sent_commands
from ._command_faults import before_handling, unanswered
from ._routing_core import pooled_socket, sent_types
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


@contextmanager
def _floor_list_failing_on_read(
    monkeypatch: pytest.MonkeyPatch, read: int
) -> Iterator[None]:
    """Counted from here, Core refuses the ``read``-th floor list and answers
    every other."""
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
    with sent_commands(await pooled_socket(component_core_client)) as sent:
        await _floors_in_reordered_registry(hass, component_tools)

    assert WS_REGISTRIES in sent_types(sent)
    assert "config/area_registry/list" not in sent_types(sent)
    assert "config/floor_registry/list" not in sent_types(sent)


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

    with sent_commands(await pooled_socket(component_core_client)) as sent:
        listed = await component_tools.ha_list_floors_areas()

    assert WS_REGISTRIES in sent_types(sent)
    assert "config/area_registry/list" not in sent_types(sent)
    assert "config/floor_registry/list" not in sent_types(sent)
    assert listed["floors"][0]["future_field"] == 1
    assert listed["floors"][0]["areas"][0]["future_field"] == 1


async def test_an_omitted_floor_level_is_kept(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """Core's floor update clears the level on ``level=None``, so an omitted
    level must not reach it as None."""
    floors = fr.async_get(hass)
    floor = floors.async_create("Ground", level=2)

    await tools.ha_set_area_or_floor(kind="floor", id=floor.floor_id, name="Main")

    assert _stored_floor(floors, floor.floor_id).level == 2


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


async def test_a_refused_remove_keeps_cores_code_and_text(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal other than an unknown id is Core's own, not a missing area."""
    kitchen = ar.async_get(hass).async_create("Kitchen")

    def refuse(self: ar.AreaRegistry, area_id: str) -> None:
        raise HomeAssistantError("area is locked")

    monkeypatch.setattr(ar.AreaRegistry, "async_delete", refuse)

    error = await rejected_call_body(
        tools.ha_remove_area_or_floor(kind="area", id=kitchen.id)
    )

    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert "area is locked" in error["error"]["message"]
    assert error["ha_error_code"] == "home_assistant_error"


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
    with sent_commands(await pooled_socket(component_core_client)) as sent:
        error = await rejected_call_body(
            component_tools.ha_set_area_or_floor(
                kind="area", id="missing", name="Attic"
            )
        )

    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert WS_REGISTRIES in sent_types(sent)


async def test_labels_core_dropped_are_read_back_through_the_component(
    hass: HomeAssistant,
    component_tools: AreaTools,
    component_core_client: HomeAssistantClient,
) -> None:
    """Core drops a label that was removed after the tool checked it and still
    answers success; the read that confirms the loss asks the component."""
    labels = lr.async_get(hass)
    label = labels.async_create("Guests")
    area = ar.async_get(hass).async_create("Hall")

    with (
        before_handling(
            hass,
            "config/area_registry/update",
            lambda msg: labels.async_delete(label.label_id),
        ),
        sent_commands(await pooled_socket(component_core_client)) as sent,
    ):
        error = await rejected_call_body(
            component_tools.ha_set_area_or_floor(
                kind="area", id=area.id, labels=[label.label_id]
            )
        )

    assert "requested labels" in error["error"]["message"]
    assert error["write_committed"] is True
    types = sent_types(sent)
    assert types[types.index("config/area_registry/update") + 1 :] == [WS_REGISTRIES]


async def test_a_refused_label_reread_reports_the_refusal_not_dropped_labels(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label is removed after the tool checked it, so Core drops it and
    answers success; the area list read that should confirm the loss is refused.
    That refusal decides nothing about the labels: the error says the check
    failed and carries Core's reason."""
    labels = lr.async_get(hass)
    label = labels.async_create("Guests")

    def refuse(self: ar.AreaRegistry) -> Any:
        raise HomeAssistantError("area registry is not readable")

    monkeypatch.setattr(ar.AreaRegistry, "async_list_areas", refuse)

    with before_handling(
        hass,
        "config/area_registry/create",
        lambda msg: labels.async_delete(label.label_id),
    ):
        error = await rejected_call_body(
            tools.ha_set_area_or_floor(
                kind="area", name="Hall", labels=[label.label_id]
            )
        )

    assert error["error"]["code"] == "SERVICE_CALL_FAILED"
    assert error["error"]["message"] == (
        "Area write succeeded, but label verification failed"
    )
    assert "area registry is not readable" in error["error"]["details"]
    assert error["write_committed"] is True
    assert error["expected_labels"] == [label.label_id]
    assert ar.async_get(hass).async_get_area(error["area_id"]) is not None


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

    with sent_commands(await pooled_socket(component_core_client)) as sent:
        error = await rejected_call_body(
            component_tools.ha_set_area_or_floor(
                kind="area", id=hall.id, name="Kitchen"
            )
        )

    assert error["ha_error_code"] == "invalid_info"
    assert WS_REGISTRIES not in sent_types(sent)


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


async def test_a_reorder_reply_names_the_order_read_before_it(
    hass: HomeAssistant, tools: AreaTools
) -> None:
    """No backup keeps the order from before a reorder: the reply carries the
    order read before it and says so."""
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


@pytest.mark.parametrize("kind", ["floor", "area"])
async def test_a_reorder_reply_names_the_order_core_lists_not_the_one_sent(
    hass: HomeAssistant, tools: AreaTools, kind: str
) -> None:
    """Core takes an id named twice at its first place. The reply's order is
    read from Core after the reorder, so it names each id once."""
    if kind == "floor":
        floors = fr.async_get(hass)
        first, second = (floors.async_create(n).floor_id for n in ("Ground", "Attic"))
    else:
        areas = ar.async_get(hass)
        first, second = (areas.async_create(n).id for n in ("Kitchen", "Hall"))

    result = await tools.ha_set_area_or_floor(kind=kind, order=[second, first, second])

    listed = (
        [f.floor_id for f in fr.async_get(hass).async_list_floors()]
        if kind == "floor"
        else [a.id for a in ar.async_get(hass).async_list_areas()]
    )
    assert result["order"] == listed == [second, first]


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

    with sent_commands(await pooled_socket(component_core_client)) as sent:
        await component_tools.ha_set_area_or_floor(
            kind="floor", order=[attic.floor_id, ground.floor_id]
        )

    reorder = "config/floor_registry/reorder"
    assert [t for t in sent_types(sent) if t in (WS_REGISTRIES, reorder)] == [
        WS_REGISTRIES,
        reorder,
        WS_REGISTRIES,
    ]


async def test_a_reorder_core_applied_succeeds_when_the_order_cannot_be_read_back(
    hass: HomeAssistant,
    tools: AreaTools,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Core applied the reorder, so a failed read of the new order is a warning,
    not a failed call, and the reply's order is None, not a guess. The log keeps
    why the read failed."""
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
    (logged,) = [r for r in caplog.records if r.name == "ha_mcp.tools.area_reorder"]
    assert (logged.levelname, bool(logged.exc_info)) == ("WARNING", True)
    assert [f.floor_id for f in floors.async_list_floors()] == [
        attic.floor_id,
        ground.floor_id,
    ]


async def test_a_reorder_is_not_sent_when_the_order_before_it_cannot_be_read(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order read before the reorder is the only record of the earlier order;
    without it nothing is reordered, and the error says so."""
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
    assert any("Nothing was reordered" in s for s in error["error"]["suggestions"])
    assert [f.floor_id for f in floors.async_list_floors()] == [
        ground.floor_id,
        attic.floor_id,
    ]


async def test_a_reorder_that_was_not_sent_says_nothing_was_reordered(
    hass: HomeAssistant,
    tools: AreaTools,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reorder that never left the server changed nothing: the error says so
    instead of sending the caller to check whether it was applied."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)
    # The order before it is read on a connection of its own, so this fails
    # the reorder alone.
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantCommandNotSent("no connection")),
    )

    error = await rejected_call_body(
        tools.ha_set_area_or_floor(
            kind="floor", order=[attic.floor_id, ground.floor_id]
        )
    )

    assert error["previous_order"] == [ground.floor_id, attic.floor_id]
    assert any("Nothing was reordered" in s for s in error["error"]["suggestions"])


async def test_a_reorder_core_does_not_answer_says_how_to_check_and_go_back(
    hass: HomeAssistant, tools: AreaTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reorder that was sent and got no answer may have been applied: the
    error carries the order read before the reorder and says how to read the
    one after it and how to go back."""
    floors = fr.async_get(hass)
    ground = floors.async_create("Ground", level=0)
    attic = floors.async_create("Attic", level=2)

    async with unanswered(hass, "config/floor_registry/reorder", monkeypatch):
        error = await rejected_call_body(
            tools.ha_set_area_or_floor(
                kind="floor", order=[attic.floor_id, ground.floor_id]
            )
        )

    assert error["previous_order"] == [ground.floor_id, attic.floor_id]
    suggestions = error["error"]["suggestions"]
    assert any("ha_list_floors_areas" in s for s in suggestions)
    assert any("previous_order" in s for s in suggestions)


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

    with sent_commands(await pooled_socket(component_core_client)) as sent:
        result = await component_tools.ha_set_area_or_floor(
            kind="area", id=kitchen.id, name="Galley"
        )

    assert result["success"] is True
    assert WS_REGISTRIES not in sent_types(sent)
    assert "config/area_registry/list" not in sent_types(sent)
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
