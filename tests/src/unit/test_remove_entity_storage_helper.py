"""ha_remove_entity on a stored helper's entity (#2632): the stored helper
stays and gets its entity back when Home Assistant restarts, so the reply says
so and names the tool that removes the helper itself. A YAML helper has a
registry entry but no stored item, so its removal names no tool.

The tool runs on the server's own client against a real ``hass``: the
registry, the stored helpers and every refusal are Core's.
"""

import json
import uuid
from typing import Any
from unittest.mock import PropertyMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools

from ._gateway import Gateway
from ._gateway import gateway as gateway
from ._gateway import gateway_client as gateway_client


@pytest.fixture
async def config(hass: HomeAssistant) -> None:
    """Core's registry commands."""
    assert await async_setup_component(hass, "config", {})


async def _remove(client: HomeAssistantClient, entity_id: str) -> dict[str, Any]:
    result: dict[str, Any] = await EntityTools(client).ha_remove_entity(
        entity_id=entity_id
    )
    return result


@pytest.mark.parametrize(
    ("platform", "fields", "remove"),
    [
        (
            "input_boolean",
            {},
            "ha_remove_helpers_integrations(helper_type='input_boolean', "
            "target='{id}', confirm=True)",
        ),
        (
            "zone",
            {"latitude": 52.5, "longitude": 13.4, "radius": 50},
            "ha_remove_zone(zone_id='{id}')",
        ),
    ],
)
@pytest.mark.usefixtures("config")
async def test_a_stored_helper_s_entity_names_the_call_that_removes_it(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
    platform: str,
    fields: dict[str, Any],
    remove: str,
) -> None:
    """The stored porch stays, so removing its entity alone removes nothing.
    The registry is read before the entry is gone."""
    assert await async_setup_component(hass, platform, {})
    # Core restores a deleted helper's registry entry by name (test design rule 6).
    created = await core_client.send_websocket_message(
        {"type": f"{platform}/create", "name": f"Porch {uuid.uuid4().hex[:8]}"} | fields
    )
    storage_id = created["result"]["id"]
    entity_id = entity_registry.async_get_entity_id(platform, platform, storage_id)
    assert entity_id is not None
    # The entity_id no longer says which item it belongs to; the registry does.
    entity_registry.async_update_entity(entity_id, new_entity_id=f"{platform}.renamed")

    result = await _remove(core_client, f"{platform}.renamed")

    assert entity_registry.async_get(f"{platform}.renamed") is None
    assert remove.format(id=storage_id) in result["warnings"][0]
    assert "restarts" in result["warnings"][0]


@pytest.mark.usefixtures("config")
async def test_a_yaml_helper_s_entity_names_no_tool(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    core_client: HomeAssistantClient,
) -> None:
    """input_boolean porch has a registry entry but no stored item."""
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"porch": {"name": "Porch"}}}
    )
    entity_registry.async_update_entity(
        "input_boolean.porch", new_entity_id="input_boolean.renamed"
    )

    result = await _remove(core_client, "input_boolean.renamed")

    assert entity_registry.async_get("input_boolean.renamed") is None
    assert "warnings" not in result


@pytest.mark.usefixtures("config")
async def test_another_integration_s_entity_gets_no_helper_note(
    entity_registry: er.EntityRegistry, core_client: HomeAssistantClient
) -> None:
    entity_registry.async_get_or_create(
        "sensor", "hue", "abc", suggested_object_id="old"
    )

    result = await _remove(core_client, "sensor.old")

    assert entity_registry.async_get("sensor.old") is None
    assert "warnings" not in result


@pytest.mark.usefixtures("config")
async def test_an_entity_without_a_registry_entry_gets_no_note(
    core_client: HomeAssistantClient,
) -> None:
    """Its removal reports the missing entry itself."""
    with pytest.raises(ToolError) as exc_info:
        await _remove(core_client, "input_boolean.renamed")

    payload = json.loads(str(exc_info.value))
    assert payload["error"]["code"] == "ENTITY_NOT_FOUND"
    assert "warnings" not in payload


@pytest.mark.usefixtures("config")
async def test_a_dropped_registry_read_is_named_as_unknown_not_removed_silently(
    entity_registry: er.EntityRegistry,
    gateway: Gateway,
    gateway_client: HomeAssistantClient,
) -> None:
    """The gateway drops the socket on the registry read; the client reconnects
    and the removal after it goes through."""
    entity_registry.async_get_or_create(
        "input_boolean", "input_boolean", "porch", suggested_object_id="renamed"
    )
    gateway.drops = {"config/entity_registry/get"}

    result = await _remove(gateway_client, "input_boolean.renamed")

    assert entity_registry.async_get("input_boolean.renamed") is None
    assert "could not be read" in result["warnings"][0]


@pytest.mark.usefixtures("config")
async def test_a_refused_registry_read_is_named_as_unknown_not_removed_silently(
    entity_registry: er.EntityRegistry, core_client: HomeAssistantClient
) -> None:
    """A plain success would hide a stored helper the removal leaves."""
    entity_registry.async_get_or_create(
        "input_boolean", "input_boolean", "porch", suggested_object_id="renamed"
    )

    # Core refuses the registry read; the removal itself still goes through.
    with patch.object(
        er.RegistryEntry,
        "extended_dict",
        new_callable=PropertyMock,
        side_effect=Unauthorized(),
    ):
        result = await _remove(core_client, "input_boolean.renamed")

    assert entity_registry.async_get("input_boolean.renamed") is None
    assert "could not be read" in result["warnings"][0]


@pytest.mark.usefixtures("config")
async def test_a_refused_list_read_is_named_as_unknown_not_removed_silently(
    entity_registry: er.EntityRegistry, core_client: HomeAssistantClient
) -> None:
    """The entry is an input_boolean's, but Core has no input_boolean commands
    loaded, so the stored items cannot be listed."""
    entity_registry.async_get_or_create(
        "input_boolean", "input_boolean", "porch", suggested_object_id="renamed"
    )

    result = await _remove(core_client, "input_boolean.renamed")

    assert entity_registry.async_get("input_boolean.renamed") is None
    assert "could not be read" in result["warnings"][0]
