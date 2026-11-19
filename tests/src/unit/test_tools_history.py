"""``ha_get_history`` failures, recorder workload guardrails, ``fields`` and ``order``.

Every reply is Core's: the tool runs on ``core_client``, Home Assistant's
timezone comes from Core's ``/api/config``, and the queries that run read
Core's recorder. A refused query is one whose recorder command never reaches
Core, which the ``sent`` spy shows.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.config import reset_global_settings
from ha_mcp.tools.tools_history import HistoryTools

from ._ha_timezone import unresolvable_zone
from ._recorded_history import import_statistic, record_states, statistics

START = datetime(2026, 1, 1, tzinfo=UTC)
RECORDER_QUERIES = frozenset(
    {"history/history_during_period", "recorder/statistics_during_period"}
)
# Over the 25-entity statistics ceiling: the refusal then reports the
# calendar-aligned scan window whatever its length.
TOO_MANY_STATISTICS = [f"sensor.test_{index}" for index in range(26)]


def _enable_guardrails(monkeypatch: pytest.MonkeyPatch, enabled: bool = True) -> None:
    """Set the Advanced setting and publish it as the settings UI does."""
    monkeypatch.setenv(
        "HAMCP_ENABLE_HISTORY_QUERY_GUARDRAILS", "true" if enabled else "false"
    )
    reset_global_settings()


@pytest.fixture
def guardrails(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_guardrails(monkeypatch)


@pytest.fixture
async def utc(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


@pytest.fixture
async def recorded(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Core's recorder and history."""
    assert await async_setup_component(hass, "history", {})


