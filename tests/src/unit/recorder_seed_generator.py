"""Write the E2E recorder database to ``RECORDER_SEED_OUT`` with Core's own recorder.

``session_recorder_seed`` runs this module by path. Its name keeps it out of the
unit suite's collection; it lives in the unit tree for that tree's real-Core rules.
"""

import json
import logging
import math
import os
import sqlite3
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sun import STATE_ABOVE_HORIZON, STATE_BELOW_HORIZON
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_FRIENDLY_NAME,
    ATTR_UNIT_OF_MEASUREMENT,
    SUN_EVENT_SUNRISE,
    SUN_EVENT_SUNSET,
    UnitOfEnergy,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

# Inside Core's default purge_keep_days of 10, with room for a session's length.
SPAN = timedelta(days=9, hours=18)
SAMPLE = timedelta(minutes=15)
SEED = Path(__file__).parents[2] / "initial_test_state"
CONFIGURATION = SEED / "configuration.yaml"
RESTORE_STATE = SEED / ".storage" / "core.restore_state"
CORE_CONFIG = SEED / ".storage" / "core.config"
PAGINATION = "input_number.e2e_pagination_seed"
# Core's Sun entity keeps every other attribute out of the recorder.
SUN = {ATTR_FRIENDLY_NAME: "Sun"}

# As Core's demo sensor platform (demo/sensor.py) states them.
OUTSIDE_TEMPERATURE = {
    ATTR_STATE_CLASS: SensorStateClass.MEASUREMENT,
    ATTR_UNIT_OF_MEASUREMENT: UnitOfTemperature.CELSIUS,
    ATTR_DEVICE_CLASS: SensorDeviceClass.TEMPERATURE,
    ATTR_FRIENDLY_NAME: "Outside Temperature",
}
TOTAL_ENERGY_KWH = {
    ATTR_STATE_CLASS: SensorStateClass.TOTAL,
    ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.KILO_WATT_HOUR,
    ATTR_DEVICE_CLASS: SensorDeviceClass.ENERGY,
    ATTR_FRIENDLY_NAME: "Total energy 1",
}
TOTAL_ENERGY_MWH = TOTAL_ENERGY_KWH | {
    ATTR_UNIT_OF_MEASUREMENT: UnitOfEnergy.MEGA_WATT_HOUR,
    ATTR_FRIENDLY_NAME: "Total energy 2",
}
ENERGY = {
    "sensor.total_energy_kwh": TOTAL_ENERGY_KWH,
    "sensor.total_energy_mwh": TOTAL_ENERGY_MWH,
}


@pytest.fixture
def persistent_database() -> bool:
    return True


@pytest.fixture(autouse=True)
def quiet_logging() -> None:
    """Keep Core's SQL log out of a failure's output, which must hold the traceback."""
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def _restored_energy() -> dict[str, float]:
    """The values the demo energy sensors restore at boot."""
    stored = json.loads(RESTORE_STATE.read_text(encoding="utf-8"))["data"]
    return {
        entry["state"]["entity_id"]: float(entry["extra_data"]["native_value"])
        for entry in stored
        if entry["state"]["entity_id"] in ENERGY
    }


def _location() -> dict[str, float]:
    """Where the E2E instance is, so the sun rows continue into its live ones."""
    stored = json.loads(CORE_CONFIG.read_text(encoding="utf-8"))["data"]
    return {key: stored[key] for key in ("latitude", "longitude", "elevation")}


def _sun_states(
    hass: HomeAssistant, start: datetime, now: datetime
) -> list[tuple[datetime, str, str, Mapping[str, Any]]]:
    rows: list[tuple[datetime, str, str, Mapping[str, Any]]] = []
    day = start.date()
    while day <= now.date():
        for event, state in (
            (SUN_EVENT_SUNRISE, STATE_ABOVE_HORIZON),
            (SUN_EVENT_SUNSET, STATE_BELOW_HORIZON),
        ):
            at = get_astral_event_date(hass, event, day)
            if at is not None and start < at < now:
                rows.append((at, "sun.sun", state, SUN))
        day += timedelta(days=1)
    rows.sort(key=lambda row: row[0])
    before = (
        STATE_BELOW_HORIZON
        if rows[0][2] == STATE_ABOVE_HORIZON
        else STATE_ABOVE_HORIZON
    )
    return [(start, "sun.sun", before, SUN), *rows]


