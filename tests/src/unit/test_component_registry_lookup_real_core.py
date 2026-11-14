"""Helper deletes resolved through the component's ``registry_lookup`` on a real ``hass``.

``ha_remove_helpers_integrations`` reads a flow helper's entities and a storage
helper's storage id from the entity registry. With the component installed
that is one ``ha_mcp_tools/registry_lookup`` read instead of the whole-registry
dump (flow helpers) or the per-id retry loop (storage helpers). The registry,
the config entries and the stored helpers are Core's own.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    get_websocket_client,
)
from ha_mcp.tools.component_registry_lookup import WS_REGISTRY_LOOKUP
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._client_spies import sent_commands


@pytest.fixture
async def tools(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> IntegrationTools:
    """The tool on a ``hass`` answering Core's registry commands and storing
    input_button helpers."""
    for domain in ("config", "input_button"):
        assert await async_setup_component(hass, domain, {})
    return IntegrationTools(component_core_client)


@pytest.fixture
async def socket(
    component_core_client: HomeAssistantClient,
) -> HomeAssistantWebSocketClient:
    """The pooled socket the registry lookup goes over."""
    return await get_websocket_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=component_core_client.verify_ssl,
    )


def _lookups(spy: Any) -> list[dict[str, Any]]:
    return [
        dict(c.kwargs) for c in spy.call_args_list if c.args[0] == WS_REGISTRY_LOOKUP
    ]


def _types(spy: Any) -> list[str]:
    return [str(c.args[0]) for c in spy.call_args_list]


async def _stored_button_ids(client: HomeAssistantClient) -> list[str]:
    listed = await client.send_websocket_message({"type": "input_button/list"})
    assert listed["success"], listed
    return [str(item["id"]) for item in listed["result"]]


async def _stored_button(
    hass: HomeAssistant, client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> tuple[str, str]:
    """A stored input_button with a fresh name; returns (storage_id, entity_id)."""
    created = await client.send_websocket_message(
        {"type": "input_button/create", "name": f"my_button_{uuid.uuid4().hex[:8]}"}
    )
    assert created["success"], created
    await hass.async_block_till_done()
    storage_id = str(created["result"]["id"])
    entity_id = entity_registry.async_get_entity_id(
        "input_button", "input_button", storage_id
    )
    assert entity_id is not None
    return storage_id, entity_id


async def test_a_flow_helper_delete_names_every_entity_of_its_entry(
    hass: HomeAssistant,
    tools: IntegrationTools,
    socket: HomeAssistantWebSocketClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """A utility_meter's tariff entities all belong to one entry; none is
    dropped, and the whole registry is never dumped to find them."""
    meter = MockConfigEntry(domain="utility_meter")
    meter.add_to_hass(hass)
    members = {
        entity_registry.async_get_or_create(
            domain, "utility_meter", unique_id, config_entry=meter
        ).entity_id
        for domain, unique_id in (
            ("sensor", "peak"),
            ("sensor", "offpeak"),
            ("select", "tariff"),
        )
    }
    other = MockConfigEntry(domain="test")
    other.add_to_hass(hass)
    entity_registry.async_get_or_create("light", "test", "other", config_entry=other)
    target = sorted(members)[0]

    with sent_commands(socket) as sent:
        result = await tools.ha_remove_helpers_integrations(
            target=target, helper_type="utility_meter", confirm=True, wait=False
        )

    assert result["entry_id"] == meter.entry_id
    assert set(result["entity_ids"]) == members
    assert hass.config_entries.async_get_entry(meter.entry_id) is None
    assert _lookups(sent) == [{"config_entry_id": meter.entry_id}]
    assert "config/entity_registry/list" not in _types(sent)


async def test_a_storage_helper_delete_resolves_its_storage_id_in_one_read(
    hass: HomeAssistant,
    tools: IntegrationTools,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """The entity was renamed, so only the registry knows its storage id; one
    component read finds it and the retrying per-id read never runs."""
    storage_id, entity_id = await _stored_button(
        hass, component_core_client, entity_registry
    )
    entity_registry.async_update_entity(
        entity_id, new_entity_id="input_button.front_door"
    )
    await hass.async_block_till_done()

    with sent_commands(socket) as sent:
        result = await tools.ha_remove_helpers_integrations(
            target="input_button.front_door",
            helper_type="input_button",
            confirm=True,
            wait=False,
        )

    assert result["unique_id"] == storage_id
    assert storage_id not in await _stored_button_ids(component_core_client)
    assert _lookups(sent) == [{"entity_ids": ["input_button.front_door"]}]
    assert "config/entity_registry/get" not in _types(sent)


async def test_an_unregistered_entity_never_deletes_the_helper_its_object_id_names(
    hass: HomeAssistant,
    tools: IntegrationTools,
    component_core_client: HomeAssistantClient,
    socket: HomeAssistantWebSocketClient,
    entity_registry: er.EntityRegistry,
) -> None:
    """The stored helper ``<id>`` was renamed away and an unregistered entity
    took ``input_button.<id>``: removing that entity must not guess the
    storage id from its object id and delete the other helper."""
    storage_id, entity_id = await _stored_button(
        hass, component_core_client, entity_registry
    )
    entity_registry.async_update_entity(entity_id, new_entity_id="input_button.moved")
    await hass.async_block_till_done()
    hass.states.async_set(entity_id, "unknown")

    with sent_commands(socket) as sent, pytest.raises(ToolError) as excinfo:
        await tools.ha_remove_helpers_integrations(
            target=entity_id, helper_type="input_button", confirm=True, wait=False
        )

    assert json.loads(str(excinfo.value))["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert _lookups(sent) == [{"entity_ids": [entity_id]}]
    assert "input_button/delete" not in _types(sent)
    assert storage_id in await _stored_button_ids(component_core_client)
