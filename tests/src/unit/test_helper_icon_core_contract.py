"""Where a storage helper's icon goes, and what is reported when its entity
cannot be found, on a real ``hass`` through the server's own client.

With the ``ha_mcp_tools`` component the tools read Core's own helper schemas:
person and tag declare no icon field, input_boolean does. Without the
component no schema is read, so an icon goes to the entity registry.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.config_helpers.listing import listed_items
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_zones import ZoneTools

from ._backup_target_identity_core import create_named, entity_of, move_entity
from ._command_faults import dropped_on, pooled_socket
from ._helper_tool_core import set_up
from ._helper_tool_core import tools as tools
from ._zone_icon_core import icon_of


async def stored(client: HomeAssistantClient, helper_type: str, item_id: str) -> Any:
    """The stored item ``item_id`` as ``<type>/list`` lists it."""
    listed = await client.send_websocket_message({"type": f"{helper_type}/list"})
    assert listed["success"], listed
    return next(i for i in listed_items(listed["result"]) if i["id"] == item_id)


async def test_a_created_zone_named_home_is_named_by_its_own_entity_id(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The built-in home zone holds zone.home, so Core names the new zone
    "Home" zone.home_2: zone.home would point the caller at the home zone."""
    for domain in ("config", "zone"):
        assert await async_setup_component(hass, domain, {})

    result = await ZoneTools(core_client).ha_set_zone(
        name="Home", latitude=52.5, longitude=13.4, wait=False
    )

    assert result["entity_id"] == "zone.home_2"
    assert entity_of(er.async_get(hass), "zone", result["zone_id"]) == "zone.home_2"


@pytest.mark.parametrize(
    "answered", [True, False], ids=["failure reply", "dropped connection"]
)
async def test_a_failed_registry_read_after_create_is_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient, answered: bool
) -> None:
    """The helper exists: an error would make a retrying caller create it
    twice. Without ``config`` Core refuses the registry read as an unknown
    command."""
    domains = ["input_boolean"] if answered else ["config", "input_boolean"]
    for domain in domains:
        assert await async_setup_component(hass, domain, {})
    tools = HelperConfigTools(core_client)

    if answered:
        result = await tools.ha_config_set_helper(
            helper_type="input_boolean", name="Lamp", icon="mdi:lamp", wait=False
        )
    else:
        socket = await pooled_socket(core_client)
        with dropped_on(hass, "config/entity_registry/list", socket):
            result = await tools.ha_config_set_helper(
                helper_type="input_boolean", name="Lamp", icon="mdi:lamp", wait=False
            )

    assert result["success"] is True
    assert "could not be read" in result["warnings"][0]
    assert (await stored(core_client, "input_boolean", "lamp"))["name"] == "Lamp"


@pytest.mark.parametrize(
    ("helper_type", "name"), [("person", "Anna"), ("tag", "Front door")]
)
async def test_an_icon_core_has_no_field_for_goes_to_the_entity_registry(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    helper_type: str,
    name: str,
) -> None:
    """Person and tag have no icon field in Core's schemas: the entity shows
    the registry icon."""
    await set_up(hass, helper_type)

    result = await HelperConfigTools(component_core_client).ha_config_set_helper(
        helper_type=helper_type, name=name, icon="mdi:star", wait=False
    )

    assert icon_of(er.async_get(hass), result["entity_id"]) == "mdi:star"
    item = await stored(component_core_client, helper_type, result["data"]["id"])
    assert "icon" not in item


async def test_a_person_update_writes_the_icon_to_the_entity_registry(
    hass: HomeAssistant, core_client: HomeAssistantClient, tools: HelperConfigTools
) -> None:
    await set_up(hass, "person")
    person_id = await create_named(core_client, "person", "Anna")
    entity_id = entity_of(er.async_get(hass), "person", person_id)

    await tools.ha_config_set_helper(
        helper_type="person", helper_id=entity_id, icon="mdi:account", wait=False
    )

    assert icon_of(er.async_get(hass), entity_id) == "mdi:account"
    assert "icon" not in await stored(core_client, "person", person_id)


