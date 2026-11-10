"""``ha_mcp_tools/exposure`` list mode against Core's own ``expose_entity/list``.

The component builds its list from Core's public per-assistant settings; Core's
list walks its exposed-entities store and the entity registry. Both run here on
a real ``hass``, so the two maps must match entry for entry.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
    ws_list_exposed_entities,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api.lookups import _do_exposure


async def test_list_mode_matches_cores_list(hass: HomeAssistant) -> None:
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

    connection = MagicMock()
    ws_list_exposed_entities(
        hass, connection, {"id": 1, "type": "homeassistant/expose_entity/list"}
    )
    cores = connection.send_result.call_args.args[1]["exposed_entities"]

    assert cores == {
        "light.kitchen": {"conversation": True},
        "scene.movie": {"cloud.alexa": True},
    }
    assert _do_exposure(hass, {})["exposed_entities"] == cores
