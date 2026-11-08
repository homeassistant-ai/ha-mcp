"""``ha_list_services`` shaping over Core's own service descriptions.

#2752 shipped with every service name and description empty: the routing tests
fed the server a hand-built payload that matched its wrong (nested) translation
lookup. Here the payload is Core's own, built by the component's prep from an
integration set up in a real ``hass`` (``counter``: its ``services.yaml``
filters on no supported features, so Core loads no other integration).
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api.services import (
    _do_services_list,
    _services_list_prep,
)
from ha_mcp.tools.tools_services import _process_services


async def _catalog(hass: HomeAssistant, domain: str) -> dict[str, Any]:
    assert await async_setup_component(hass, domain, {})
    await hass.async_block_till_done()
    prep = await _services_list_prep(hass, {"type": "ha_mcp_tools/services_list"})
    return _do_services_list(hass, {"domain": domain}, **prep)


async def test_listed_service_carries_cores_translated_names_and_descriptions(
    hass: HomeAssistant,
) -> None:
    catalog = await _catalog(hass, "counter")
    translations = catalog["translations"]

    out = _process_services(
        catalog["services"],
        translations,
        domain_filter="counter",
        detail_level="full",
    )

    set_value = out["services"]["counter.set_value"]
    prefix = "component.counter.services.set_value"
    assert set_value["name"] == translations[f"{prefix}.name"]
    assert set_value["description"] == translations[f"{prefix}.description"]
    assert (
        set_value["fields"]["value"]["description"]
        == (translations[f"{prefix}.fields.value.description"])
    )
