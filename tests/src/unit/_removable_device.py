"""A device of a set-up integration that lets Core remove its devices."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant.config_entries import ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

DOMAIN = "removable"


async def removable_device(
    hass: HomeAssistant, *, name_by_user: str | None = None
) -> dr.DeviceEntry:
    """A device Core's ``config/device_registry/remove`` can remove."""
    mock_integration(
        hass,
        MockModule(
            DOMAIN,
            async_setup_entry=AsyncMock(return_value=None),
            async_remove_config_entry_device=AsyncMock(return_value=True),
        ),
    )
    mock_platform(hass, f"{DOMAIN}.config_flow", None)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    dev = registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "1")}
    )
    if name_by_user is not None:
        dev = registry.async_update_device(dev.id, name_by_user=name_by_user) or dev
    with mock_config_flow(DOMAIN, ConfigFlow):
        assert await hass.config_entries.async_setup(entry.entry_id)
    return dev
