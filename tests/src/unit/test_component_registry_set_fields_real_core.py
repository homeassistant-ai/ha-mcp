"""``ha_mcp_tools/registry_set_fields`` on a real ``hass``.

Core keeps some fields of a registry entry in a set and lists a set in hash
order. A backup diff and the restore of a deleted registry item ask this
command which of an entry's fields those are, instead of keeping the list on
the server. Each entry here is created through Core's own registry.
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
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_call, ws_result

_COMMAND = "ha_mcp_tools/registry_set_fields"


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

    assert result == {"set_fields": set_fields}


async def test_an_entry_core_does_not_hold_is_answered_with_none(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    result = await ws_result(component_ws, _COMMAND, registry="area", item_id="gone")

    assert result == {"set_fields": None}


async def test_a_device_registry_core_does_not_hand_out_is_refused(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A device registry that cannot be read is an error, as for the other
    registries, and not the answer for a device Core does not hold."""
    with patch.object(dr, "async_get", side_effect=RuntimeError("core drift")):
        reply = await ws_call(component_ws, _COMMAND, registry="device", item_id="any")

    assert reply["success"] is False, reply
    assert "device registry" in reply["error"]["message"], reply


async def test_a_category_without_its_scope_is_refused(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A category id is unique within its scope only, so a request without the
    scope is an error and not the answer for a category that is gone."""
    category = cr.async_get(hass).async_create(scope="automation", name="Lights")

    reply = await ws_call(
        component_ws, _COMMAND, registry="category", item_id=category.category_id
    )

    assert reply["success"] is False, reply
    assert reply["error"]["code"] == "home_assistant_error", reply
    assert "scope" in reply["error"]["message"], reply
