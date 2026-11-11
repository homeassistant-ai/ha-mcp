"""An update cannot blank a helper's name, as a create cannot (#2632)."""

import json

import pytest
from homeassistant.components.zone.const import DATA_ZONE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_zones import ZoneTools


async def test_blank_name_update_is_refused_before_any_write(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's zone schema would store a blank name, which the zone dialog
    refuses, so the tool refuses it without writing."""
    # ``config`` serves the registry commands.
    for domain in ("config", "zone"):
        assert await async_setup_component(hass, domain, {})
    created = await ZoneTools(core_client).ha_set_zone(
        name="Home Office", latitude=52.5, longitude=13.4, wait=False
    )
    entity_id = created["entity_id"]
    entry = er.async_get(hass).async_get(entity_id)
    assert entry is not None

    with pytest.raises(ToolError) as exc_info:
        await HelperConfigTools(core_client).ha_config_set_helper(
            helper_type="zone",
            helper_id=entity_id,
            config={"name": "  "},
        )

    error = json.loads(str(exc_info.value))["error"]
    assert "name cannot be blank" in error["message"]
    zones = hass.data[DATA_ZONE].storage_collection.async_items()
    stored = next(z for z in zones if z["id"] == entry.unique_id)
    assert stored["name"] == "Home Office"
