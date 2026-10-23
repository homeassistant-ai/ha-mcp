"""The in-process server's access switch (#2801).

Turning it off closes every way into the server while the entry stays loaded,
so access can be granted on demand and revoked by automations. The state lives
in ``entry.data`` (see :func:`embedded_entry.access_enabled`), not in the
entity: the entry setup reads it before this platform exists, and a disabled
entity must not leave the server permanently off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .embedded_entry import access_enabled, async_set_access

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add the access switch; only the server entry forwards this platform."""
    async_add_entities([ServerAccessSwitch(entry)])


class ServerAccessSwitch(SwitchEntity):
    """Grants or blocks all access to the in-process server."""

    _attr_has_entity_name = True
    _attr_translation_key = "server_access"
    # A config entity is not exposed to Assist by default, so a conversation
    # agent or voice satellite cannot turn access on.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_server_access"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    @property
    def is_on(self) -> bool:
        return access_enabled(self._entry)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await async_set_access(self.hass, self._entry, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await async_set_access(self.hass, self._entry, False)
        self.async_write_ha_state()
