"""Search diagnostics for registry accessors that fail with or without a filter.

The component's ``ha_mcp_tools/search`` runs on a real ``hass``. Core's
registries cannot be made to fail, so the one accessor under test is replaced
on the real registry for the duration of the search.
"""

from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant.common import MockConfigEntry

from ._component_core import ws_result


async def _search(component_ws: Any, area_filter: str | None) -> dict[str, Any]:
    location = {"area_filter": area_filter} if area_filter else {}
    result: dict[str, Any] = await ws_result(
        component_ws, "ha_mcp_tools/search", search_types=["entity"], **location
    )
    return result


@pytest.mark.parametrize("area_filter", ["Kitchen", None])
@pytest.mark.parametrize(
    ("registry_name", "method_name"),
    [
        ("entity", "async_get"),
        ("area", "async_get_area"),
        ("floor", "async_get_floor"),
        ("label", "async_get_label"),
    ],
)
async def test_search_reports_failed_registry_lookup(
    hass: HomeAssistant,
    component_ws: Any,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    label_registry: lr.LabelRegistry,
    registry_name: str,
    method_name: str,
    area_filter: str | None,
) -> None:
    floor = floor_registry.async_create("Ground floor")
    kitchen = area_registry.async_create("Kitchen", floor_id=floor.floor_id)
    label = label_registry.async_create("Task lighting")
    entry = entity_registry.async_get_or_create(
        "light", "test", "kitchen", suggested_object_id="kitchen"
    )
    entity_registry.async_update_entity(
        entry.entity_id, area_id=kitchen.id, labels={label.label_id}
    )
    hass.states.async_set("light.kitchen", "on")
    registries: dict[str, object] = {
        "entity": entity_registry,
        "area": area_registry,
        "floor": floor_registry,
        "label": label_registry,
    }

    with patch.object(
        registries[registry_name],
        method_name,
        side_effect=RuntimeError("registry unavailable"),
    ):
        result = await _search(component_ws, area_filter)

    assert result["partial"] is True
    assert registry_name in result["partial_reason"]
    diagnostic = "location" if area_filter else "entity"
    assert result["diagnostics"][f"{diagnostic}_registries_unavailable"] == 1


@pytest.mark.parametrize("area_filter", ["Kitchen", None])
async def test_missing_registry_entries_are_not_accessor_failures(
    hass: HomeAssistant,
    component_ws: Any,
    area_registry: ar.AreaRegistry,
    area_filter: str | None,
) -> None:
    area_registry.async_create("Kitchen")
    hass.states.async_set("light.kitchen", "on")

    result = await _search(component_ws, area_filter)

    assert result["partial"] is False
    assert result["entity_total_matches"] == (0 if area_filter else 1)


@pytest.mark.parametrize("area_filter", ["Kitchen", None])
async def test_search_reports_failed_device_collection(
    hass: HomeAssistant,
    component_ws: Any,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    floor_registry: fr.FloorRegistry,
    area_filter: str | None,
) -> None:
    """The light's area comes only from its device, so a device collection
    that cannot be enumerated loses the area match and says so."""
    floor = floor_registry.async_create("Ground floor")
    kitchen = area_registry.async_create("Kitchen", floor_id=floor.floor_id)
    config_entry = MockConfigEntry(domain="test")
    config_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={("test", "kitchen")}
    )
    device_registry.async_update_device(device.id, area_id=kitchen.id)
    entity_registry.async_get_or_create(
        "light", "test", "kitchen", device_id=device.id, suggested_object_id="kitchen"
    )
    hass.states.async_set("light.kitchen", "on")

    # Iterating the registry's devices reads the container's values.
    with patch.object(
        dr.ActiveDeviceRegistryItems,
        "values",
        side_effect=RuntimeError("device collection unavailable"),
    ):
        result = await _search(component_ws, area_filter)

    assert result["entity_total_matches"] == (0 if area_filter else 1)
    assert result["partial"] is True
    assert "device" in result["partial_reason"]
    diagnostic = "location" if area_filter else "entity"
    assert result["diagnostics"][f"{diagnostic}_registries_unavailable"] == 1
