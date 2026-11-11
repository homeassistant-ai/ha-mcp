"""Tests for config-entry reconfigure registry reads."""

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient


async def test_registry_lists_return_cores_rows(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Both registry methods return the rows Core's WebSocket API lists."""
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="demo")
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "device-1")}
    )
    entity = entity_registry.async_get_or_create(
        "light", "demo", "uid-1", device_id=device.id
    )

    devices = await core_client.list_device_registry()
    entities = await core_client.list_entity_registry()

    assert [row["id"] for row in devices] == [device.id]
    assert [row["entity_id"] for row in entities] == [entity.entity_id]
    assert entities[0]["device_id"] == device.id


@pytest.mark.parametrize(
    "method_name", ["list_entity_registry", "list_device_registry"]
)
async def test_registry_list_rejects_error_envelope(
    core_client: HomeAssistantClient, method_name: str
) -> None:
    """Core's error reply is not silently converted to an empty list.

    Without the ``config`` integration Core has no registry commands and
    answers ``unknown_command``.
    """
    with pytest.raises(HomeAssistantAPIError, match="Unexpected response from"):
        await getattr(core_client, method_name)()
