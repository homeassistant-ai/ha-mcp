"""Devices on a real ``hass``, each the entity below Core's own ``light``,
``switch``, ``number``, ``lock``, ``climate`` or ``media_player`` integration.

The entities subclass that integration's entity class and are added through
``setup_test_component_platform``, so every call goes through Core's services
and their validation. ``calls`` keeps
what each device received. A device follows the call by default; ``behaviour``
makes one ``stay`` as it is, settle ``on`` or ``off`` whatever was asked, be
``remove``d, or ``fail_once`` before it follows. Core hands no call to an
unavailable entity, so an unavailable device does its ``on``, ``off`` or
``remove`` when Core fires a call naming it: it comes back, or goes away,
while the call is out.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.components.lock import LockEntity
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.number import NumberEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    EVENT_CALL_SERVICE,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import setup_test_component_platform

UNAVAILABLE = "unavailable"
# A lock or player reaches its end state this long after its intermediate one:
# long enough that a reply cut short at the intermediate state would show it.
SECOND_PHASE_S = 0.05


@dataclass(frozen=True)
class Received:
    """One command a device received from Core's integration."""

    entity_id: str
    command: str
    data: dict[str, Any]


class Devices:
    """The devices of one test, by entity id."""

    def __init__(self) -> None:
        self.calls: list[Received] = []
        self.behaviour: dict[str, str] = {}
        # Runs as each device receives a command, to observe Core at that moment.
        self.on_call: Callable[[], object] | None = None
        self.by_id: dict[str, _Device] = {}

    def targets(self) -> list[str]:
        """The entity id of every command received, in order."""
        return [call.entity_id for call in self.calls]

    @callback
    def heard(self, event: Event[Any]) -> None:
        """Apply ``on``/``off``/``remove`` to an unavailable device a call names."""
        raw = event.data["service_data"].get(ATTR_ENTITY_ID) or []
        named = raw.split(",") if isinstance(raw, str) else list(raw)
        for entity_id in (name.strip() for name in named):
            device = self.by_id.get(entity_id)
            if (
                device is not None
                and not device.available
                and self.behaviour.get(entity_id) in ("on", "off", "remove")
            ):
                device.hass.async_create_task(device.settle(lambda: None))


class _Device(Entity):
    _attr_should_poll = False

    def __init__(self, devices: Devices, entity_id: str, state: str) -> None:
        self.entity_id = entity_id
        self._attr_unique_id = entity_id
        self._attr_available = state != UNAVAILABLE
        self.devices = devices
        devices.by_id[entity_id] = self

    def to_off(self) -> None:
        """Settle off; only a device with an on/off state has one."""

    def to_on(self) -> None:
        """Settle on; only a device with an on/off state has one."""

    async def receive(
        self, command: str, data: dict[str, Any], follow: Callable[[], None]
    ) -> None:
        self.devices.calls.append(Received(self.entity_id, command, data))
        if self.devices.on_call is not None:
            self.devices.on_call()
        # The device answers after a round trip, not inside the call itself.
        await asyncio.sleep(0)
        await self.settle(follow)

    async def settle(self, follow: Callable[[], None]) -> None:
        how = self.devices.behaviour.get(self.entity_id, "follow")
        if how == "fail_once":
            self.devices.behaviour[self.entity_id] = "follow"
            raise HomeAssistantError(f"{self.entity_id} did not answer")
        if how == "stay":
            return
        if how == "remove":
            # Gone for good: a registered entity would otherwise leave a
            # restored ``unavailable`` state behind.
            await self.async_remove(force_remove=True)
            return
        self._attr_available = True
        if how == "off":
            self.to_off()
        elif how == "on":
            self.to_on()
        else:
            follow()
        self.async_write_ha_state()


class Light(_Device, LightEntity):
    _attr_color_mode = ColorMode.BRIGHTNESS

    def __init__(
        self,
        devices: Devices,
        entity_id: str,
        state: str,
        members: list[str] | None,
    ) -> None:
        super().__init__(devices, entity_id, state)
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_is_on = state == "on"
        self._attr_brightness = 255
        if members is not None:
            # A group light, as Core's group lights report their members.
            self._attr_extra_state_attributes = {ATTR_ENTITY_ID: members}

    def to_off(self) -> None:
        self._attr_is_on = False

    def to_on(self) -> None:
        self._attr_is_on = True

    async def async_turn_on(self, **kwargs: Any) -> None:
        def follow() -> None:
            self._attr_is_on = True
            self._attr_brightness = kwargs.get("brightness", self._attr_brightness)

        await self.receive("turn_on", kwargs, follow)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.receive("turn_off", kwargs, self.to_off)