def _states(
    now: datetime, helper: Mapping[str, Any], restored: Mapping[str, float]
) -> list[tuple[datetime, str, str, Mapping[str, Any]]]:
    rows = [(now - SAMPLE * k, PAGINATION, str(float(k)), helper) for k in range(1, 13)]
    rows += [
        (now - timedelta(hours=6 * j), PAGINATION, str(float(100 + j)), helper)
        for j in range(1, 37)
    ]
    samples = SPAN // SAMPLE
    for i in range(samples):
        at = now - SAMPLE * (samples - 1 - i)
        hour = at.hour + at.minute / 60
        temperature = 12 + 6 * math.sin(2 * math.pi * (hour - 9) / 24)
        rows.append(
            (
                at,
                "sensor.outside_temperature",
                f"{temperature:.2f}",
                OUTSIDE_TEMPERATURE,
            )
        )
        # Each total rises to the value its live sensor resumes from.
        rows += [
            (at, entity_id, f"{restored[entity_id] * (i + 1) / samples:.9f}", attrs)
            for entity_id, attrs in ENERGY.items()
        ]
    return rows


def _vacuum_into(db_url: str, out: Path) -> None:
    source = sqlite3.connect(db_url.removeprefix("sqlite:///"))
    try:
        source.execute("VACUUM INTO ?", (str(out),))
    finally:
        source.close()
    # A WAL-mode file would grow -wal and -shm sidecars as soon as it is opened.
    copy = sqlite3.connect(out)
    try:
        (mode,) = copy.execute("PRAGMA journal_mode=DELETE").fetchone()
    finally:
        copy.close()
    assert mode == "delete", mode


@pytest.fixture
def copy_after_shutdown(recorder_db_url: str) -> Iterator[None]:
    """Copy the database out once the recorder has shut down and closed its run."""
    yield
    out = Path(os.environ["RECORDER_SEED_OUT"])
    _vacuum_into(recorder_db_url, out)
    assert not any(out.with_name(out.name + s).exists() for s in ("-wal", "-shm"))


@pytest.fixture
def written_at(freezer: FrozenDateTimeFactory) -> datetime:
    """Start the clock SPAN ago, as an instance installed then.

    Core marks statistics compiled up to the moment it creates a database; a
    database created now would leave its latest period, and at :55 its hour, empty.
    """
    now = dt_util.utcnow()
    freezer.move_to(now - SPAN)
    return now


# Fixtures are torn down in reverse: copy_after_shutdown runs after the recorder's.
@pytest.mark.usefixtures("copy_after_shutdown")
async def test_write_recorder_seed(
    written_at: datetime,
    recorder_db_url: str,
    recorder_mock: Recorder,
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
) -> None:
    config = await hass.async_add_executor_job(load_yaml_dict, CONFIGURATION)
    assert await async_setup_component(
        hass, "input_number", {"input_number": config["input_number"]}
    )
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    helper = hass.states.get(PAGINATION)
    assert helper is not None

    restored = await hass.async_add_executor_job(_restored_energy)
    location = await hass.async_add_executor_job(_location)
    hass.config.latitude = location["latitude"]
    hass.config.longitude = location["longitude"]
    hass.config.elevation = int(location["elevation"])
    installed = dt_util.utcnow()
    freezer.move_to(written_at)
    now = written_at
    states = [
        *_states(now, helper.attributes, restored),
        *_sun_states(hass, installed, now),
    ]
    for at, entity_id, state, attributes in sorted(states, key=lambda row: row[0]):
        # The helper's state_info names the attributes its entity keeps out of the recorder.
        info = helper.state_info if entity_id == PAGINATION else None
        hass.states.async_set(
            entity_id, state, attributes, state_info=info, timestamp=at.timestamp()
        )
    await async_wait_recording_done(hass)

    period = (now - SPAN).replace(minute=0, second=0, microsecond=0)
    while period + timedelta(minutes=5) <= now:
        do_adhoc_statistics(hass, start=period)
        period += timedelta(minutes=5)
    await async_wait_recording_done(hass)
