"""Recorder values must be labelled from Core metadata, never current states."""

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder import websocket_api as recorder_websocket_api
from homeassistant.components.recorder.models import (
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_history
from ha_mcp.tools.statistics_helpers import fetch_statistics_metadata

STATISTIC_ID = "sensor.energy"
START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 2, tzinfo=UTC)
UNIT_CLASS = {"kWh": "energy", "MWh": "energy", "%": "unitless", None: None}


async def import_hour(
    hass: HomeAssistant, unit: str | None, total: float, start: datetime = START
) -> None:
    """Commit one hour of a sum statistic stored in ``unit`` to Core's recorder."""
    metadata: StatisticMetaData = {
        "mean_type": StatisticMeanType.NONE,
        "has_sum": True,
        "name": None,
        "source": "recorder",
        "statistic_id": STATISTIC_ID,
        "unit_class": UNIT_CLASS[unit],
        "unit_of_measurement": unit,
    }
    async_import_statistics(hass, metadata, [{"start": start, "sum": total}])
    await async_wait_recording_done(hass)


async def query(
    client: HomeAssistantClient,
    *,
    offset: int = 0,
    statistic_types: tuple[str, ...] | None = ("sum", "change"),
) -> dict[str, Any]:
    """One hourly page of ``ha_get_history(source="statistics")``: the tool's
    data with the warnings it lifts to the top of the response."""
    response = await tools_history.HistoryTools(client).ha_get_history(
        entity_ids=STATISTIC_ID,
        source="statistics",
        start_time=START.isoformat(),
        end_time=END.isoformat(),
        period="hour",
        statistic_types=None if statistic_types is None else list(statistic_types),
        limit=1,
        offset=offset,
    )
    return {**response["data"], "warnings": response.get("warnings", [])}


@pytest.mark.parametrize("stored,display", [("kWh", "kWh"), ("MWh", "kWh")])
async def test_labels_core_converted_values_with_display_unit(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    stored: str,
    display: str,
) -> None:
    hass.states.async_set(STATISTIC_ID, "1", {"unit_of_measurement": display})
    await import_hour(hass, stored, 1250.0 if stored == "kWh" else 1.25)
    direct = await core_client.send_websocket_message(
        {
            "type": "recorder/statistics_during_period",
            "start_time": START.isoformat(),
            "end_time": END.isoformat(),
            "statistic_ids": [STATISTIC_ID],
            "period": "hour",
            "types": ["sum", "change"],
        }
    )
    assert direct["success"] is True

    result = await query(core_client)
    entity = result["entities"][0]
    assert entity["unit_of_measurement"] == "kWh"
    assert entity["unit_source"] == "recorder_metadata"
    assert entity["statistics_metadata"]["statistics_unit_of_measurement"] == stored
    # Core's rows as Core sent them, in the display unit.
    assert entity["statistics"] == direct["result"][STATISTIC_ID]
    assert entity["statistics"][0]["sum"] == 1250.0


@pytest.mark.parametrize("in_window,offset", [(False, 0), (True, 10)])
async def test_unit_is_available_even_when_page_is_empty(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    in_window: bool,
    offset: int,
) -> None:
    hass.states.async_set(STATISTIC_ID, "1", {"unit_of_measurement": "kWh"})
    await import_hour(hass, "kWh", 1.0, START if in_window else START - timedelta(1))
    result = await query(core_client, offset=offset)
    assert result["entities"][0]["statistics"] == []
    assert result["entities"][0]["unit_of_measurement"] == "kWh"


async def test_missing_metadata_keeps_values_and_explains_unknown_unit(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The statistic is cleared between the values read and the metadata read."""
    await import_hour(hass, "kWh", 2.0)

    async def clear_then_read(
        client: Any, statistic_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        cleared = await core_client.send_websocket_message(
            {"type": "recorder/clear_statistics", "statistic_ids": statistic_ids}
        )
        assert cleared["success"] is True
        await async_wait_recording_done(hass)
        return await fetch_statistics_metadata(client, statistic_ids)

    with patch.object(tools_history, "fetch_statistics_metadata", clear_then_read):
        result = await query(core_client)
    entity = result["entities"][0]
    assert entity["statistics"][0]["sum"] == 2.0
    assert entity["unit_of_measurement"] is None
    assert entity["unit_source"] == "unknown"
    assert entity["unit_reason"] == "statistics_metadata_missing"
    assert result["warnings"]


@pytest.mark.parametrize("failure", ["rejected", "timeout"])
async def test_metadata_failure_does_not_discard_numeric_results(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    failure: str,
) -> None:
    await import_hour(hass, "kWh", 2.0)

    async def read_offline(
        client: Any, statistic_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        with patch.object(
            client,
            "send_websocket_message",
            AsyncMock(side_effect=TimeoutError("metadata timeout")),
        ):
            return await fetch_statistics_metadata(client, statistic_ids)

    failing: AbstractContextManager[Any]
    if failure == "rejected":
        # Core answers the metadata command with an error.
        failing = patch.object(
            recorder_websocket_api,
            "async_list_statistic_ids",
            AsyncMock(side_effect=HomeAssistantError("metadata unavailable")),
        )
    else:
        failing = patch.object(tools_history, "fetch_statistics_metadata", read_offline)
    with failing:
        result = await query(core_client)
    entity = result["entities"][0]
    assert entity["statistics"][0]["sum"] == 2.0
    assert entity["unit_of_measurement"] is None
    assert entity["unit_reason"] == "statistics_metadata_unavailable"
    assert result["warnings"]


@pytest.mark.parametrize("stored", [None, "%"])
async def test_unitless_metadata_is_distinguished_from_failed_lookup(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    stored: str | None,
) -> None:
    # A state without a unit: Core displays a percentage as a plain ratio.
    hass.states.async_set(STATISTIC_ID, "2")
    await import_hour(hass, stored, 2.0)
    result = await query(core_client)
    entity = result["entities"][0]
    assert entity["statistics_metadata"]["display_unit_of_measurement"] is None
    assert entity["unit_of_measurement"] is None
    assert entity["unit_source"] == "recorder_metadata"
    assert entity["unit_reason"] == "statistics_are_unitless"


async def test_import_committing_between_reads_never_yields_rows_without_unit(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core commits an import's rows and metadata together; here, after our first read."""
    hass.states.async_set(STATISTIC_ID, "1", {"unit_of_measurement": "kWh"})

    async def import_then_read(
        client: Any, statistic_ids: list[str]
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        await import_hour(hass, "kWh", 1.0)
        return await fetch_statistics_metadata(client, statistic_ids)

    with patch.object(tools_history, "fetch_statistics_metadata", import_then_read):
        result = await query(core_client, statistic_types=None)

    entity = result["entities"][0]
    assert not entity["statistics"] or entity["unit_of_measurement"] == "kWh", entity
