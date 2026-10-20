"""Config entry migrations, for both entry types."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import OPT_PIP_SPEC

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an entry up to the config flow's minor version.

    1.2 (#2784): drop a saved server package override, once, so the update
    runs the server its release pins; an override saved afterwards stays.
    """
    if entry.minor_version < 2:
        options = {k: v for k, v in entry.options.items() if k != OPT_PIP_SPEC}
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
    return True