def _no_answer(client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Home Assistant's ``/api/config`` gives no answer."""
    monkeypatch.setattr(
        client,
        "get_config",
        AsyncMock(side_effect=HomeAssistantConnectionError("no answer")),
    )


def _recorder_queries(sent: AsyncMock) -> list[str]:
    return [
        call.args[0]["type"]
        for call in sent.await_args_list
        if call.args[0]["type"] in RECORDER_QUERIES
    ]


async def _history(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(**kwargs)
    return result


async def _refusal(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc_info:
        await HistoryTools(client).ha_get_history(**kwargs)
    response: dict[str, Any] = json.loads(str(exc_info.value))
    return response


def _window(days: int) -> dict[str, str]:
    return {
        "start_time": START.isoformat(),
        "end_time": (START + timedelta(days=days)).isoformat(),
    }


# --- failures ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "hint"),
    [("statistics", "state_class"), ("history", "entity ids")],
)
async def test_lost_connection_suggests_the_check_for_the_source(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    hint: str,
) -> None:
    """Statistics need a state_class; raw history only needs the entity to exist."""
    monkeypatch.setattr(
        core_client,
        "send_websocket_message",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection lost")),
    )

    response = await _refusal(core_client, entity_ids="sensor.test", source=source)

    suggestions = " ".join(response["error"]["suggestions"]).lower()
    assert hint in suggestions
    assert ("state_class" in suggestions) is (source == "statistics")


async def test_history_disabled_in_core_is_a_service_failure(
    core_client: HomeAssistantClient,
) -> None:
    """Without Core's history integration the command is unknown to Core."""
    response = await _refusal(core_client, entity_ids="sensor.temp")

    assert response["error"]["code"] == "SERVICE_CALL_FAILED"


@pytest.mark.parametrize("fields", [123, '["'], ids=["not-a-list", "bad-json"])
async def test_malformed_fields_are_refused(
    core_client: HomeAssistantClient, fields: Any
) -> None:
    response = await _refusal(core_client, entity_ids="sensor.temp", fields=fields)

    assert response["error"]["code"] == "VALIDATION_FAILED"
    assert response["parameter"] == "fields"


# --- time range validation ---------------------------------------------------


@pytest.mark.parametrize("enabled", [True, False], ids=["guarded", "unguarded"])
async def test_reversed_time_range_is_refused_before_core_is_asked(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    _enable_guardrails(monkeypatch, enabled)

    response = await _refusal(
        core_client,
        entity_ids="sensor.temp",
        start_time="2026-01-02T00:00:00Z",
        end_time="2026-01-01T00:00:00Z",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails")
async def test_reversed_calendar_range_is_reported_even_when_ha_is_unreachable(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's own mistake is named, not a connection failure from the
    timezone lookup that a valid range would need."""
    _no_answer(core_client, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.temp",
        source="statistics",
        start_time="2026-01-02T00:00:00Z",
        end_time="2026-01-01T00:00:00Z",
        period="day",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails")
async def test_zero_length_window_is_refused_with_guardrails_on(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    response = await _refusal(
        core_client,
        entity_ids="sensor.temp",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:00:00Z",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("recorded")
async def test_zero_length_window_runs_with_guardrails_off(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await _history(core_client, entity_ids="sensor.temp", start_time="0h")

    assert result["data"]["success"] is True
    assert _recorder_queries(sent) == ["history/history_during_period"]


@pytest.mark.usefixtures("guardrails")
async def test_future_start_without_end_names_start_time(
    core_client: HomeAssistantClient,
) -> None:
    """The caller gave no end_time, so the message must not blame one."""
    error = (
        await _refusal(
            core_client, entity_ids="sensor.test", start_time="9998-01-01T00Z"
        )
    )["error"]

    assert "start_time" in error["message"]
    assert "end_time" not in error["suggestion"]


@pytest.mark.usefixtures("guardrails")
async def test_last_calendar_year_is_refused_even_when_ha_is_unreachable(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Year 9999 cannot be aligned to the next calendar boundary."""
    _no_answer(core_client, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        source="statistics",
        start_time="9999-12-30T00:00:00Z",
        end_time="9999-12-31T00:00:00Z",
        period="day",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_overflowing_relative_time_is_a_validation_error(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    response = await _refusal(
        core_client, entity_ids="sensor.test", start_time="99999999d"
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["parameter"] == "start_time"
    assert _recorder_queries(sent) == []


# --- raw history budget ------------------------------------------------------


@pytest.mark.usefixtures("guardrails")
async def test_raw_history_over_the_entity_hour_budget_is_refused(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    response = await _refusal(
        core_client, entity_ids=["sensor.one", "sensor.two"], **_window(6)
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["estimated_entity_hours"] == 2 * 6 * 24
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails", "recorded")
async def test_relative_window_exactly_at_the_budget_runs(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Ten days of one entity is the 240 entity-hour budget, not over it."""
    result = await _history(core_client, entity_ids="sensor.temp", start_time="10d")

    assert result["data"]["success"] is True
    assert _recorder_queries(sent) == ["history/history_during_period"]


@pytest.mark.usefixtures("guardrails")
async def test_history_refusal_names_the_detail_flags_that_raise_the_cost(
    core_client: HomeAssistantClient,
) -> None:
    response = await _refusal(
        core_client,
        entity_ids="sensor.temp",
        minimal_response=False,
        significant_changes_only=False,
        **_window(2),
    )

    assert response["minimal_response"] is False
    assert response["significant_changes_only"] is False
    suggestions = response["error"]["suggestions"]
    assert any("minimal_response=true" in item for item in suggestions)
    assert any("significant_changes_only=true" in item for item in suggestions)


@pytest.mark.usefixtures("guardrails")
async def test_history_of_more_than_ten_entities_is_refused(
    core_client: HomeAssistantClient,
) -> None:
    response = await _refusal(
        core_client,
        entity_ids=[f"sensor.test_{index}" for index in range(11)],
        start_time="1h",
    )

    assert response["entity_count"] == 11
    assert response["max_entities"] == 10


@pytest.mark.usefixtures("recorded")
async def test_large_history_query_runs_when_guardrails_are_left_off(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Guardrails are opt-in, and the query goes over the shared connection."""
    result = await _history(core_client, entity_ids="sensor.temp", **_window(31))

    assert result["data"]["success"] is True
    assert _recorder_queries(sent) == ["history/history_during_period"]


@pytest.mark.usefixtures("recorded")
async def test_turning_guardrails_on_applies_to_the_next_query(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await _history(core_client, entity_ids="sensor.temp", **_window(31))
    _enable_guardrails(monkeypatch)

    response = await _refusal(core_client, entity_ids="sensor.temp", **_window(31))

    assert first["data"]["success"] is True
    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert _recorder_queries(sent) == ["history/history_during_period"]


# --- statistics budget -------------------------------------------------------


@pytest.mark.usefixtures("guardrails")
async def test_5minute_statistics_over_the_row_budget_suggest_hourly(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    response = await _refusal(
        core_client,
        entity_ids=["sensor.one", "sensor.two"],
        source="statistics",
        period="5minute",
        **_window(30),
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["estimated_rows"] > response["max_estimated_rows"]
    suggestions = response["error"]["suggestions"]
    assert any("period='hour'" in item for item in suggestions)
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails")
async def test_statistics_of_more_than_25_entities_are_refused(
    core_client: HomeAssistantClient,
) -> None:
    response = await _refusal(
        core_client,
        entity_ids=TOO_MANY_STATISTICS,
        source="statistics",
        start_time="1h",
        period="hour",
    )

    assert response["entity_count"] == 26
    assert response["max_entities"] == 25


@pytest.mark.usefixtures("guardrails", "utc")
async def test_month_statistics_count_every_hour_of_the_months_touched(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    """Two minutes across a month boundary scan all of January and February."""
    response = await _refusal(
        core_client,
        entity_ids=[f"sensor.test_{index}" for index in range(15)],
        source="statistics",
        start_time="2026-01-31T23:59:00Z",
        end_time="2026-02-01T00:01:00Z",
        period="month",
    )

    assert response["estimated_rows"] == 15 * (31 + 28) * 24
    assert response["scan_granularity_minutes"] == 60
    assert response["scan_start_time"] == "2026-01-01T00:00:00+00:00"
    assert response["scan_end_time"] == "2026-03-01T00:00:00+00:00"
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails", "utc")
async def test_year_statistics_count_every_hour_of_the_years_touched(
    core_client: HomeAssistantClient,
) -> None:
    """Coarser periods do not shrink the scan, so none is suggested."""
    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        source="statistics",
        start_time="2025-12-31T23:59:00Z",
        end_time="2027-01-01T00:01:00Z",
        period="year",
    )

    assert response["estimated_rows"] == 3 * 365 * 24
    assert response["scan_start_time"] == "2025-01-01T00:00:00+00:00"
    assert response["scan_end_time"] == "2028-01-01T00:00:00+00:00"
    assert not any("period" in item for item in response["error"]["suggestions"])


@pytest.mark.usefixtures("guardrails", "utc")
async def test_week_statistics_scan_whole_weeks_from_monday(
    core_client: HomeAssistantClient,
) -> None:
    response = await _refusal(
        core_client,
        entity_ids=TOO_MANY_STATISTICS,
        source="statistics",
        start_time="2026-01-07T12:00:00Z",
        end_time="2026-01-08T12:00:00Z",
        period="week",
    )

    assert response["scan_start_time"] == "2026-01-05T00:00:00+00:00"
    assert response["scan_end_time"] == "2026-01-12T00:00:00+00:00"


@pytest.mark.usefixtures("guardrails", "utc")
async def test_december_month_statistics_scan_into_the_next_year(
    core_client: HomeAssistantClient,
) -> None:
    response = await _refusal(
        core_client,
        entity_ids=TOO_MANY_STATISTICS,
        source="statistics",
        start_time="2026-12-15T12:00:00Z",
        end_time="2026-12-16T12:00:00Z",
        period="month",
    )

    assert response["scan_end_time"] == "2027-01-01T00:00:00+00:00"


@pytest.mark.usefixtures("guardrails")
async def test_day_statistics_scan_follows_the_local_dst_day(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The spring-forward day in New York is 23 hours long."""
    await hass.config.async_set_time_zone("America/New_York")

    response = await _refusal(
        core_client,
        entity_ids=TOO_MANY_STATISTICS,
        source="statistics",
        start_time="2026-03-08T12:00:00Z",
        end_time="2026-03-08T13:00:00Z",
        period="day",
    )

    scan = datetime.fromisoformat(response["scan_end_time"]) - datetime.fromisoformat(
        response["scan_start_time"]
    )
    assert scan == timedelta(hours=23)
    assert response["estimated_rows"] == 26 * 23


@pytest.mark.usefixtures("guardrails")
async def test_month_statistics_scan_starts_at_local_midnight(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await hass.config.async_set_time_zone("America/New_York")

    response = await _refusal(
        core_client,
        entity_ids=[f"sensor.test_{index}" for index in range(20)],
        source="statistics",
        start_time="2026-03-08T12:00:00Z",
        end_time="2026-05-08T12:00:00Z",
        period="month",
    )

    assert response["scan_start_time"] == "2026-03-01T05:00:00+00:00"
    assert response["scan_end_time"] == "2026-06-01T04:00:00+00:00"


async def test_a_time_without_an_offset_is_refused_in_a_zone_the_server_cannot_resolve(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading it in UTC instead would move it by the zone's offset."""
    await unresolvable_zone(hass, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        start_time="2026-01-01T00:00:00",
        end_time="2026-01-01T06:00:00",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert _recorder_queries(sent) == []


async def test_a_reversed_range_without_offsets_is_named_when_ha_timezone_is_unreachable(
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two times without an offset compare without Home Assistant's time zone,
    so the caller's own mistake is named, not the failed lookup."""
    _no_answer(core_client, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        start_time="2026-01-02T00:00:00",
        end_time="2026-01-01T00:00:00",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def test_a_reversed_range_without_offsets_is_read_in_ha_timezone(
    core_client: HomeAssistantClient,
) -> None:
    """With the zone at hand, the range is checked as Core reads it."""
    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        start_time="2026-01-02T00:00:00",
        end_time="2026-01-01T00:00:00",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["start_time"] == "2026-01-02T00:00:00-08:00"


@pytest.mark.parametrize(
    "start_time",
    ["2026-01-01T00:00:00", "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"],
    ids=["both_without_offset", "end_without_offset", "end_without_offset_before"],
)
async def test_a_time_without_an_offset_is_refused_when_ha_timezone_is_unreachable(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    start_time: str,
) -> None:
    """Core reads such a time in Home Assistant's time zone; reading it in UTC
    instead would query with that bound moved by the zone's offset. Beside a
    time with an offset, even a day apart, the order is not known without it."""
    _no_answer(core_client, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        start_time=start_time,
        end_time="2026-01-01T06:00:00",
    )

    assert response["error"]["code"] == "CONNECTION_FAILED"
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails")
async def test_calendar_statistics_are_refused_when_ha_timezone_is_unreachable(
    core_client: HomeAssistantClient,
    sent: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Estimating in UTC instead could let an oversized local scan through."""
    _no_answer(core_client, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        source="statistics",
        period="day",
        **_window(1),
    )

    assert response["error"]["code"] == "CONNECTION_FAILED"
    assert _recorder_queries(sent) == []


@pytest.mark.usefixtures("guardrails")
async def test_ha_timezone_the_server_cannot_resolve_is_not_a_connection_failure(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server has no tzdata entry for the zone Home Assistant reports."""
    await unresolvable_zone(hass, monkeypatch)

    response = await _refusal(
        core_client,
        entity_ids="sensor.test",
        source="statistics",
        start_time="1d",
        period="day",
    )

    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["home_assistant_timezone"] == "America/New_York"


@pytest.mark.usefixtures("guardrails", "recorded", "utc")
async def test_two_calendar_years_of_yearly_statistics_run(
    core_client: HomeAssistantClient, sent: AsyncMock
) -> None:
    result = await _history(
        core_client,
        entity_ids="sensor.test",
        source="statistics",
        start_time="2026-01-01T00:00:00Z",
        end_time="2027-01-01T00:00:00Z",
        period="year",
    )

    assert result["data"]["success"] is True
    assert _recorder_queries(sent) == ["recorder/statistics_during_period"]


# --- fields and order --------------------------------------------------------


async def _record_temperatures(hass: HomeAssistant) -> None:
    await record_states(
        hass,
        "sensor.temp",
        *(
            (state, None, START.timestamp() + 60 * minute)
            for minute, state in enumerate(["20", "21", "22"], start=1)
        ),
    )


@pytest.mark.usefixtures("recorded")
async def test_fields_narrow_history_data_to_the_requested_keys(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without fields, data holds the history keys the parameter documents."""
    await _record_temperatures(hass)

    full = await _history(core_client, entity_ids="sensor.temp", **_window(1))
    narrowed = await _history(
        core_client, entity_ids="sensor.temp", fields=["source", "period"], **_window(1)
    )

    assert set(full["data"]) == {
        "success",
        "source",
        "entities",
        "period",
        "query_params",
    }
    assert set(narrowed["data"]) == {"success", "source", "period"}
    assert "metadata" in narrowed


@pytest.mark.usefixtures("recorded")
async def test_unknown_field_is_reported_outside_data(
    core_client: HomeAssistantClient,
) -> None:
    result = await _history(
        core_client, entity_ids="sensor.temp", fields=["nonexistent"], **_window(1)
    )

    assert result["data"]["success"] is True
    assert "warnings" not in result["data"]
    assert any("nonexistent" in warning for warning in result["warnings"])


@pytest.mark.usefixtures("recorded")
async def test_fields_narrow_statistics_data_to_the_requested_keys(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without fields, data holds the statistics keys the parameter documents."""
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [{"start": START, "sum": 1.0}],
    )
    end = START + timedelta(days=1)

    full = await statistics(core_client, ["sensor.energy"], START, end)
    narrowed = await statistics(
        core_client, ["sensor.energy"], START, end, fields=["period_type"]
    )

    assert set(full["data"]) == {
        "success",
        "source",
        "entities",
        "period_type",
        "time_range",
        "statistic_types",
        "query_params",
    }
    assert narrowed["data"] == {"success": True, "period_type": "hour"}


@pytest.mark.usefixtures("recorded")
async def test_order_asc_returns_the_oldest_state_first(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record_temperatures(hass)

    result = await _history(
        core_client, entity_ids="sensor.temp", order="asc", **_window(1)
    )

    [entity] = result["data"]["entities"]
    assert [row["state"] for row in entity["states"]] == ["20", "21", "22"]
