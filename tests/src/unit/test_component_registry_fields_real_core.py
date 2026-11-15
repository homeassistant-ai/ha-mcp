"""``ha_mcp_tools/registry_fields`` on a real ``hass``.

A backup restore writes a stored registry row back, and a backup diff compares
one with the live row. Which keys Core's commands take, which fields Core
keeps in a set (listed in hash order) and which points in time Core sets
itself are asked of this command instead of being kept on the server. Each
entry here is created through Core's own registry.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._command_faults import taking
from ._component_core import ws_call, ws_result

_COMMAND = "ha_mcp_tools/registry_fields"


@pytest.fixture
async def config_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` integration, which registers the registries' commands."""
    assert await async_setup_component(hass, "config", {})


def _entries(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    """One entry of every registry kind, as the parameters that name it."""
    label = lr.async_get(hass).async_create("Security")
    category = cr.async_get(hass).async_create(scope="automation", name="Lights")
    floor = fr.async_get(hass).async_create("Ground", aliases={"downstairs"})
    area = ar.async_get(hass).async_create(
        "Hall", aliases={"entrance"}, labels={label.label_id}
    )
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "1")}
    )
    child = devices.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("test", "2")},
        parent_device_id=device.id,
    )
    entities = er.async_get(hass)
    entity = entities.async_get_or_create("light", "test", "1")
    entities.async_update_entity(entity.entity_id, aliases=["first", "second"])
    return {
        "label": {"item_id": label.label_id},
        "category": {"item_id": category.category_id, "scope": "automation"},
        "floor": {"item_id": floor.floor_id},
        "area": {"item_id": area.id},
        "device": {"item_id": device.id},
        "child device": {"registry": "device", "item_id": child.id},
        "entity": {"item_id": entity.entity_id},
    }


# What the pinned Core keeps in a set. A Core release that changes one of these
# shows here, and the server follows the command's answer without a change.
@pytest.mark.usefixtures("config_commands")
@pytest.mark.parametrize(
    ("kind", "set_fields"),
    [
        ("label", []),
        ("category", []),
        ("floor", ["aliases"]),
        ("area", ["aliases", "labels"]),
        ("device", ["connections", "identifiers", "labels"]),
        ("child device", ["identifiers", "labels"]),
        # An entity's aliases are a list Core keeps in the order given.
        ("entity", ["labels"]),
    ],
)
async def test_the_fields_core_keeps_in_a_set_are_named(
    hass: HomeAssistant,
    component_ws: MockHAClientWebSocket,
    kind: str,
    set_fields: list[str],
) -> None:
    params = {"registry": kind, **_entries(hass)[kind]}

    result = await ws_result(component_ws, _COMMAND, **params)

    assert result["set_fields"] == set_fields
    # The points in time Core sets on every entry, which no update takes.
    assert result["stamped_fields"] == ["created_at", "modified_at"]


@pytest.mark.usefixtures("config_commands")
async def test_the_fields_a_command_takes_are_read_from_cores_schema(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """The keys are those of the schema Core registered, so a key Core's
    command starts to take is named, each command for itself; the keys of the
    message itself are not fields of the row."""
    before = await ws_result(component_ws, _COMMAND, registry="label")

    with taking(hass, "config/label_registry/update", "badge"):
        update_grew = await ws_result(component_ws, _COMMAND, registry="label")
    with taking(hass, "config/label_registry/create", "badge"):
        create_grew = await ws_result(component_ws, _COMMAND, registry="label")

    assert before["update_fields"] == [
        "color",
        "description",
        "icon",
        "label_id",
        "name",
    ]
    assert before["create_fields"] == ["color", "description", "icon", "name"]
    assert update_grew == {
        **before,
        "update_fields": sorted([*before["update_fields"], "badge"]),
    }
    assert create_grew == {
        **before,
        "create_fields": sorted([*before["create_fields"], "badge"]),
    }


@pytest.mark.usefixtures("config_commands")
@pytest.mark.parametrize("kind", ["device", "entity"])
async def test_a_registry_core_creates_no_entry_in_by_command_has_no_create_fields(
    component_ws: MockHAClientWebSocket, kind: str
) -> None:
    result = await ws_result(component_ws, _COMMAND, registry=kind)

    assert result["create_fields"] is None
    assert result["update_fields"]


@pytest.mark.usefixtures("config_commands")
async def test_a_point_in_time_the_update_command_takes_is_not_a_stamp(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A stamp is a point in time no update can set. On a Core whose update
    takes ``created_at``, only ``modified_at`` is left."""
    area = ar.async_get(hass).async_create("Hall")

    with taking(hass, "config/area_registry/update", "created_at"):
        result = await ws_result(
            component_ws, _COMMAND, registry="area", item_id=area.id
        )

    assert result["stamped_fields"] == ["modified_at"]


@pytest.mark.usefixtures("config_commands")
@pytest.mark.parametrize("params", [{}, {"item_id": "gone"}])
async def test_without_an_entry_only_the_commands_fields_are_answered(
    component_ws: MockHAClientWebSocket, params: dict[str, Any]
) -> None:
    """No ``item_id``, or one Core does not hold: what Core's commands take is
    answered, and nothing about an entry."""
    result = await ws_result(component_ws, _COMMAND, registry="area", **params)

    assert "name" in result["update_fields"]
    assert "name" in result["create_fields"]
    assert (result["set_fields"], result["stamped_fields"]) == (None, None)


async def test_a_registry_core_registers_no_update_command_for_is_refused(
    component_ws: MockHAClientWebSocket,
) -> None:
    """Without Core's ``config`` integration no registry has an update command:
    the command says so instead of naming no fields."""
    reply = await ws_call(component_ws, _COMMAND, registry="area")

    assert reply["success"] is False, reply
    assert "no update command" in reply["error"]["message"], reply


@pytest.mark.usefixtures("config_commands")
async def test_a_device_registry_core_does_not_hand_out_is_refused(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A device registry that cannot be read is an error, as for the other
    registries, and not the answer for a device Core does not hold."""
    with patch.object(dr, "async_get", side_effect=RuntimeError("core drift")):
        reply = await ws_call(component_ws, _COMMAND, registry="device", item_id="any")

    assert reply["success"] is False, reply
    assert "device registry" in reply["error"]["message"], reply


@pytest.mark.usefixtures("config_commands")
@pytest.mark.parametrize("scope", ["automation", "script"])
async def test_a_category_is_read_in_its_scope(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket, scope: str
) -> None:
    """A category id is unique within its scope only: the entry is found in the
    scope named, and a request without the scope is an error and not the answer
    for a category that is gone."""
    category = cr.async_get(hass).async_create(scope=scope, name="Lights")
    named = {"registry": "category", "item_id": category.category_id}

    found = await ws_result(component_ws, _COMMAND, **named, scope=scope)
    reply = await ws_call(component_ws, _COMMAND, **named)

    assert found["set_fields"] == []
    assert reply["success"] is False, reply
    assert reply["error"]["code"] == "home_assistant_error", reply
    assert "scope" in reply["error"]["message"], reply
