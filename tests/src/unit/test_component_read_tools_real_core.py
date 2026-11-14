"""Read tools served by the component's commands on a real ``hass``.

Each tool runs on the server's own client against Core with the
``ha_mcp_tools/*`` commands registered, so a drift between what a component
command returns and what the tool makes of it fails here. Registries, states,
storage helpers, config entries and exposure settings are Core's
own, set up through Core's public API.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.homeassistant.exposed_entities import (
    async_expose_entity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_search import SearchTools

from ._core_setup import with_config_component
from ._search_tool import search_tools_on


@pytest.fixture
async def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The component-served client on a ``hass`` that answers registry commands."""
    return await with_config_component(hass, component_core_client)


@pytest.fixture
def search_tools(
    client: HomeAssistantClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SearchTools:
    return search_tools_on(client, tmp_path, monkeypatch)


@contextmanager
def _sent(client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on the WebSocket messages the server sends to Core meanwhile."""
    with patch.object(
        client,
        "send_websocket_message",
        AsyncMock(wraps=client.send_websocket_message),
    ) as spy:
        yield spy


def _types(spy: AsyncMock) -> list[str]:
    return [str(call.args[0].get("type")) for call in spy.call_args_list]


def _office_upstairs(hass: HomeAssistant) -> tuple[str, str]:
    """An Office area on an Upstairs floor; returns (area_id, floor_id)."""
    floor = fr.async_get(hass).async_create(f"Upstairs {uuid.uuid4().hex[:8]}")
    area = ar.async_get(hass).async_create(
        f"Office {uuid.uuid4().hex[:8]}", floor_id=floor.floor_id
    )
    return area.id, floor.floor_id


def _child_device_sensor(hass: HomeAssistant, area_id: str) -> str:
    """A sensor on a child device whose parent alone sits in ``area_id``."""
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    parent = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "parent")}
    )
    devices.async_update_device(parent.id, area_id=area_id)
    child = devices.async_get_or_create_child(
        config_entry_id=entry.entry_id,
        identifiers={("test", "child")},
        parent_device_id=parent.id,
    )
    sensor = er.async_get(hass).async_get_or_create(
        "sensor",
        "test",
        "child_sensor",
        config_entry=entry,
        device_id=child.id,
        original_name="Child Sensor",
    )
    hass.states.async_set(sensor.entity_id, "21", {"friendly_name": "Child Sensor"})
    return sensor.entity_id


def _names(hass: HomeAssistant, area_id: str, floor_id: str) -> tuple[str, str]:
    area = ar.async_get(hass).async_get_area(area_id)
    floor = fr.async_get(hass).async_get_floor(floor_id)
    assert area is not None and floor is not None
    return area.name, floor.name


# --- overview and search -------------------------------------------------------


async def test_overview_counts_a_child_device_entity_in_its_parents_area(
    hass: HomeAssistant, search_tools: SearchTools
) -> None:
    area_id, _ = _office_upstairs(hass)
    _child_device_sensor(hass, area_id)

    overview = await search_tools.ha_get_overview()

    assert overview["area_analysis"][area_id]["count"] == 1


async def test_search_names_a_child_device_entitys_inherited_area_and_floor(
    hass: HomeAssistant, search_tools: SearchTools
) -> None:
    area_id, floor_id = _office_upstairs(hass)
    entity_id = _child_device_sensor(hass, area_id)
    area, floor = _names(hass, area_id, floor_id)

    found = await search_tools.ha_search(
        query="child sensor", result_fields=["entity_id", "area", "floor"]
    )

    assert found["entities"] == [{"entity_id": entity_id, "area": area, "floor": floor}]


# --- helpers ---------------------------------------------------------------------


async def test_a_renamed_storage_helper_lists_its_storage_id_and_current_name(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The storage id is what an update needs; the name is the one shown now."""
    assert await async_setup_component(hass, "input_boolean", {})
    created = await client.send_websocket_message(
        {"type": "input_boolean/create", "name": f"Guest {uuid.uuid4().hex[:8]}"}
    )
    storage_id = created["result"]["id"]
    entity_id = er.async_get(hass).async_get_entity_id(
        "input_boolean", "input_boolean", storage_id
    )
    assert entity_id is not None
    er.async_get(hass).async_update_entity(entity_id, name="Current Guest")
    await hass.async_block_till_done()

    with _sent(client) as sent:
        listed = await HelperConfigTools(client).ha_config_list_helpers(
            helper_type="input_boolean"
        )

    (record,) = listed["helpers"]
    assert (record["id"], record["entity_id"], record["name"]) == (
        storage_id,
        entity_id,
        "Current Guest",
    )
    assert "input_boolean/list" not in _types(sent)


async def test_a_flow_helper_lists_its_entry_options_but_never_its_data(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    entry = MockConfigEntry(
        domain="template",
        title="Creation Title",
        options={"state": "{{ is_state('sun.sun', 'above_horizon') }}"},
        data={"api_key": "DATA_SECRET_XYZ"},
    )
    entry.add_to_hass(hass)
    sensor = er.async_get(hass).async_get_or_create(
        "binary_sensor", "template", "sun_up", config_entry=entry
    )
    er.async_get(hass).async_update_entity(sensor.entity_id, name="Sun Is Up")

    listed = await HelperConfigTools(client).ha_config_list_helpers(
        helper_type="template"
    )

    (record,) = listed["helpers"]
    assert record["entry_id"] == entry.entry_id
    assert record["entity_id"] == sensor.entity_id
    assert record["name"] == "Sun Is Up"
    assert record["options"] == {"state": "{{ is_state('sun.sun', 'above_horizon') }}"}
    assert "id" not in record
    assert "DATA_SECRET_XYZ" not in json.dumps(listed)


async def test_tags_come_from_cores_tag_list(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    """The component does not cover tags: an empty component answer must not
    read as "no tags exist"."""
    assert await async_setup_component(hass, "tag", {})
    await client.send_websocket_message({"type": "tag/create", "tag_id": "tag-42"})

    with _sent(client) as sent:
        listed = await HelperConfigTools(client).ha_config_list_helpers(
            helper_type="tag"
        )

    assert [h["id"] for h in listed["helpers"]] == ["tag-42"]
    assert "tag/list" in _types(sent)


# --- states ----------------------------------------------------------------------


async def test_bulk_states_come_from_the_component_with_missing_ids_named(
    hass: HomeAssistant, search_tools: SearchTools, client: HomeAssistantClient
) -> None:
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})
    hass.states.async_set("sensor.temp", "21", {"friendly_name": "Temp"})

    with patch.object(
        client, "get_entity_state", AsyncMock(wraps=client.get_entity_state)
    ) as rest_read:
        result = await search_tools.ha_get_state(
            entity_id=["light.lamp", "sensor.temp", "light.ghost"]
        )

    data = result["data"]
    assert set(data["states"]) == {"light.lamp", "sensor.temp"}
    assert data["states"]["light.lamp"]["attributes"]["friendly_name"] == "Lamp"
    assert [(e["entity_id"], e["error"]["code"]) for e in data["errors"]] == [
        ("light.ghost", "ENTITY_NOT_FOUND")
    ]
    rest_read.assert_not_awaited()


async def test_a_single_state_comes_from_the_component(
    hass: HomeAssistant, search_tools: SearchTools, client: HomeAssistantClient
) -> None:
    hass.states.async_set("light.lamp", "on", {"friendly_name": "Lamp"})

    with patch.object(
        client, "get_entity_state", AsyncMock(wraps=client.get_entity_state)
    ) as rest_read:
        result = await search_tools.ha_get_state(entity_id="light.lamp")

    assert (result["data"]["entity_id"], result["data"]["state"]) == (
        "light.lamp",
        "on",
    )
    rest_read.assert_not_awaited()


# --- entity registry enrichment --------------------------------------------------


async def test_a_child_device_entity_gets_its_parents_area_and_floor(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    area_id, floor_id = _office_upstairs(hass)
    entity_id = _child_device_sensor(hass, area_id)

    result = await EntityTools(client).ha_get_entity(entity_id=entity_id)

    entry = result["entity_entry"]
    assert (entry["area"], entry["floor"]) == _names(hass, area_id, floor_id)


# --- exposure listing ----------------------------------------------------


async def test_the_exposure_listing_names_a_child_device_entitys_parent_place(
    hass: HomeAssistant, client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    area_id, floor_id = _office_upstairs(hass)
    entity_id = _child_device_sensor(hass, area_id)
    async_expose_entity(hass, "conversation", entity_id, True)

    result = await EntityTools(client).ha_get_entity()

    info = result["entity_info"][entity_id]
    assert (info["area"], info["floor"]) == _names(hass, area_id, floor_id)
