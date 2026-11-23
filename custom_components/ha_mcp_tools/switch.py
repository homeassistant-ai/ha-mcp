"""The in-process server's access and write-permission switches.

Turning Access off closes every way into the server while the entry stays
loaded, so access can be granted on demand and revoked by automations. Its
state lives in ``entry.data`` (see :func:`embedded_entry.access_enabled`).
Read Only Mode persists the server's existing flag and applies it live; its
polling keeps the entity aligned with edits from the web panel.

Both entities are registered disabled, so they are opt-in: a service call with
``entity_id: all`` reaches every enabled entity of the switch platform,
whatever its entity category. Home Assistant adds no disabled entity to the
platform.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .embedded_entry import access_enabled, async_set_access
from .embedded_read_only import (
    ReadOnlyStateError,
    embedded_config_dir,
    read_only_mode,
    set_read_only_mode,
)

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)
SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add server controls; only the server entry forwards this platform."""
    async_add_entities(
        [ServerAccessSwitch(entry), ServerReadOnlyModeSwitch(hass, entry)],
        update_before_add=True,
    )


class ServerAccessSwitch(SwitchEntity):
    """Grants or blocks all access to the in-process server."""

    _attr_has_entity_name = True
    _attr_translation_key = "server_access"
    # A config entity is not exposed to Assist by default, so a conversation
    # agent or voice satellite is not offered it unless someone exposes it.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
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
        # The stored state is off before the stop runs; if the stop raises, the
        # switch must still show it.
        try:
            await async_set_access(self.hass, self._entry, False)
        finally:
            self.async_write_ha_state()


class ServerReadOnlyModeSwitch(SwitchEntity):
    """Control the embedded server's Read Only Mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "server_read_only_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False
    _attr_should_poll = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_server_read_only_mode"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})
        self._read_only = False
        self._config_dir: Path = embedded_config_dir(hass.config.config_dir)

    @property
    def is_on(self) -> bool:
        return self._read_only

    async def async_update(self) -> None:
        try:
            self._read_only = await self.hass.async_add_executor_job(
                read_only_mode, self._config_dir
            )
        except ReadOnlyStateError as err:
            self._attr_available = False
            _LOGGER.warning("Cannot read embedded Read Only Mode: %s", err)
        else:
            self._attr_available = True

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set_read_only(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set_read_only(False)

    async def _set_read_only(self, enabled: bool) -> None:
        await self.hass.async_add_executor_job(
            set_read_only_mode, self._config_dir, enabled
        )
        self._read_only = enabled
        self._attr_available = True
        self.async_write_ha_state()
