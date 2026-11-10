"""The device each HA-MCP config entry is listed under."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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


def _manifest(monkeypatch: pytest.MonkeyPatch, read: AsyncMock) -> None:
    """What reading this integration's manifest gives."""
    monkeypatch.setattr(entry_device, "async_get_integration", read)


async def test_the_device_reports_the_installed_release(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(monkeypatch, AsyncMock(return_value=SimpleNamespace(version="9.1.0")))
    device = await _registered_device(hass, device_registry)
    assert device.sw_version == "9.1.0"


@pytest.mark.parametrize(
    "read",
    [
        AsyncMock(return_value=SimpleNamespace(version=None)),
        AsyncMock(side_effect=OSError("unreadable")),
    ],
    ids=["version-less manifest", "unreadable manifest"],
)
async def test_a_manifest_problem_never_reads_none_or_blocks_setup(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    monkeypatch: pytest.MonkeyPatch,
    read: AsyncMock,
) -> None:
    _manifest(monkeypatch, read)
    device = await _registered_device(hass, device_registry)
    assert device.sw_version == COMPONENT_VERSION
