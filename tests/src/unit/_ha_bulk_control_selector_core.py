"""``ha_bulk_control`` on a real ``hass``, and a salon of lights for selector mode.

``light.sofa`` and ``light.vitrine`` sit in Core's ``salon`` area, both members
of ``light.salon_group``; ``light.veilleuse`` is in the salon too, hidden in
Core's entity registry. The lights are devices below Core's own ``light``
integration (``_switched_states``), so every registry row, state and service
call is Core's.
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

from ._switched_states import Devices, set_up_devices

SALON: BulkControlSelector = {"domain": "light", "area_ids": ["salon"]}
SALON_BUT_VITRINE: BulkControlSelector = {
    "domain": "light",
    "area_ids": ["salon"],
    "exclude_entity_ids": ["light.vitrine"],
}


def bulk_tools(client: HomeAssistantClient) -> ServiceTools:
    """``ha_bulk_control`` with the device tools it dispatches through."""
    return ServiceTools(client, DeviceControlTools(client))


def switched(devices: Devices) -> list[tuple[str, str]]:
    """``(command, entity_id)`` of every command a light received."""
    return [(call.command, call.entity_id) for call in devices.calls]


async def furnish_salon(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    entity_registry: er.EntityRegistry,
) -> Devices:
    """The salon above, with Core's registry commands loaded."""
    assert await async_setup_component(hass, "config", {})
    salon = area_registry.async_create("Salon")
    lights = ("light.sofa", "light.vitrine", "light.salon_group", "light.veilleuse")
    devices = await set_up_devices(
        hass,
        dict.fromkeys(lights, "on"),
        {"light.salon_group": ["light.sofa", "light.vitrine"]},
    )
    for entity_id in lights:
        entity_registry.async_update_entity(entity_id, area_id=salon.id)
    entity_registry.async_update_entity(
        "light.veilleuse", hidden_by=er.RegistryEntryHider.USER
    )
    return devices
