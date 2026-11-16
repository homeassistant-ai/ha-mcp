"""Clearing and quote-only names on ``ha_set_device`` / ``ha_set_entity`` (issue #2585).

Some MCP clients cannot send an empty string: ``""`` arrives as two literal
quote characters and was stored as the device's name, while a single space
arrives intact. A whitespace-only value now clears like ``''`` does, a
quote-only name is rejected before any registry write, and a device rename
that did not happen is no longer echoed in ``updates``.
"""

from __future__ import annotations

import asyncio

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

from ._held_command import held_command
from ._tool_errors import error_of

QUOTE_ONLY_NAMES = ['""', "''", '" "']


def _assert_rejected_with_clear_hint(exc_info: pytest.ExceptionInfo[ToolError]) -> None:
    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    hints = " ".join([error.get("suggestion", ""), *error.get("suggestions", [])])
    assert "' '" in hints, f"hint must offer the single-space clear: {error}"


@pytest.fixture(autouse=True)
async def registry_commands(hass: HomeAssistant) -> None:
    """Core's ``config`` integration, which answers the registry commands."""
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def device(hass: HomeAssistant, device_registry: dr.DeviceRegistry) -> dr.DeviceEntry:
    """A device the user named."""
    entry = MockConfigEntry(domain="demo")
    entry.add_to_hass(hass)
    created = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("demo", "lamp")}
    )
    named = device_registry.async_update_device(created.id, name_by_user="Old Lamp")
    assert named is not None
    return named


@pytest.fixture
def lamp(device: dr.DeviceEntry, entity_registry: er.EntityRegistry) -> str:
    """A light on ``device``."""
    return entity_registry.async_get_or_create(
        "light", "demo", "lamp", device_id=device.id
    ).entity_id


@pytest.fixture
def bulb(entity_registry: er.EntityRegistry) -> str:
    """A light on no device."""
    return entity_registry.async_get_or_create("light", "demo", "bulb").entity_id


