"""``ha_get_history(source="statistics")`` hands ``statistic_types`` to Core.

The tool refuses only an empty list, which would make Core return rows without
values; every other type list goes to Core's recorder as given, and Core picks
the values it returns or refuses the request. Statistics are imported into
Core's recorder and every reply is Core's.
"""

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._client_spies import sent_messages
from ._recorded_history import import_statistic, statistics

STATISTIC_ID = "sensor.temperature"
START = datetime(2024, 1, 1, tzinfo=UTC)
END = datetime(2024, 1, 2, tzinfo=UTC)


async def _one_hour(hass: HomeAssistant) -> None:
    """One hour of a statistic Core keeps mean, min, max and sum for."""
    hass.states.async_set(STATISTIC_ID, "20", {"unit_of_measurement": "°C"})
    await import_statistic(
        hass,
        STATISTIC_ID,
        "°C",
        "temperature",
        [{"start": START, "mean": 20.0, "min": 19.0, "max": 21.0, "sum": 5.0}],
        mean=True,
    )


def _statistics_requests(sent: Any) -> list[dict[str, Any]]:
    return [
        call.args[0]
        for call in sent.await_args_list
        if call.args[0]["type"] == "recorder/statistics_during_period"
    ]


@pytest.mark.parametrize("statistic_types", [[], "[]"], ids=["list", "string"])
async def test_empty_types_are_refused_before_core_is_asked(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    statistic_types: list[str] | str,
) -> None:
    """Core answers types=[] with rows that carry no values; that is never useful."""
    await _one_hour(hass)

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await statistics(
            core_client, [STATISTIC_ID], START, END, statistic_types=statistic_types
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "statistic_types" in error["message"]
    assert _statistics_requests(sent) == []


async def test_no_types_lets_core_return_every_type(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _one_hour(hass)

    with sent_messages(core_client) as sent:
        result = await statistics(core_client, [STATISTIC_ID], START, END)

    [request] = _statistics_requests(sent)
    assert "types" not in request
    row = result["data"]["entities"][0]["statistics"][0]
    assert {"mean", "min", "max", "sum"} <= row.keys()
    assert {"mean", "min", "max", "sum"} <= set(result["data"]["statistic_types"])


@pytest.mark.parametrize(
    "statistic_types", [["mean", "sum"], "mean,sum"], ids=["list", "comma-separated"]
)
async def test_requested_types_are_the_types_core_returns(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    statistic_types: list[str] | str,
) -> None:
    await _one_hour(hass)

    with sent_messages(core_client) as sent:
        result = await statistics(
            core_client, [STATISTIC_ID], START, END, statistic_types=statistic_types
        )

    [request] = _statistics_requests(sent)
    assert set(request["types"]) == {"mean", "sum"}
    row = result["data"]["entities"][0]["statistics"][0]
    assert row.keys() - {"start", "end"} == {"mean", "sum"}


async def test_core_rejection_of_a_type_is_surfaced(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The tool holds no copied type list: Core judges the type and says why."""
    await _one_hour(hass)

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await statistics(
            core_client, [STATISTIC_ID], START, END, statistic_types=["invalid_type"]
        )

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "invalid_type" in error["message"]
    assert [request["types"] for request in _statistics_requests(sent)] == [
        ["invalid_type"]
    ]
