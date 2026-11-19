"""The device each HA-MCP config entry is listed under."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.loader import async_get_integration
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ha_mcp_tools import entry_device
from custom_components.ha_mcp_tools.const import COMPONENT_VERSION, DOMAIN


async def _registered_device(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> dr.DeviceEntry:
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    await entry_device.async_register_entry_device(hass, entry, name="N", model="M")
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, entry.entry_id), entry.entry_id
    )
    assert device is not None
    return device


async def test_the_device_reports_the_installed_release(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
) -> None:
    installed = await async_get_integration(hass, DOMAIN)
    assert installed.version
    device = await _registered_device(hass, device_registry)
    assert device.sw_version == str(installed.version)


async def test_a_versionless_manifest_uses_the_component_version(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed = await async_get_integration(hass, DOMAIN)
    monkeypatch.delitem(installed.manifest, "version")
    device = await _registered_device(hass, device_registry)
    assert device.sw_version == COMPONENT_VERSION


async def test_an_unreadable_manifest_does_not_block_setup(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        entry_device, "async_get_integration", AsyncMock(side_effect=OSError("unreadable"))
    )
    device = await _registered_device(hass, device_registry)
    assert device.sw_version == COMPONENT_VERSION
