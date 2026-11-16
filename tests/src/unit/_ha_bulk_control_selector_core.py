"""``ha_bulk_control`` on a real ``hass``, and a salon of lights for selector mode.

``light.sofa`` and ``light.vitrine`` sit in Core's ``salon`` area, both members
of ``light.salon_group``; ``light.veilleuse`` is in the salon too, hidden in
Core's entity registry. ``SwitchedStates`` serves the light services as a
device would, so every registry row, state and service call is Core's.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.bulk_selector import BulkControlSelector
from ha_mcp.tools.device_control import DeviceControlTools
from ha_mcp.tools.tools_service import ServiceTools

from ._switched_states import SwitchedStates

SALON: BulkControlSelector = {"domain": "light", "area_ids": ["salon"]}
SALON_BUT_VITRINE: BulkControlSelector = {
    "domain": "light",
    "area_ids": ["salon"],
    "exclude_entity_ids": ["light.vitrine"],
}


def bulk_tools(client: HomeAssistantClient) -> ServiceTools:
    """``ha_bulk_control`` with the device tools it dispatches through."""
    return ServiceTools(client, DeviceControlTools(client))


def switched(devices: SwitchedStates) -> list[tuple[str, str]]:
    """``(service, entity_id)`` of every light service call Core ran."""
    return [(call.service, call.data["entity_id"]) for call in devices.calls]


async def furnish_salon(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> SwitchedStates:
    """The salon above, with Core's registry commands loaded."""
    assert await async_setup_component(hass, "config", {})
    salon = area_registry.async_create("Salon")
    for object_id in ("sofa", "vitrine", "salon_group", "veilleuse"):
        entry = entity_registry.async_get_or_create(
            "light", "test", object_id, suggested_object_id=object_id
        )
        entity_registry.async_update_entity(entry.entity_id, area_id=salon.id)
        hass.states.async_set(entry.entity_id, "on")
    entity_registry.async_update_entity(
        "light.veilleuse", hidden_by=er.RegistryEntryHider.USER
    )
    hass.states.async_set(
        "light.salon_group", "on", {"entity_id": ["light.sofa", "light.vitrine"]}
    )
    return SwitchedStates(hass)