class Switch(_Device, SwitchEntity):
    def __init__(self, devices: Devices, entity_id: str, state: str) -> None:
        super().__init__(devices, entity_id, state)
        self._attr_is_on = state == "on"

    def to_off(self) -> None:
        self._attr_is_on = False

    def to_on(self) -> None:
        self._attr_is_on = True

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.receive("turn_on", kwargs, self.to_on)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.receive("turn_off", kwargs, self.to_off)


class Number(_Device, NumberEntity):
    def __init__(self, devices: Devices, entity_id: str, state: str) -> None:
        super().__init__(devices, entity_id, state)
        self._attr_native_value = None if state == UNAVAILABLE else float(state)

    async def async_set_native_value(self, value: float) -> None:
        def follow() -> None:
            self._attr_native_value = value

        await self.receive("set_value", {"value": value}, follow)


class Lock(_Device, LockEntity):
    """Passes through ``locking`` and reports ``locked`` a moment later."""

    def __init__(self, devices: Devices, entity_id: str, state: str) -> None:
        super().__init__(devices, entity_id, state)
        self._attr_is_locked = state == "locked"

    async def _locked(self) -> None:
        await asyncio.sleep(SECOND_PHASE_S)
        self._attr_is_locking = False
        self._attr_is_locked = True
        self.async_write_ha_state()

    async def async_lock(self, **kwargs: Any) -> None:
        def follow() -> None:
            self._attr_is_locking = True
            self.hass.async_create_task(self._locked())

        await self.receive("lock", kwargs, follow)


class Climate(_Device, ClimateEntity):
    """A thermostat in one HVAC mode, its target starting at 19 °C."""

    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(self, devices: Devices, entity_id: str, state: str) -> None:
        super().__init__(devices, entity_id, state)
        self._attr_hvac_modes = [HVACMode(state)]
        self._attr_hvac_mode = HVACMode(state)
        self._attr_native_target_temperature = 19.0

    async def async_set_temperature(self, **kwargs: Any) -> None:
        def follow() -> None:
            self._attr_native_target_temperature = kwargs[ATTR_TEMPERATURE]

        await self.receive("set_temperature", kwargs, follow)


class MediaPlayer(_Device, MediaPlayerEntity):
    """Reports a position tick while still playing, then ``paused``."""

    _attr_supported_features = (
        MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY
    )

    def __init__(self, devices: Devices, entity_id: str, state: str) -> None:
        super().__init__(devices, entity_id, state)
        self._attr_state = MediaPlayerState(state)
        self._attr_media_position = 0
        self._attr_media_position_updated_at = dt_util.utcnow()

    async def _paused(self) -> None:
        await asyncio.sleep(SECOND_PHASE_S)
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        def follow() -> None:
            self._attr_media_position = 5
            self._attr_media_position_updated_at = dt_util.utcnow()
            self.hass.async_create_task(self._paused())

        await self.receive("media_pause", {}, follow)


_DEVICES: dict[str, type[Switch | Number | Lock | Climate | MediaPlayer]] = {
    "switch": Switch,
    "number": Number,
    "lock": Lock,
    "climate": Climate,
    "media_player": MediaPlayer,
}


RANG = "rang"


async def set_up_ringing_automation(hass: HomeAssistant) -> str:
    """A Core automation that fires ``RANG`` when triggered; its entity id."""
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "ring",
                "alias": "Ring",
                "triggers": {"trigger": "event", "event_type": "never_fired"},
                "actions": {"event": RANG},
            }
        },
    )
    await hass.async_block_till_done()
    return "automation.ring"


async def set_up_devices(
    hass: HomeAssistant,
    states: dict[str, str],
    members: dict[str, list[str]] | None = None,
) -> Devices:
    """One device per entity id in its starting state (``unavailable`` for one
    Core cannot reach), with Core's integration set up for each domain.

    A light in ``members`` is a group light listing those members.
    """
    devices = Devices()
    by_domain: dict[str, list[Entity]] = {}
    for entity_id, state in states.items():
        domain = entity_id.split(".", 1)[0]
        device: _Device
        if domain == "light":
            device = Light(devices, entity_id, state, (members or {}).get(entity_id))
        else:
            device = _DEVICES[domain](devices, entity_id, state)
        by_domain.setdefault(domain, []).append(device)
    for domain, entities in by_domain.items():
        setup_test_component_platform(hass, domain, entities)
        assert await async_setup_component(hass, domain, {domain: {"platform": "test"}})
    await hass.async_block_till_done()
    hass.bus.async_listen(EVENT_CALL_SERVICE, devices.heard)
    return devices
