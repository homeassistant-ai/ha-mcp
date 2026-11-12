"""``ha_list_services`` shaping over Core's own service descriptions.

#2752 shipped with every service name and description empty: the routing tests
fed the server a hand-built payload that matched its wrong (nested) translation
lookup. Here the tool reads the catalog through the component from an
integration set up in a real ``hass`` (``counter``: its ``services.yaml``
filters on no supported features, so Core loads no other integration), and the
names are checked against Core's own translations.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_services import ServiceDiscoveryTools


async def test_listed_service_carries_cores_translated_names_and_descriptions(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "counter", {})
    await hass.async_block_till_done()
    translations = await async_get_translations(hass, "en", "services", ["counter"])

    out = await ServiceDiscoveryTools(component_core_client).ha_list_services(
        domain="counter", detail_level="full"
    )

    set_value = out["services"]["counter.set_value"]
    prefix = "component.counter.services.set_value"
    assert set_value["name"] == translations[f"{prefix}.name"]
    assert set_value["description"] == translations[f"{prefix}.description"]
    assert (
        set_value["fields"]["value"]["description"]
        == (translations[f"{prefix}.fields.value.description"])
    )
