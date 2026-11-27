"""Core's ``utility_meter`` integration, set up from a config entry as Core's
own tests set it up."""

from __future__ import annotations

from typing import Any

from homeassistant.components.utility_meter.config_flow import ConfigFlowHandler
from homeassistant.components.utility_meter.const import (
    CONF_METER_DELTA_VALUES,
    CONF_METER_NET_CONSUMPTION,
    CONF_METER_OFFSET,
    CONF_METER_PERIODICALLY_RESETTING,
    CONF_METER_TYPE,
    CONF_SENSOR_ALWAYS_AVAILABLE,
    CONF_SOURCE_SENSOR,
    CONF_TARIFFS,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry


def meter_options() -> dict[str, Any]:
    """Every option Core's creation form takes, for a meter named Energy."""
    return {
        CONF_NAME: "Energy",
        CONF_SOURCE_SENSOR: "sensor.power",
        CONF_METER_TYPE: "none",
        # Core's number selector stores the offset as a float.
        CONF_METER_OFFSET: 0.0,
        CONF_TARIFFS: [],
        CONF_METER_NET_CONSUMPTION: False,
        CONF_METER_DELTA_VALUES: False,
        CONF_METER_PERIODICALLY_RESETTING: True,
        CONF_SENSOR_ALWAYS_AVAILABLE: False,
    }


async def set_up_meter(hass: HomeAssistant) -> MockConfigEntry:
    """A loaded meter; ``CONF_METER_TYPE`` (``cycle``) none, so it schedules
    no reset."""
    entry = MockConfigEntry(
        domain="utility_meter",
        title="Energy",
        options=meter_options(),
        version=ConfigFlowHandler.VERSION,
        minor_version=ConfigFlowHandler.MINOR_VERSION,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry
