"""Recorder data written through Core for the history and statistics tool tests."""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from typing import Any

import pytest
from homeassistant.components.recorder import core as recorder_core
from homeassistant.components.recorder import migration
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
    create_engine_test_for_schema_version_postfix,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_history import HistoryTools


@asynccontextmanager
async def migrating(
    async_test_recorder: Any, hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """A recorder started on an older database, held inside its live schema
    migration until the block ends."""
    started = threading.Event()
    stall = threading.Event()
    migrate_live = migration.migrate_schema_live

    def held(*args: Any) -> Any:
        started.set()
        stall.wait(30)
        return migrate_live(*args)

    monkeypatch.setattr(migration, "migrate_schema_live", held)
    monkeypatch.setattr(
        recorder_core,
        "create_engine",
        partial(
            create_engine_test_for_schema_version_postfix,
            hass=hass,
            schema_version_postfix="51",
        ),
    )
    try:
        async with async_test_recorder(
            hass, wait_recorder=False, wait_recorder_setup=False
        ):
            assert await hass.async_add_executor_job(started.wait, 30)
            try:
                yield
            finally:
                # Before the recorder's teardown, which joins its thread.
                stall.set()
            await async_wait_recording_done(hass)
    finally:
        stall.set()


async def record_states(
    hass: HomeAssistant,
    entity_id: str,
    *changes: tuple[str, dict[str, Any] | None, float],
) -> None:
    """Set ``entity_id`` to each ``(state, attributes, timestamp)`` and commit
    the rows to Core's recorder."""
    for state, attributes, timestamp in changes:
        hass.states.async_set(entity_id, state, attributes, timestamp=timestamp)
    await async_wait_recording_done(hass)


async def import_statistic(
    hass: HomeAssistant,
    statistic_id: str,
    unit: str | None,
    unit_class: str | None,
    rows: list[StatisticData],
    *,
    mean: bool = False,
) -> None:
    """Commit hourly ``rows`` of a sum (and, with ``mean``, mean) statistic
    stored in ``unit`` to Core's recorder."""
    metadata: StatisticMetaData = {
        "mean_type": StatisticMeanType.ARITHMETIC if mean else StatisticMeanType.NONE,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": statistic_id,
        "unit_class": unit_class,
        "unit_of_measurement": unit,
    }
    async_import_statistics(hass, metadata, rows)
    await async_wait_recording_done(hass)


async def statistics(
    client: HomeAssistantClient,
    statistic_ids: list[str],
    start: datetime,
    end: datetime,
    **kwargs: Any,
) -> dict[str, Any]:
    """``ha_get_history(source="statistics")`` for one hourly window."""
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(
        entity_ids=statistic_ids,
        source="statistics",
        start_time=start.isoformat(),
        end_time=end.isoformat(),
        period="hour",
        **kwargs,
    )
    return result