@pytest.mark.parametrize(
    ("helper_type", "fields", "waited_on"),
    [
        # Unnamed: Core refuses to update a named tag without its entity.
        ("tag", {"tag_id": "abc-1"}, False),
        ("input_boolean", {"name": "Lamp"}, True),
    ],
    ids=["tag", "input_boolean"],
)
async def test_an_update_without_its_entity_names_what_was_not_applied(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    area_registry: ar.AreaRegistry,
    helper_type: str,
    fields: dict[str, Any],
    waited_on: bool,
) -> None:
    """The entity's registry entry was removed: the area cannot be applied.
    A tag update is never waited on, so a missing entity is no reason for it."""
    await set_up(hass, helper_type)
    created = await core_client.send_websocket_message(
        {"type": f"{helper_type}/create", **fields}
    )
    assert created["success"], created
    item_id = created["result"]["id"]
    entity_registry.async_remove(entity_of(entity_registry, helper_type, item_id))
    hall = area_registry.async_create("Hall")

    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type=helper_type, helper_id=item_id, area_id=hall.id, wait=True
    )

    assert "the area given were not applied" in result["warnings"][0]
    assert ("not waited on" in result["warnings"][0]) is waited_on


async def test_a_failed_registry_read_on_tag_update_is_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Even with no registry field given, the missing entity_id is named.
    Without ``config`` Core refuses the registry read as an unknown command."""
    assert await async_setup_component(hass, "tag", {})
    created = await core_client.send_websocket_message(
        {"type": "tag/create", "tag_id": "abc-1", "name": "Front door"}
    )
    assert created["success"], created

    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="tag", helper_id="abc-1", name="Front", wait=False
    )

    assert "could not be read" in result["warnings"][0]
    assert (await stored(core_client, "tag", "abc-1"))["name"] == "Front"


async def test_an_icon_core_s_schema_declares_stays_in_the_stored_item(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The helper dialog shows only the stored icon; a registry override
    would hide every later change made there."""
    await set_up(hass, "input_boolean")

    result = await HelperConfigTools(component_core_client).ha_config_set_helper(
        helper_type="input_boolean", name="Lamp", icon="mdi:lamp", wait=False
    )

    item = await stored(component_core_client, "input_boolean", "lamp")
    assert item["icon"] == "mdi:lamp"
    assert icon_of(er.async_get(hass), result["entity_id"]) is None


async def test_without_the_component_an_icon_goes_to_the_entity_registry(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """No schema says where Core keeps the icon: the entity settings' place
    is used, as the entity shows it."""
    await set_up(hass, "input_boolean")

    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean", name="Lamp", icon="mdi:lamp", wait=False
    )

    assert "icon" not in await stored(core_client, "input_boolean", "lamp")
    assert icon_of(er.async_get(hass), result["entity_id"]) == "mdi:lamp"


async def test_the_registry_icon_goes_to_the_new_helper_s_own_entity(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A renamed helper holds input_boolean.lamp, so Core names the new one
    input_boolean.lamp_2: <type>.<id> would give the icon to the other."""
    await set_up(hass, "input_boolean")
    other = await create_named(core_client, "input_boolean", "Other")
    move_entity(entity_registry, "input_boolean", other, "input_boolean.lamp")

    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean", name="Lamp", icon="mdi:lamp", wait=False
    )

    assert result["entity_id"] == "input_boolean.lamp_2"
    assert icon_of(entity_registry, "input_boolean.lamp_2") == "mdi:lamp"
    assert icon_of(entity_registry, "input_boolean.lamp") is None


async def test_a_cleared_icon_also_leaves_the_stored_item(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """Clearing only the registry would bring an older stored icon back."""
    await set_up(hass, "input_boolean")
    lamp = await create_named(core_client, "input_boolean", "Lamp", icon="mdi:lamp")
    entity_id = entity_of(entity_registry, "input_boolean", lamp)
    entity_registry.async_update_entity(entity_id, icon="mdi:map")

    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean", helper_id=entity_id, icon="", wait=False
    )

    assert "icon" not in await stored(core_client, "input_boolean", lamp)
    assert icon_of(entity_registry, entity_id) is None
