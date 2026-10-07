"""Recorder values must be labelled from Core metadata, never current states."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_history import _fetch_statistics

START = datetime(2026, 9, 1, tzinfo=UTC)
END = datetime(2026, 9, 2, tzinfo=UTC)


def metadata(statistic_id="sensor.energy", stored="kWh", display="kWh"):
    return {
        "statistic_id": statistic_id,
        "statistics_unit_of_measurement": stored,
        "display_unit_of_measurement": display,
        "unit_class": "energy",
        "has_mean": False,
        "mean_type": 0,
        "has_sum": True,
        "source": "recorder",
        "name": None,
    }


async def query(records, rows, *, offset=0, metadata_error=None):
    client = MagicMock()

    async def dispatch(message):
        if message["type"] == "recorder/get_statistics_metadata":
            if isinstance(metadata_error, Exception):
                raise metadata_error
            if metadata_error:
                return {"success": False, "error": metadata_error}
            return {"success": True, "result": records}
        if message["type"] == "recorder/statistics_during_period":
            return {"success": True, "result": {"sensor.energy": rows}}
        raise AssertionError(f"Unexpected command: {message}")

    client.send_websocket_message = AsyncMock(side_effect=dispatch)
    result = await _fetch_statistics(
        client, ["sensor.energy"], START, END, "hour", ["sum", "change"], 1, offset
    )
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,display", [("kWh", "kWh"), ("MWh", "kWh")])
async def test_labels_core_converted_values_with_display_unit(stored, display):
    result = await query([metadata(stored=stored, display=display)], [
        {"start": 1000, "end": 2000, "sum": 1250.0, "change": 250.0}
    ])
    entity = result["entities"][0]
    assert entity["unit_of_measurement"] == "kWh"
    assert entity["unit_source"] == "recorder_metadata"
    assert entity["statistics_metadata"]["statistics_unit_of_measurement"] == stored
    assert entity["statistics"] == [
        {"start": 1000, "end": 2000, "sum": 1250.0, "change": 250.0}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,offset", [([], 0), ([{"start": 1000, "sum": 1.0}], 10)])
async def test_unit_is_available_even_when_page_is_empty(rows, offset):
    result = await query([metadata()], rows, offset=offset)
    assert result["entities"][0]["statistics"] == []
    assert result["entities"][0]["unit_of_measurement"] == "kWh"


@pytest.mark.asyncio
async def test_missing_metadata_keeps_values_and_explains_unknown_unit():
    result = await query([], [{"start": 1000, "sum": 2.0, "unit_of_measurement": "wrong"}])
    entity = result["entities"][0]
    assert entity["statistics"][0]["sum"] == 2.0
    assert entity["unit_of_measurement"] is None
    assert entity["unit_source"] == "unknown"
    assert entity["unit_reason"] == "statistics_metadata_missing"
    assert result["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{"code": "unknown_command"}, TimeoutError("metadata timeout")])
async def test_metadata_failure_does_not_discard_numeric_results(failure):
    result = await query([], [{"start": 1000, "sum": 2.0}], metadata_error=failure)
    entity = result["entities"][0]
    assert entity["statistics"][0]["sum"] == 2.0
    assert entity["unit_of_measurement"] is None
    assert entity["unit_reason"] == "statistics_metadata_unavailable"
    assert result["warnings"]


@pytest.mark.asyncio
async def test_stored_unit_alone_is_not_assumed_to_be_output_unit():
    record = metadata(stored="MWh")
    del record["display_unit_of_measurement"]
    result = await query([record], [{"start": 1000, "sum": 1250.0}])
    assert result["entities"][0]["unit_of_measurement"] is None
    assert result["entities"][0]["unit_reason"] == "display_unit_not_reported"


@pytest.mark.asyncio
async def test_unitless_metadata_is_distinguished_from_failed_lookup():
    result = await query([metadata(stored=None, display=None)], [{"start": 1000, "sum": 2.0}])
    entity = result["entities"][0]
    assert entity["unit_of_measurement"] is None
    assert entity["unit_source"] == "recorder_metadata"
    assert entity["unit_reason"] == "statistics_are_unitless"
