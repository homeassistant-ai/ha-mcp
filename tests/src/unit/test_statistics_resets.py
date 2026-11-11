"""Never expose value-scaled reset timestamps when native recovery is unavailable."""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.statistics_resets import restore_reset_timestamps

STATISTIC_ID = "sensor.energy"
START = datetime(2024, 1, 1, 0, tzinfo=UTC)
LAST_RESET = datetime(2023, 12, 31, 0, tzinfo=UTC)
# The query the tool sends; the failing variants leave out what Core requires.
QUERY: dict[str, Any] = {
    "period": "hour",
    "start_time": "2024-01-01T00:00:00+00:00",
    "units": {"energy": "kWh"},
}


@pytest.mark.parametrize("failure", ["not_asked", "rejected", "timeout", "empty"])
async def test_unrecoverable_timestamp_is_omitted_without_losing_values(
    failure: str,
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hass.states.async_set(STATISTIC_ID, "1", {"unit_of_measurement": "kWh"})
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": STATISTIC_ID,
            "unit_class": "energy",
            "unit_of_measurement": "MWh",
        },
        [{"start": START, "last_reset": LAST_RESET, "sum": 123}],
    )
    await async_wait_recording_done(hass)

    fetched = await core_client.send_websocket_message(
        {
            **QUERY,
            "type": "recorder/statistics_during_period",
            "statistic_ids": [STATISTIC_ID],
            "types": ["last_reset", "sum"],
        }
    )
    assert fetched["success"] is True
    rows = fetched["result"]
    assert rows[STATISTIC_ID][0]["last_reset"] is not None
    values = {k: v for k, v in rows[STATISTIC_ID][0].items() if k != "last_reset"}

    metadata: dict[str, dict[str, Any]] = {}
    query = dict(QUERY)
    if failure != "not_asked":
        listed = await core_client.send_websocket_message(
            {
                "type": "recorder/get_statistics_metadata",
                "statistic_ids": [STATISTIC_ID],
            }
        )
        metadata = {record["statistic_id"]: record for record in listed["result"]}
    if failure == "rejected":
        # Core refuses a statistics query without a start_time.
        del query["start_time"]
    elif failure == "empty":
        # No hour of the statistic falls in this window, so Core answers {}.
        query["start_time"] = "2100-01-01T00:00:00+00:00"
    elif failure == "timeout":
        monkeypatch.setattr(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=TimeoutError("offline")),
        )

    with patch.object(
        core_client,
        "send_websocket_message",
        wraps=core_client.send_websocket_message,
    ) as sent:
        warnings = await restore_reset_timestamps(core_client, rows, metadata, query)
    assert rows == {STATISTIC_ID: [values]}
    assert "last_reset omitted" in warnings[0]
    if failure == "not_asked":
        sent.assert_not_called()
    else:
        sent.assert_awaited_once()
        assert sent.await_args is not None
        request = sent.await_args.args[0]
        # The recovery asks Core for the same statistic in its stored unit.
        assert request["type"] == "recorder/statistics_during_period"
        assert request["statistic_ids"] == [STATISTIC_ID]
        assert request["types"] == ["last_reset"]
        assert request["units"] == {"energy": "MWh"}