class TestSetDeviceNameClearing:
    @pytest.mark.parametrize("name", QUOTE_ONLY_NAMES)
    async def test_quote_only_name_is_not_stored_as_device_name(
        self,
        core_client: HomeAssistantClient,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        name: str,
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await RegistryTools(core_client).ha_set_device(
                device_id=device.id, name=name
            )

        _assert_rejected_with_clear_hint(exc_info)
        assert device_registry.async_get(device.id) == device

    async def test_quote_only_disabled_by_is_rejected(
        self,
        core_client: HomeAssistantClient,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await RegistryTools(core_client).ha_set_device(
                device_id=device.id, disabled_by='""'
            )

        _assert_rejected_with_clear_hint(exc_info)
        assert device_registry.async_get(device.id) == device

    @pytest.mark.parametrize("name", ["Bob's Lamp", '12" Monitor'])
    async def test_name_containing_quotes_is_stored(
        self,
        core_client: HomeAssistantClient,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        name: str,
    ) -> None:
        await RegistryTools(core_client).ha_set_device(device_id=device.id, name=name)

        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.name_by_user == name

    @pytest.mark.parametrize("field", ["name", "area_id", "disabled_by"])
    async def test_blank_value_clears_device_field(
        self,
        core_client: HomeAssistantClient,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
        area_registry: ar.AreaRegistry,
        field: str,
    ) -> None:
        kitchen = area_registry.async_create("Kitchen")
        device_registry.async_update_device(
            device.id,
            area_id=kitchen.id,
            disabled_by=dr.DeviceEntryDisabler.USER,
        )

        await RegistryTools(core_client).ha_set_device(
            device_id=device.id, **{field: " "}
        )

        stored = device_registry.async_get(device.id)
        assert stored is not None
        cleared = {
            "name": stored.name_by_user,
            "area_id": stored.area_id,
            "disabled_by": stored.disabled_by,
        }
        assert cleared[field] is None
        assert all(value is not None for key, value in cleared.items() if key != field)


class TestSetEntityNameClearing:
    async def test_padded_device_class_is_stripped(
        self,
        core_client: HomeAssistantClient,
        entity_registry: er.EntityRegistry,
    ) -> None:
        window = entity_registry.async_get_or_create("binary_sensor", "demo", "window")

        await EntityTools(core_client).ha_set_entity(
            entity_id=window.entity_id, device_class=" window "
        )

        stored = entity_registry.async_get(window.entity_id)
        assert stored is not None
        assert stored.device_class == "window"

    @pytest.mark.parametrize("field", ["name", "icon", "device_class"])
    async def test_quote_only_value_is_not_stored(
        self,
        core_client: HomeAssistantClient,
        lamp: str,
        entity_registry: er.EntityRegistry,
        field: str,
    ) -> None:
        before = entity_registry.async_get(lamp)

        with pytest.raises(ToolError) as exc_info:
            await EntityTools(core_client).ha_set_entity(
                entity_id=lamp, **{field: '""'}
            )

        _assert_rejected_with_clear_hint(exc_info)
        assert entity_registry.async_get(lamp) == before

    async def test_quote_only_device_name_rejected_before_entity_write(
        self,
        core_client: HomeAssistantClient,
        lamp: str,
        device: dr.DeviceEntry,
        entity_registry: er.EntityRegistry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        before = entity_registry.async_get(lamp)

        with pytest.raises(ToolError) as exc_info:
            await EntityTools(core_client).ha_set_entity(
                entity_id=lamp, icon="mdi:lamp", new_device_name='""'
            )

        _assert_rejected_with_clear_hint(exc_info)
        assert entity_registry.async_get(lamp) == before
        assert device_registry.async_get(device.id) == device

    @pytest.mark.parametrize("field", ["name", "area_id", "icon"])
    async def test_whitespace_clears_entity_override(
        self,
        core_client: HomeAssistantClient,
        bulb: str,
        entity_registry: er.EntityRegistry,
        area_registry: ar.AreaRegistry,
        field: str,
    ) -> None:
        kitchen = area_registry.async_create("Kitchen")
        entity_registry.async_update_entity(
            bulb, name="Desk", area_id=kitchen.id, icon="mdi:desk-lamp"
        )

        await EntityTools(core_client).ha_set_entity(entity_id=bulb, **{field: " "})

        stored = entity_registry.async_get(bulb)
        assert stored is not None
        cleared = {"name": stored.name, "area_id": stored.area_id, "icon": stored.icon}
        assert cleared[field] is None
        assert all(value is not None for key, value in cleared.items() if key != field)


class TestSetEntityDeviceNameEcho:
    async def test_blank_device_name_neither_renames_nor_echoes(
        self,
        core_client: HomeAssistantClient,
        lamp: str,
        device: dr.DeviceEntry,
        entity_registry: er.EntityRegistry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        result = await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, icon="mdi:lamp", new_device_name=" "
        )

        stored = entity_registry.async_get(lamp)
        assert stored is not None
        assert stored.icon == "mdi:lamp"
        assert device_registry.async_get(device.id) == device
        assert not any(u.startswith("device_name") for u in result["updates"])
        assert any("ha_set_device(name='')" in w for w in result["warnings"])
        assert "device_rename" not in result
        assert "partial" not in result

    async def test_blank_device_name_alone_is_an_error(
        self,
        core_client: HomeAssistantClient,
        lamp: str,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        with pytest.raises(ToolError) as exc_info:
            await EntityTools(core_client).ha_set_entity(
                entity_id=lamp, new_device_name=" "
            )

        _assert_rejected_with_clear_hint(exc_info)
        assert device_registry.async_get(device.id) == device

    async def test_deviceless_entity_rename_not_echoed(
        self,
        core_client: HomeAssistantClient,
        bulb: str,
    ) -> None:
        result = await EntityTools(core_client).ha_set_entity(
            entity_id=bulb, new_device_name="Lamp"
        )

        assert not any(u.startswith("device_name") for u in result["updates"])
        assert result["device_rename"]["warnings"]
        assert result["warnings"] == result["device_rename"]["warnings"]

    async def test_failed_device_rename_not_echoed(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        lamp: str,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        """The device is removed while the rename is on its way: Core refuses it."""
        async with held_command(hass, "config/device_registry/update") as held:
            call = asyncio.create_task(
                EntityTools(core_client).ha_set_entity(
                    entity_id=lamp, new_device_name="Lamp"
                )
            )
            await held.reached_by(call)
            device_registry.async_remove_device(device.id)
            held.release.set()
            result = await call

        assert not any(u.startswith("device_name") for u in result["updates"])
        assert result["partial"] is True

    async def test_successful_device_rename_is_echoed(
        self,
        core_client: HomeAssistantClient,
        lamp: str,
        device: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        result = await EntityTools(core_client).ha_set_entity(
            entity_id=lamp, new_device_name="Lamp"
        )

        stored = device_registry.async_get(device.id)
        assert stored is not None
        assert stored.name_by_user == "Lamp"
        assert any(u.startswith("device_name") for u in result["updates"])
