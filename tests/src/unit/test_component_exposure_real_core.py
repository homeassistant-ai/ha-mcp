"""``ha_mcp_tools/exposure`` list mode against Core's own ``expose_entity/list``.

The component builds its list from Core's public per-assistant settings; Core's
list walks its exposed-entities store and the entity registry. Both run here on
a real ``hass``, so the two maps must match entry for entry.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ._component_core import ws_result


async def test_list_mode_matches_cores_list(
    hass: HomeAssistant, component_ws: Any
) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    registry = er.async_get(hass)
    for object_id in ("kitchen", "attic"):
        registry.async_get_or_create(
            "light", "demo", object_id, suggested_object_id=object_id
        )
    async_expose_entity(hass, "conversation", "light.kitchen", True)
    async_expose_entity(hass, "cloud.alexa", "light.attic", False)
    # Without a registry entry Core keeps the setting in its store.
    async_expose_entity(hass, "cloud.alexa", "scene.movie", True)
    # Exposed while unregistered, registered afterwards: the store still holds
    # the setting, but Core's list reads the registry entry, which has none.
    async_expose_entity(hass, "cloud.alexa", "light.late", True)
    registry.async_get_or_create("light", "demo", "late", suggested_object_id="late")

    cores = (await ws_result(component_ws, "homeassistant/expose_entity/list"))[
        "exposed_entities"
    ]
    components = await ws_result(component_ws, "ha_mcp_tools/exposure")

    assert cores == {
        "light.kitchen": {"conversation": True},
        "scene.movie": {"cloud.alexa": True},
    }
    assert components["exposed_entities"] == cores
