"""``ha_get_history`` offset/limit pagination (issue #930).

States and statistics are written to Core's recorder and read back through
the tool; every row is Core's. Each entity carries total_count, offset, limit,
count, has_more and next_offset.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_history import HistoryTools

from ._client_spies import sent_messages
from ._recorded_history import import_statistic, record_states, statistics

START = datetime(2024, 1, 1, tzinfo=UTC)
END = START + timedelta(days=7)
SENSOR = "sensor.test"
ENERGY = "sensor.energy"


async def _record(hass: HomeAssistant, count: int, entity_id: str = SENSOR) -> None:
    """``count`` state changes "0", "1", ... one minute apart, oldest first."""
    assert await async_setup_component(hass, "history", {})
    await record_states(
        hass,
        entity_id,
        *((str(i), None, START.timestamp() + 60 * (i + 1)) for i in range(count)),
    )


async def _history(
    client: HomeAssistantClient, entity_ids: str | list[str] = SENSOR, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(
        entity_ids=entity_ids,
        start_time=START.isoformat(),
        end_time=END.isoformat(),
        **kwargs,
    )
    return result


async def _import_rows(hass: HomeAssistant, count: int) -> None:
    """``count`` hourly statistics rows whose mean is the row's position."""
    await import_statistic(
        hass,
        ENERGY,
        "kWh",
        "energy",
        [
            {"start": START + timedelta(hours=i), "mean": float(i), "sum": float(i)}
            for i in range(count)
        ],
        mean=True,
    )


def _entity(result: dict[str, Any]) -> dict[str, Any]:
    [entity] = result["data"]["entities"]
    return entity


# --- history source ---------------------------------------------------------


async def test_history_first_page_reports_the_next_offset(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, 20)

    entity = _entity(await _history(core_client, limit=5))

    assert len(entity["states"]) == 5
    assert entity["count"] == 5
    assert entity["limit"] == 5
    assert entity["total_count"] == 20
    assert entity["offset"] == 0
    assert entity["has_more"] is True
    assert entity["next_offset"] == 5


async def test_history_offset_skips_the_newest_states(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The default order is newest first, so offset=5 starts at the sixth newest."""
    await _record(hass, 20)

    entity = _entity(await _history(core_client, limit=5, offset=5))

    assert [row["state"] for row in entity["states"]] == ["14", "13", "12", "11", "10"]
    assert entity["offset"] == 5


async def test_history_offset_past_the_end_returns_an_empty_last_page(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, 10)

    entity = _entity(await _history(core_client, limit=5, offset=100))

    assert entity["states"] == []
    assert entity["has_more"] is False
    assert entity["next_offset"] is None
    assert entity["total_count"] == 10


async def test_history_partial_last_page_says_there_is_no_more(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, 7)

    entity = _entity(await _history(core_client, limit=5, offset=5))

    assert [row["state"] for row in entity["states"]] == ["1", "0"]
    assert entity["has_more"] is False
    assert entity["next_offset"] is None


async def test_history_without_limit_returns_one_page_of_100(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, 150)

    entity = _entity(await _history(core_client))

    assert entity["count"] == 100
    assert entity["total_count"] == 150
    assert entity["has_more"] is True
    assert entity["next_offset"] == 100


async def test_history_limit_above_the_documented_maximum_is_clamped(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The limit parameter documents a maximum of 1000."""
    await _record(hass, 5)

    entity = _entity(await _history(core_client, limit=1001))

    assert entity["limit"] == 1000
    assert entity["count"] == 5


# --- offset with several entities --------------------------------------------


async def test_offset_with_several_entities_is_refused_before_core_is_asked(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Pages are per entity, so an offset across entities has no single meaning."""
    await _record(hass, 5, "sensor.a")
    await _record(hass, 5, "sensor.b")

    with sent_messages(core_client) as sent, pytest.raises(ToolError) as exc_info:
        await _history(core_client, ["sensor.a", "sensor.b"], offset=1, limit=10)

    error = json.loads(str(exc_info.value))["error"]
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "single entity_id" in error["message"]
    assert not any(
        call.args[0]["type"] == "history/history_during_period"
        for call in sent.await_args_list
    )


async def test_several_entities_page_each_entity_from_offset_zero(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, 5, "sensor.a")
    await _record(hass, 5, "sensor.b")

    result = await _history(core_client, ["sensor.a", "sensor.b"], offset=0, limit=3)

    entities = result["data"]["entities"]
    assert [entity["entity_id"] for entity in entities] == ["sensor.a", "sensor.b"]
    for entity in entities:
        assert entity["offset"] == 0
        assert entity["count"] == 3
        assert entity["total_count"] == 5


# --- statistics source -------------------------------------------------------


async def test_statistics_without_limit_returns_one_page_of_100(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _import_rows(hass, 150)

    entity = _entity(await statistics(core_client, [ENERGY], START, END))

    assert entity["count"] == 100
    assert entity["limit"] == 100
    assert entity["offset"] == 0
    assert entity["total_count"] == 150
    assert entity["has_more"] is True
    assert entity["next_offset"] == 100


async def test_statistics_offset_skips_the_oldest_rows(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _import_rows(hass, 20)

    entity = _entity(
        await statistics(core_client, [ENERGY], START, END, limit=5, offset=10)
    )

    assert [row["mean"] for row in entity["statistics"]] == [
        10.0,
        11.0,
        12.0,
        13.0,
        14.0,
    ]
    assert entity["offset"] == 10


async def test_statistics_offset_past_the_end_returns_an_empty_last_page(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _import_rows(hass, 5)

    entity = _entity(
        await statistics(core_client, [ENERGY], START, END, limit=5, offset=50)
    )

    assert entity["statistics"] == []
    assert entity["has_more"] is False
    assert entity["next_offset"] is None


async def test_statistics_query_params_show_the_defaults_applied(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _import_rows(hass, 5)

    result = await statistics(core_client, [ENERGY], START, END)

    assert result["data"]["query_params"] == {
        "statistic_types": None,
        "limit": 100,
        "offset": 0,
    }


async def test_statistics_query_params_echo_the_caller_values(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _import_rows(hass, 50)

    result = await statistics(
        core_client,
        [ENERGY],
        START,
        END,
        statistic_types=["mean"],
        limit=10,
        offset=5,
    )

    assert result["data"]["query_params"] == {
        "statistic_types": ["mean"],
        "limit": 10,
        "offset": 5,
    }


@pytest.mark.parametrize(
    "statistic_types", ["mean,max", '["mean","max"]'], ids=["comma", "bracketed"]
)
async def test_statistics_query_params_show_string_types_as_a_list(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    statistic_types: str,
) -> None:
    """Regression #990: query_params echoed the raw string while the
    top-level statistic_types held the parsed list."""
    await _import_rows(hass, 5)

    result = await statistics(
        core_client, [ENERGY], START, END, statistic_types=statistic_types
    )

    assert result["data"]["query_params"]["statistic_types"] == ["mean", "max"]
    assert result["data"]["statistic_types"] == ["mean", "max"]
