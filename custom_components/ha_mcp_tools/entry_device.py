"""The device each HA-MCP config entry is listed under in Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .const import COMPONENT_VERSION, DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def async_register_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, *, name: str, model: str
) -> None:
    """Create or refresh the entry's device, versioned from the manifest.

    The version comes from the manifest like the options-form version line,
    degrading to the compiled-in COMPONENT_VERSION so a manifest hiccup never
    breaks setup. Tied to the config entry, so Home Assistant removes the
    device with it.
    """
    from homeassistant.helpers import device_registry as dr
    from homeassistant.loader import async_get_integration

    version = COMPONENT_VERSION
    try:
        integration = await async_get_integration(hass, DOMAIN)
        if integration.version is None:
            # A manifest without a version reads as None rather than raising,
            # and ``str()`` would put the literal "None" on the device.
            raise ValueError("the manifest carries no version")
        version = str(integration.version)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug(
            "Could not read the component version for the %s device, using the "
            "compiled-in %s: %s",
            name,
            COMPONENT_VERSION,
            err,
        )
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=name,
        manufacturer="homeassistant-ai",
        model=model,
        sw_version=version,
        configuration_url="https://github.com/homeassistant-ai/ha-mcp",
    )
