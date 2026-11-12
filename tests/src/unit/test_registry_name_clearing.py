"""Clearing and quote-only names on ``ha_set_device`` / ``ha_set_entity`` (issue #2585).

Some MCP clients cannot send an empty string: ``""`` arrives as two literal
quote characters and was stored as the device's name, while a single space
arrives intact. A whitespace-only value now clears like ``''`` does, a
quote-only name is rejected before any registry write, and a device rename
that did not happen is no longer echoed in ``updates``.

The tools run on the server's own client against a real ``hass``: the
devices, entities and areas are Core's registries.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_entities import EntityTools
from ha_mcp.tools.tools_registry import RegistryTools

from ._client_spies import sent_messages

QUOTE_ONLY_NAMES = ['""', "''", '" "']


@pytest.fixture
async def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    # ``config`` serves the registry commands.
    assert await async_setup_component(hass, "config", {})
    entry = MockConfigEntry(domain="test")
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def device(
    config_entry: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> dr.DeviceEntry:
    return device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("test", "lamp")},
        name="Lamp device",
    )


@pytest.fixture
def lamp(
    config_entry: MockConfigEntry,
    device: dr.DeviceEntry,
    entity_registry: er.EntityRegistry,
) -> str:
    """A light on ``device``; returns its entity_id."""
    return entity_registry.async_get_or_create(
        "light", "test", "lamp", config_entry=config_entry, device_id=device.id
    ).entity_id


@pytest.fixture
def area(area_registry: ar.AreaRegistry) -> str:
    return area_registry.async_create("Living Room").id


@pytest.fixture
def devices(core_client: HomeAssistantClient) -> RegistryTools:
    return RegistryTools(core_client)


@pytest.fixture
def entities(core_client: HomeAssistantClient) -> EntityTools:
    return EntityTools(core_client)


@contextmanager
def _nothing_sent(client: HomeAssistantClient) -> Iterator[None]:
    with sent_messages(client) as sent:
        yield
    assert sent.call_args_list == []


@contextmanager
def _rejected_with_clear_hint(client: HomeAssistantClient) -> Iterator[None]:
    """The call is refused before it sends anything, and the refusal offers
    the single-space clear."""
    with _nothing_sent(client), pytest.raises(ToolError) as exc_info:
        yield
    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    hints = " ".join([error.get("suggestion", ""), *error.get("suggestions", [])])
    assert "' '" in hints, f"hint must offer the single-space clear: {error}"


def _device_sends(sent: Any) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.call_args_list
        if call.args[0]["type"] == "config/device_registry/update"
    ]


# ---------------------------------------------------------------------------
# ha_set_device
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", QUOTE_ONLY_NAMES)
async def test_a_quote_only_name_is_not_stored_as_device_name(
    devices: RegistryTools,
    core_client: HomeAssistantClient,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
    name: str,
) -> None:
    with _rejected_with_clear_hint(core_client):
        await devices.ha_set_device(device_id=device.id, name=name)

    stored = device_registry.async_get(device.id)
    assert stored is not None
    assert stored.name_by_user is None


async def test_a_quote_only_disabled_by_is_rejected(
    devices: RegistryTools, core_client: HomeAssistantClient, device: dr.DeviceEntry
) -> None:
    with _rejected_with_clear_hint(core_client):
        await devices.ha_set_device(device_id=device.id, disabled_by='""')


@pytest.mark.parametrize("name", ["Bob's Lamp", '12" Monitor'])
async def test_a_name_containing_quotes_is_stored(
    devices: RegistryTools,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
    name: str,
) -> None:
    await devices.ha_set_device(device_id=device.id, name=name)

    stored = device_registry.async_get(device.id)
    assert stored is not None
    assert stored.name_by_user == name


@pytest.mark.parametrize(
    ("field", "value", "attribute"),
    [
        ("name", "", "name_by_user"),
        ("name", " ", "name_by_user"),
        ("area_id", " ", "area_id"),
        ("disabled_by", " ", "disabled_by"),
    ],
)
async def test_a_blank_value_clears_the_device_field(
    devices: RegistryTools,
    core_client: HomeAssistantClient,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
    area: str,
    field: str,
    value: str,
    attribute: str,
) -> None:
    device_registry.async_update_device(
        device.id,
        name_by_user="Old",
        area_id=area,
        disabled_by=dr.DeviceEntryDisabler.USER,
    )

    with sent_messages(core_client) as sent:
        await devices.ha_set_device(device_id=device.id, **{field: value})

    assert [call.args[0]["type"] for call in sent.call_args_list] == [
        "config/device_registry/update"
    ]
    stored = device_registry.async_get(device.id)
    assert stored is not None
    assert getattr(stored, attribute) is None


# ---------------------------------------------------------------------------
# ha_set_entity
# ---------------------------------------------------------------------------


async def test_a_padded_device_class_is_stripped(
    entities: EntityTools,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_id = entity_registry.async_get_or_create(
        "binary_sensor", "test", "window", config_entry=config_entry
    ).entity_id

    await entities.ha_set_entity(entity_id=entity_id, device_class=" window ")

    stored = entity_registry.async_get(entity_id)
    assert stored is not None
    assert stored.device_class == "window"


@pytest.mark.parametrize("field", ["name", "icon", "device_class"])
async def test_a_quote_only_value_is_not_stored(
    entities: EntityTools,
    core_client: HomeAssistantClient,
    lamp: str,
    field: str,
) -> None:
    with _rejected_with_clear_hint(core_client):
        await entities.ha_set_entity(entity_id=lamp, **{field: '""'})


async def test_a_quote_only_device_name_is_rejected_before_the_entity_write(
    entities: EntityTools,
    core_client: HomeAssistantClient,
    lamp: str,
    entity_registry: er.EntityRegistry,
) -> None:
    with _rejected_with_clear_hint(core_client):
        await entities.ha_set_entity(
            entity_id=lamp, icon="mdi:lamp", new_device_name='""'
        )

    stored = entity_registry.async_get(lamp)
    assert stored is not None
    assert stored.icon is None


@pytest.mark.parametrize("field", ["name", "area_id", "icon"])
async def test_whitespace_clears_the_entity_override(
    entities: EntityTools,
    lamp: str,
    area: str,
    entity_registry: er.EntityRegistry,
    field: str,
) -> None:
    # Core keeps an area of the entity's own only beside a name of its own.
    overrides: dict[str, Any] = {
        "name": {"name": "Old"},
        "area_id": {"name": "Old", "area_id": area},
        "icon": {"icon": "mdi:lamp"},
    }[field]
    entity_registry.async_update_entity(lamp, **overrides)

    await entities.ha_set_entity(entity_id=lamp, **{field: " "})

    stored = entity_registry.async_get(lamp)
    assert stored is not None
    assert getattr(stored, field) is None


# ---------------------------------------------------------------------------
# ha_set_entity's device rename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("new_device_name", ["", " "])
async def test_a_blank_device_name_neither_renames_nor_echoes(
    entities: EntityTools,
    core_client: HomeAssistantClient,
    lamp: str,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
    new_device_name: str,
) -> None:
    device_registry.async_update_device(device.id, name_by_user="Old")

    with sent_messages(core_client) as sent:
        result = await entities.ha_set_entity(
            entity_id=lamp, icon="mdi:lamp", new_device_name=new_device_name
        )

    assert _device_sends(sent) == []
    stored = device_registry.async_get(device.id)
    assert stored is not None
    assert stored.name_by_user == "Old"
    assert not any(u.startswith("device_name") for u in result["updates"])
    assert any("ha_set_device(name='')" in w for w in result["warnings"])
    assert "device_rename" not in result
    assert "partial" not in result


@pytest.mark.parametrize("new_device_name", ["", " "])
async def test_a_blank_device_name_alone_is_an_error(
    entities: EntityTools,
    core_client: HomeAssistantClient,
    lamp: str,
    new_device_name: str,
) -> None:
    with _rejected_with_clear_hint(core_client):
        await entities.ha_set_entity(entity_id=lamp, new_device_name=new_device_name)


async def test_a_deviceless_entity_rename_is_not_echoed(
    entities: EntityTools,
    config_entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    entity_id = entity_registry.async_get_or_create(
        "light", "test", "loose", config_entry=config_entry
    ).entity_id

    result = await entities.ha_set_entity(entity_id=entity_id, new_device_name="Lamp")

    assert not any(u.startswith("device_name") for u in result["updates"])
    assert result["device_rename"]["warnings"]
    assert result["warnings"] == result["device_rename"]["warnings"]


async def test_a_device_rename_core_refuses_is_not_echoed(
    core_client: HomeAssistantClient,
    hass_read_only_access_token: str,
    lamp: str,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A non-admin may read the entity's registry entry, but Core refuses it
    the device update."""
    with patch.object(core_client, "token", hass_read_only_access_token):
        result = await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, new_device_name="Lamp"
        )

    assert not any(u.startswith("device_name") for u in result["updates"])
    assert result["partial"] is True
    stored = device_registry.async_get(device.id)
    assert stored is not None
    assert stored.name_by_user is None


async def test_a_successful_device_rename_is_echoed(
    entities: EntityTools,
    lamp: str,
    device: dr.DeviceEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    result = await entities.ha_set_entity(entity_id=lamp, new_device_name="Lamp")

    assert any(u.startswith("device_name") for u in result["updates"])
    stored = device_registry.async_get(device.id)
    assert stored is not None
    assert stored.name_by_user == "Lamp"
