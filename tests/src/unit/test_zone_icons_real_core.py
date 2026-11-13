"""Zone and helper icons on a real ``hass``, through the server's own client.

The shared ``core_client`` fixture connects the server's client to the test
``hass``, so every reply, error and registry entry below is Core's own. The component
path registers the ``ha_mcp_tools/*`` commands; the core path leaves them
out, as an install without the component has none.
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components import websocket_api
from homeassistant.components.device_tracker import ATTR_IN_ZONES
from homeassistant.components.zone.const import DATA_ZONE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_zones import ZoneTools

_PATHS = ["component", "core"]


@pytest.fixture(params=_PATHS)
async def core_client(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> HomeAssistantClient:
    """The shared ``core_client`` on a ``hass`` with zones and input_boolean."""
    # ``config`` serves the registry commands.
    for domain in ("config", "zone", "input_boolean"):
        assert await async_setup_component(hass, domain, {})
    if request.param == "component":
        from custom_components.ha_mcp_tools import websocket_api

        websocket_api.async_register_commands(hass)
    return core_client


def _stored_zone(hass: HomeAssistant, entity_id: str) -> dict[str, Any]:
    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None
    zones = hass.data[DATA_ZONE].storage_collection.async_items()
    return next(z for z in zones if z["id"] == entity.unique_id)


def _override(hass: HomeAssistant, entity_id: str) -> str | None:
    entity = er.async_get(hass).async_get(entity_id)
    assert entity is not None
    return entity.icon


def _name() -> str:
    """A fresh zone name: Core gives a removed zone's registry entry back to
    a zone created again under its id."""
    return f"Office {uuid.uuid4().hex[:8]}"


async def _create(tools: ZoneTools, **fields: Any) -> dict[str, Any]:
    return await tools.ha_set_zone(
        name=fields.pop("name", None) or _name(),
        latitude=52.5,
        longitude=13.4,
        wait=False,
        **fields,
    )


async def test_a_created_zone_keeps_its_icon_in_the_zone(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The Zones editor and the zone dialog show only the stored icon (#3001)."""
    tools = ZoneTools(core_client)
    created = await _create(tools, icon="mdi:briefcase")

    assert _stored_zone(hass, created["entity_id"])["icon"] == "mdi:briefcase"
    assert _override(hass, created["entity_id"]) is None
    zone = (await tools.ha_get_zone(zone_id=created["zone_id"]))["zone"]
    assert zone["icon"] == "mdi:briefcase"
    assert "entity_icon" not in zone


async def test_an_icon_added_by_update_is_stored(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    tools = ZoneTools(core_client)
    created = await _create(tools)

    await tools.ha_set_zone(zone_id=created["zone_id"], icon="mdi:school", wait=False)

    assert _stored_zone(hass, created["entity_id"])["icon"] == "mdi:school"
    assert _override(hass, created["entity_id"]) is None


async def test_a_stored_icon_core_keeps_is_reported(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's zone update keeps a stored icon, as the zone dialog's does: the
    reply says it stayed instead of a silent success."""
    tools = ZoneTools(core_client)
    created = await _create(tools, icon="mdi:school")

    result = await tools.ha_set_zone(zone_id=created["zone_id"], icon="", wait=False)

    assert _stored_zone(hass, created["entity_id"])["icon"] == "mdi:school"
    assert result["zone_data"]["icon"] == "mdi:school"
    assert any("mdi:school" in w for w in result["warnings"])


async def test_an_override_is_named_apart_from_the_stored_icon(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An override older versions left in the registry hides a changed stored
    icon on the map: the update names it, and ha_get_zone shows both."""
    tools = ZoneTools(core_client)
    created = await _create(tools, icon="mdi:briefcase")
    er.async_get(hass).async_update_entity(created["entity_id"], icon="mdi:map")

    result = await tools.ha_set_zone(
        zone_id=created["zone_id"], icon="mdi:school", wait=False
    )

    assert result["zone_data"]["icon"] == "mdi:school"
    assert any("mdi:map" in w and "ha_set_entity" in w for w in result["warnings"])
    zone = (await tools.ha_get_zone(zone_id=created["zone_id"]))["zone"]
    assert (zone["icon"], zone["entity_icon"]) == ("mdi:school", "mdi:map")


async def test_a_cleared_icon_names_the_override_that_still_shows(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A zone older versions gave a registry-only icon keeps showing it after
    the icon is cleared: the reply names the override instead of a silent
    success."""
    tools = ZoneTools(core_client)
    created = await _create(tools)
    er.async_get(hass).async_update_entity(created["entity_id"], icon="mdi:map")

    result = await tools.ha_set_zone(zone_id=created["zone_id"], icon="", wait=False)

    assert any("mdi:map" in w and "ha_set_entity" in w for w in result["warnings"])


async def test_a_recreated_zone_names_its_restored_override(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core gives a zone created again under a removed zone's id that zone's
    registry entry back, icon override included."""
    tools = ZoneTools(core_client)
    name = _name()
    created = await _create(tools, name=name, icon="mdi:briefcase")
    er.async_get(hass).async_update_entity(created["entity_id"], icon="mdi:map")
    await tools.ha_remove_zone(zone_id=created["zone_id"], wait=False)

    again = await _create(tools, name=name, icon="mdi:school")

    assert any("mdi:map" in w for w in again.get("warnings", []))


async def test_every_zone_row_has_the_same_shape(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A stored zone lists who is in it like the home zone does: a row's
    shape must not depend on where the zone is defined."""
    tools = ZoneTools(core_client)
    # An icon, like the home zone's: a zone without one has no icon key.
    created = await _create(tools, icon="mdi:briefcase")
    hass.states.async_set(
        "device_tracker.phone", "Office", {ATTR_IN_ZONES: [created["entity_id"]]}
    )
    await hass.async_block_till_done()

    rows = {z["entity_id"]: z for z in (await tools.ha_get_zone())["zones"]}

    stored, home = rows[created["entity_id"]], rows["zone.home"]
    assert stored["device_trackers"] == ["device_tracker.phone"]
    assert set(stored) == set(home)


async def test_core_names_a_missing_coordinate(
    core_client: HomeAssistantClient,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await ZoneTools(core_client).ha_set_zone(
            name=_name(), longitude=13.4, wait=False
        )

    # Core's own zone/create message, not a copy of its required fields.
    assert "required key not provided" in str(exc_info.value)
    assert "latitude" in str(exc_info.value)


@pytest.mark.parametrize("core_client", ["component"], indirect=True)
async def test_the_component_reads_only_the_zones_states(
    core_client: HomeAssistantClient,
) -> None:
    """Reading every state of a large instance per zone lookup is the cost the
    component's targeted read exists to avoid."""
    tools = ZoneTools(core_client)
    await _create(tools)

    with patch.object(
        core_client, "get_states", wraps=core_client.get_states
    ) as all_states:
        result = await tools.ha_get_zone()

    all_states.assert_not_called()
    assert "warnings" not in result
    assert all("persons" in z for z in result["zones"])


@pytest.mark.parametrize("core_client", ["component"], indirect=True)
async def test_a_dropped_state_read_still_lists_who_is_in_each_zone(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A socket closed during the component's state read falls back to reading
    every state instead of failing a listing that was already complete."""
    tools = ZoneTools(core_client)
    created = await _create(tools, icon="mdi:briefcase")
    hass.states.async_set(
        "device_tracker.phone", "Office", {ATTR_IN_ZONES: [created["entity_id"]]}
    )
    # The component's own command stays registered with its own schema; when
    # Core receives the state read, the socket closes instead of an answer.
    _, schema = hass.data[websocket_api.DOMAIN]["ha_mcp_tools/states"]

    async def drop_the_socket(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        await websocket_manager.disconnect()

    websocket_api.async_register_command(
        hass,
        "ha_mcp_tools/states",
        websocket_api.async_response(drop_the_socket),
        schema,
    )

    with patch.object(
        core_client, "get_states", wraps=core_client.get_states
    ) as all_states:
        result = await tools.ha_get_zone()

    all_states.assert_awaited_once()
    rows = {z["entity_id"]: z for z in result["zones"]}
    assert rows[created["entity_id"]]["device_trackers"] == ["device_tracker.phone"]
    assert "warnings" not in result


@pytest.mark.parametrize("core_client", ["component"], indirect=True)
async def test_a_helper_icon_update_sets_no_registry_override(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The helper settings tab writes the icon to the stored item only; an
    override would hide every later change made there."""
    tools = HelperConfigTools(core_client)
    created = await tools.ha_config_set_helper(
        helper_type="input_boolean",
        name=f"Lamp {uuid.uuid4().hex[:8]}",
        icon="mdi:lamp",
        wait=False,
    )
    entity_id = created["entity_id"]

    await tools.ha_config_set_helper(
        helper_type="input_boolean", helper_id=entity_id, icon="mdi:star", wait=False
    )

    assert _override(hass, entity_id) is None
    state = hass.states.get(entity_id)
    assert state is not None and state.attributes["icon"] == "mdi:star"
