"""``ha_get_history`` says why the recorder returned less than the window asked for.

The recorder is Core's, holding states and statistics written at fixed times;
its purge settings, oldest state and filter are read from it, through the
component where a test takes ``component_core_client``.
"""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import ValidationIssue
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration, mock_platform
from pytest_homeassistant.components.recorder.common import (
    async_wait_purge_done,
    async_wait_recording_done,
    do_adhoc_statistics,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_history
from ha_mcp.tools.component_api import COMPONENT_VIA_MARKETPLACE
from ha_mcp.tools.tools_history import HistoryTools

from ._client_spies import sent_messages
from ._command_faults import before_handling, unanswered
from ._recorded_history import import_statistic, migrating, record_states, statistics

START = datetime(2026, 1, 1, tzinfo=UTC)
ENTITY = "sensor.outside"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def _at(moment: datetime) -> float:
    return moment.timestamp()


async def _history(
    client: HomeAssistantClient, start: datetime, end: datetime, **kwargs: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(
        **{"entity_ids": ENTITY, **kwargs},
        start_time=start.isoformat(),
        end_time=end.isoformat(),
    )
    return result


def _warnings(result: dict[str, Any]) -> str:
    return " ".join(result.get("warnings", []))


@pytest.mark.parametrize("read", ["history", "statistics summary"])
async def test_without_the_component_a_read_names_the_component_it_lacks(
    recorded: Recorder, core_client: HomeAssistantClient, read: str
) -> None:
    if read == "history":
        result = await _history(core_client, START, START + HOUR)
    else:
        result = await _summary(core_client, entity_ids=ENTITY)
    assert COMPONENT_VIA_MARKETPLACE in _warnings(result)


@pytest.fixture
async def recorded(recorder_mock: Recorder, hass: HomeAssistant) -> Recorder:
    """Core's recorder and history, in UTC."""
    await hass.config.async_set_time_zone("UTC")
    assert await async_setup_component(hass, "history", {})
    return recorder_mock


async def test_a_window_before_the_oldest_state_names_it_and_the_purge_settings(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Core answers a window it holds nothing for with an empty success."""
    await record_states(hass, ENTITY, ("5", None, _at(START + 10 * DAY)))

    result = await _history(component_core_client, START, START + DAY)

    assert result["data"]["entities"][0]["states"] == []
    warnings = _warnings(result)
    assert f"No recorded states in this window for: {ENTITY}." in warnings
    assert (START + 10 * DAY).isoformat() in warnings
    assert f"purge_keep_days: {recorded.keep_days}" in warnings
    assert f"auto_purge: {recorded.auto_purge}" in warnings


@pytest.mark.parametrize(
    "recorder_config", [{"purge_keep_days": 3, "auto_purge": False}]
)
async def test_the_purge_settings_reported_are_the_configured_ones(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await record_states(hass, ENTITY, ("5", None, _at(START + 10 * DAY)))

    warnings = _warnings(await _history(component_core_client, START, START + DAY))

    assert "purge_keep_days: 3" in warnings
    assert "auto_purge: False" in warnings


async def test_a_purge_moves_the_oldest_state_that_is_reported(
    recorded: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
) -> None:
    # The purge keeps a number of days counted back from this instant.
    with freeze_time(START + 60 * DAY):
        await record_states(hass, ENTITY, ("5", None, _at(START)))
        hass.states.async_set(ENTITY, "6")
        await async_wait_recording_done(hass)
        await hass.services.async_call(
            "recorder", "purge", {"keep_days": recorded.keep_days}, blocking=True
        )
        await async_wait_purge_done(hass)

    result = await _history(component_core_client, START - DAY, START + DAY)

    assert result["data"]["entities"][0]["states"] == []
    oldest = recorded.states_manager.oldest_ts
    assert oldest is not None and oldest > _at(START)
    assert datetime.fromtimestamp(oldest, UTC).isoformat(
        timespec="seconds"
    ) in _warnings(result)


async def test_history_that_starts_after_start_time_says_when(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Rows alone do not show that the window asked for more."""
    await record_states(hass, ENTITY, ("5", None, _at(START + HOUR)))

    result = await _history(component_core_client, START, START + 2 * HOUR)

    assert len(result["data"]["entities"][0]["states"]) == 1
    assert (
        f"Recorded states start after start_time for: {ENTITY} "
        f"(from {(START + HOUR).isoformat()})"
    ) in _warnings(result)


async def test_a_query_without_the_window_start_state_is_not_called_short(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Asked for no state at the window start, Core returns only later rows."""
    await record_states(
        hass, ENTITY, ("5", None, _at(START - HOUR)), ("6", None, _at(START + HOUR))
    )

    result = await _history(
        component_core_client,
        START,
        START + 2 * HOUR,
        core_options={"include_start_time_state": False},
    )

    assert len(result["data"]["entities"][0]["states"]) == 1
    assert "warnings" not in result


EXCLUDED = pytest.mark.parametrize(
    "recorder_config", [{"exclude": {"entities": [ENTITY]}}]
)


async def _unrecorded(hass: HomeAssistant, client: HomeAssistantClient) -> str:
    hass.states.async_set(ENTITY, "5")
    await async_wait_recording_done(hass)
    return _warnings(await _history(client, START - DAY, START + 400 * DAY))


@EXCLUDED
async def test_an_entity_the_recorder_excludes_is_named(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    with sent_messages(component_core_client) as sent:
        warnings = await _unrecorded(hass, component_core_client)
    assert f"The recorder does not record: {ENTITY}." in warnings
    assert "Marketplace" not in warnings
    assert all(
        call.args[0]["type"] != "recorder/entity_options/get"
        for call in sent.call_args_list
    )


@EXCLUDED
async def test_without_the_component_core_names_an_excluded_entity(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's own ``recorder/entity_options/get`` carries the filter's verdict."""
    assert f"The recorder does not record: {ENTITY}." in await _unrecorded(
        hass, core_client
    )


async def test_an_entity_id_without_a_state_is_named(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await record_states(hass, ENTITY, ("5", None, _at(START)))

    result = await _history(
        component_core_client, START, START + DAY, entity_ids="sensor.outsdie"
    )

    assert "No entity has a state under: sensor.outsdie" in _warnings(result)


async def test_without_the_component_the_unread_purge_settings_are_named(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await record_states(hass, ENTITY, ("5", None, _at(START + 10 * DAY)))

    warnings = _warnings(await _history(core_client, START, START + DAY))

    assert f"No recorded states in this window for: {ENTITY}." in warnings
    assert "purge settings were not read" in warnings
    assert "ha_mcp_tools component" in warnings


async def test_a_refusal_carries_cores_code_and_no_retention_guess(
    recorded: Recorder, core_client: HomeAssistantClient
) -> None:
    """Core refuses an entity ID it cannot parse; the recorder's retention has
    nothing to do with it."""
    with pytest.raises(ToolError) as refusal:
        await _history(core_client, START, START + DAY, entity_ids="not an entity id")

    response = json.loads(str(refusal.value))
    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["ha_error_code"] == "invalid_entity_ids"
    assert "retention" not in str(refusal.value)


# --- statistics ----------------------------------------------------------------


async def test_a_statistic_the_recorder_does_not_have_is_told_from_an_empty_window(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await import_statistic(
        hass, "sensor.energy", "kWh", "energy", [{"start": START, "sum": 1.0}]
    )

    result = await statistics(
        core_client,
        ["sensor.energy", "sensor.enrgy"],
        START + 30 * DAY,
        START + 31 * DAY,
    )

    assert "statistics converter could not be read" in result["warnings"][0]
    assert result["warnings"][1:] == [
        "The recorder has no statistics under: sensor.enrgy.",
        "No statistics in this window for: sensor.energy.",
    ]


async def test_cores_own_statistics_issue_explains_an_empty_statistic(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core validates each sensor statistic; one whose entity has no state gets
    its ``no_state`` issue."""
    assert await async_setup_component(hass, "sensor", {})
    await import_statistic(
        hass, "sensor.gone", "kWh", "energy", [{"start": START, "sum": 1.0}]
    )

    result = await statistics(
        core_client, ["sensor.gone"], START + 30 * DAY, START + 31 * DAY
    )

    assert "sensor.gone: Core reports the statistics issue no_state" in _warnings(
        result
    )


async def _late_five_minute_statistics(
    hass: HomeAssistant, client: HomeAssistantClient
) -> dict[str, Any]:
    """5-minute statistics of sensor.power that begin an hour into the window."""
    assert await async_setup_component(hass, "sensor", {})
    measured = {"state_class": "measurement", "unit_of_measurement": "W"}
    await record_states(
        hass,
        "sensor.power",
        ("10", measured, _at(START + HOUR)),
        ("20", measured, _at(START + HOUR + timedelta(minutes=2))),
    )
    do_adhoc_statistics(hass, start=START + HOUR)
    await async_wait_recording_done(hass)
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(
        entity_ids="sensor.power",
        source="statistics",
        period="5minute",
        start_time=START.isoformat(),
        end_time=(START + 2 * HOUR).isoformat(),
    )
    return result


async def test_five_minute_statistics_that_start_late_say_when(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The recorder purges its 5-minute statistics; the hourly ones stay."""
    result = await _late_five_minute_statistics(hass, component_core_client)

    assert result["data"]["entities"][0]["total_count"] == 1
    assert (
        "5-minute statistics start after start_time for: sensor.power "
        f"(from {(START + HOUR).isoformat()})"
    ) in _warnings(result)


async def test_late_five_minute_statistics_without_the_component_name_what_is_unread(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    result = await _late_five_minute_statistics(hass, core_client)

    assert "purge settings were not read" in _warnings(result)


async def test_a_statistics_summary_is_cores_total_over_the_window(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    sums = [10.0, 12.5, 17.0, 20.0]
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [
            {"start": START + hour * HOUR, "sum": total}
            for hour, total in enumerate(sums)
        ],
    )
    with sent_messages(core_client) as sent:
        result = await HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.energy",
            source="statistics",
            summary=True,
            statistic_types="change",
            start_time=(START + HOUR).isoformat(),
            end_time=(START + 4 * HOUR).isoformat(),
        )

    entity = result["data"]["entities"][0]
    assert entity["summary"] == {"change": sums[-1] - sums[0]}
    assert entity["unit_of_measurement"] == "kWh"
    assert "recorder/statistic_during_period" in [
        call.args[0]["type"] for call in sent.await_args_list
    ]


async def test_a_last_reset_comes_back_as_core_sends_it(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    """One recorder query: no second read to rebuild what Core already returns."""
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [{"start": START, "sum": 1.0, "last_reset": START - DAY}],
    )
    with sent_messages(core_client) as sent:
        result = await statistics(
            core_client,
            ["sensor.energy"],
            START,
            START + HOUR,
            core_options={"units": {"energy": "Wh"}},
        )

    row = result["data"]["entities"][0]["statistics"][0]
    assert row["last_reset"] == _at(START - DAY) * 1000
    assert row["sum"] == 1000.0
    assert [
        call.args[0]["type"]
        for call in sent.await_args_list
        if call.args[0]["type"] == "recorder/statistics_during_period"
    ] == ["recorder/statistics_during_period"]


# --- a reason that cannot be read never costs the rows --------------------------


async def test_rows_survive_cores_recorded_check_getting_no_answer(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await record_states(hass, ENTITY, ("5", None, _at(START + HOUR)))

    async with unanswered(hass, "recorder/entity_options/get", monkeypatch):
        result = await _history(core_client, START, START + 2 * HOUR)

    assert len(result["data"]["entities"][0]["states"]) == 1
    assert f"Whether the recorder records these was not read: {ENTITY}" in _warnings(
        result
    )


async def test_an_empty_statistic_survives_unread_issues(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core validates every statistic for the issues, which can outlast the wait."""
    async with unanswered(hass, "recorder/validate_statistics", monkeypatch):
        result = await statistics(
            core_client, ["sensor.enrgy"], START + 30 * DAY, START + 31 * DAY
        )

    assert result["data"]["entities"][0]["statistics"] == []
    assert "Core's statistics issues were not read" in _warnings(result)


async def test_an_issue_without_data_is_still_reported(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's ValidationIssue carries no data unless a platform gives it some."""

    def issues(hass: HomeAssistant, units: Any) -> dict[str, list[ValidationIssue]]:
        return {"sensor.enrgy": [ValidationIssue("odd_statistic")]}

    mock_integration(hass, MockModule("odd_meter"))
    mock_platform(
        hass, "odd_meter.recorder", SimpleNamespace(validate_statistics=issues)
    )
    assert await async_setup_component(hass, "odd_meter", {})
    await async_wait_recording_done(hass)

    result = await statistics(
        core_client, ["sensor.enrgy"], START + 30 * DAY, START + 31 * DAY
    )

    assert "sensor.enrgy: Core reports the statistics issue odd_statistic" in _warnings(
        result
    )


async def test_unread_metadata_does_not_claim_the_statistic_exists(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with unanswered(hass, "recorder/get_statistics_metadata", monkeypatch):
        result = await statistics(
            core_client, ["sensor.enrgy"], START + 30 * DAY, START + 31 * DAY
        )

    warnings = _warnings(result)
    assert "No statistics in this window" not in warnings
    assert "it is unknown whether they exist" in warnings


async def test_a_removed_entity_whose_rows_came_back_is_not_called_a_wrong_id(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    await record_states(hass, ENTITY, ("5", None, _at(START + HOUR)))
    hass.states.async_remove(ENTITY)
    await async_wait_recording_done(hass)

    result = await _history(component_core_client, START, START + 2 * HOUR)

    assert result["data"]["entities"][0]["states"]
    assert "No entity has a state under" not in _warnings(result)


async def test_five_minute_statistics_carry_their_own_first_row(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    result = await _late_five_minute_statistics(hass, component_core_client)

    assert f"purge_keep_days: {recorded.keep_days}" in _warnings(result)
    assert (
        f"First short-term statistic for sensor.power: {(START + HOUR).isoformat()}"
        in _warnings(result)
    )


async def test_a_recorder_without_states_says_so(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    result = await _history(component_core_client, START, START + DAY)

    assert "The recorder holds no states." in _warnings(result)


# --- statistics summary -----------------------------------------------------------


async def _summary(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    result: dict[str, Any] = await HistoryTools(client).ha_get_history(
        source="statistics",
        summary=True,
        start_time=START.isoformat(),
        end_time=(START + 4 * HOUR).isoformat(),
        **kwargs,
    )
    return result


# Noon on the recorded day; ticking, since the client waits on the clock.
@pytest.mark.freeze_time(START + 12 * HOUR, tick=True)
@pytest.mark.parametrize(
    ("native_period", "hours"),
    [
        ({"calendar": {"period": "day"}}, (0, 24)),
        # The ticking clock is past noon, so the 08:00 hour starts before the window.
        ({"rolling_window": {"duration": {"hours": 4}}}, (9, 12)),
    ],
)
async def test_a_summary_over_cores_own_period_totals_that_period(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    native_period: dict[str, Any],
    hours: tuple[int, int],
) -> None:
    """The tool's own window, the first four hours, gives another total."""
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [{"start": START + hour * HOUR, "sum": hour * hour} for hour in range(10)],
    )

    async def change(**kwargs: Any) -> Any:
        result = await HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.energy",
            source="statistics",
            summary=True,
            statistic_types="change",
            **kwargs,
        )
        return result["data"]["entities"][0]["summary"]["change"]

    first_four_hours = {
        "start_time": START.isoformat(),
        "end_time": (START + 4 * HOUR).isoformat(),
    }
    native = await change(**first_four_hours, core_options=native_period)

    assert native == await change(
        start_time=(START + hours[0] * HOUR).isoformat(),
        end_time=(START + hours[1] * HOUR).isoformat(),
    )
    assert native != await change(**first_four_hours)


async def test_with_guardrails_a_summary_over_cores_own_period_is_refused_unsent(
    recorded: Recorder, core_client: HomeAssistantClient
) -> None:
    """Its window is Core's to work out, so its cost cannot be estimated."""
    with (
        patch.object(
            tools_history,
            "get_global_settings",
            return_value=MagicMock(enable_history_query_guardrails=True),
        ),
        sent_messages(core_client) as sent,
        pytest.raises(ToolError) as refused,
    ):
        await _summary(
            core_client,
            entity_ids="sensor.energy",
            core_options={"calendar": {"period": "day"}},
        )

    assert json.loads(str(refused.value))["error"]["code"] == "VALIDATION_FAILED"
    assert all(
        call.args[0]["type"] != "recorder/statistic_during_period"
        for call in sent.call_args_list
    )


async def test_a_summary_without_types_takes_cores_defaults_for_each_statistic(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    for statistic_id in ("sensor.energy", "sensor.gas"):
        await import_statistic(
            hass,
            statistic_id,
            "kWh",
            "energy",
            [{"start": START, "sum": 1.0}, {"start": START + HOUR, "sum": 3.0}],
        )

    result = await _summary(core_client, entity_ids=["sensor.energy", "sensor.gas"])

    changes = [entity["summary"]["change"] for entity in result["data"]["entities"]]
    assert len(changes) == 2
    assert all(isinstance(change, float) for change in changes)
    assert "statistics converter could not be read" in _warnings(result)


async def test_a_summary_of_an_unknown_statistic_says_the_recorder_has_none(
    recorded: Recorder, core_client: HomeAssistantClient
) -> None:
    result = await _summary(core_client, entity_ids="sensor.enrgy")

    assert "The recorder has no statistics under: sensor.enrgy." in _warnings(result)


async def test_a_type_core_refuses_for_a_summary_is_cores_refusal(
    recorded: Recorder, core_client: HomeAssistantClient
) -> None:
    with pytest.raises(ToolError) as refusal:
        await _summary(core_client, entity_ids="sensor.energy", statistic_types="sum")

    assert json.loads(str(refusal.value))["ha_error_code"] == "invalid_format"


async def test_a_statistics_summary_takes_no_offset(
    recorded: Recorder, core_client: HomeAssistantClient
) -> None:
    with pytest.raises(ToolError, match="does not take an offset"):
        await _summary(core_client, entity_ids="sensor.energy", offset=1)


async def test_the_schema_shown_for_a_summary_is_the_summary_commands(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """A summary and a row read go to two Core commands with different fields."""
    await import_statistic(
        hass, "sensor.energy", "kWh", "energy", [{"start": START, "sum": 1.0}]
    )

    result = await _summary(
        component_core_client, entity_ids="sensor.energy", include_schema=True
    )

    assert (
        result["data"]["core_contract"]["command"] == "recorder/statistic_during_period"
    )


async def test_a_summary_value_the_statistic_does_not_keep_is_not_called_no_rows(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A statistic whose rows carry a mean and no sum has rows in the window
    and no change."""
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [{"start": START + hour * HOUR, "mean": hour} for hour in range(4)],
        mean=True,
    )

    result = await _summary(
        core_client, entity_ids="sensor.energy", statistic_types="change"
    )

    assert "No statistics in this window" not in _warnings(result)
    assert "Core returned no values in this window for: sensor.energy." in _warnings(
        result
    )


async def _summary_after_a_unit_change(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    unit_class: str | None,
    core_options: dict[str, Any] | None,
) -> tuple[Any, Any]:
    """The change and its unit label when the display unit moves to Wh between
    the summary's two reads."""
    hass.states.async_set("sensor.energy", "1", {"unit_of_measurement": "kWh"})
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        unit_class,
        [{"start": START + hour * HOUR, "sum": hour} for hour in range(4)],
    )

    def to_watt_hours(msg: dict[str, Any]) -> None:
        hass.states.async_set("sensor.energy", "1", {"unit_of_measurement": "Wh"})

    with before_handling(hass, "recorder/statistic_during_period", to_watt_hours):
        result = await _summary(
            client,
            entity_ids="sensor.energy",
            statistic_types="change",
            core_options=core_options,
        )
    entity = result["data"]["entities"][0]
    return entity["summary"]["change"], entity["unit_of_measurement"]


@pytest.mark.parametrize(
    "core_options", [None, {"units": {"power": "kW"}}], ids=["default", "other-class"]
)
async def test_a_summary_labels_its_values_with_the_unit_core_converted_them_to(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    core_options: dict[str, Any] | None,
) -> None:
    """Core converts each total to the display unit of that moment; the
    summary asks for the unit its label names, so a unit changed between the
    two reads cannot part them."""
    assert await _summary_after_a_unit_change(
        hass, core_client, "energy", core_options
    ) == (3.0, "kWh")


@pytest.mark.parametrize(
    ("unit_class", "core_options"),
    [(None, None), ("energy", {"units": {"power": "kW"}})],
    ids=["no-unit-class", "other-class"],
)
async def test_a_summary_through_the_component_keeps_value_and_label_together(
    recorded: Recorder,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    unit_class: str | None,
    core_options: dict[str, Any] | None,
) -> None:
    """The unit asked for and the label both come from Core's converter choice
    as the component read it before the totals: also for a statistic whose
    metadata names no unit class, and when the caller asks for other units."""
    assert await _summary_after_a_unit_change(
        hass, component_core_client, unit_class, core_options
    ) == (3.0, "kWh")


async def test_a_summary_labels_its_total_from_the_read_that_gave_its_unit(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """Read metadata and the unit for the totals from one component reply."""
    hass.states.async_set("sensor.energy", "1", {"unit_of_measurement": "kWh"})
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [{"start": START + hour * HOUR, "sum": hour} for hour in range(4)],
    )

    def to_watt_hours(msg: dict[str, Any]) -> None:
        hass.states.async_set("sensor.energy", "1", {"unit_of_measurement": "Wh"})

    with (
        before_handling(hass, "ha_mcp_tools/statistics_units", to_watt_hours),
        sent_messages(component_core_client) as sent,
    ):
        result = await _summary(
            component_core_client, entity_ids="sensor.energy", statistic_types="change"
        )

    assert not result.get("warnings")
    assert all(
        call.args[0]["type"] != "recorder/get_statistics_metadata"
        for call in sent.call_args_list
    )
    entity = result["data"]["entities"][0]
    assert (entity["summary"]["change"], entity["unit_of_measurement"]) == (
        3000.0,
        "Wh",
    )


async def test_a_summary_is_budgeted_as_the_request_it_sends(
    recorded: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """The period is not sent for a summary, so it cannot decide the budget."""
    with patch.object(
        tools_history,
        "get_global_settings",
        return_value=MagicMock(enable_history_query_guardrails=True),
    ):
        result = await HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.energy",
            source="statistics",
            summary=True,
            period="5minute",
            start_time=START.isoformat(),
            end_time=(START + 60 * DAY).isoformat(),
        )

    assert result["data"]["entities"][0]["entity_id"] == "sensor.energy"


async def test_a_live_migration_is_named_instead_of_an_empty_recorder(
    async_test_recorder: Any,
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core reads its oldest state only once a migration has finished; until
    then the recorder is not empty, it is migrating."""
    await hass.config.async_set_time_zone("UTC")
    async with migrating(async_test_recorder, hass, monkeypatch):
        assert await async_setup_component(hass, "history", {})
        result = await _history(component_core_client, START, START + DAY)

    assert "The recorder holds no states." not in _warnings(result)
    assert "migrating its database" in _warnings(result)


async def test_a_window_after_the_oldest_state_does_not_name_it(
    recorded: Recorder, hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The purge did not cut a window the recorder holds states before."""
    await record_states(hass, "sensor.other", ("1", None, _at(START)))

    result = await _history(component_core_client, START + DAY, START + 2 * DAY)

    assert f"No recorded states in this window for: {ENTITY}." in _warnings(result)
    assert "oldest state" not in _warnings(result)


async def test_a_summary_names_an_unknown_unit_only_for_a_statistic_with_values(
    recorded: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the metadata unread, a total's unit is unknown; a statistic without
    values gets the no-values warning instead."""
    await import_statistic(
        hass,
        "sensor.energy",
        "kWh",
        "energy",
        [{"start": START + hour * HOUR, "sum": hour} for hour in range(4)],
    )

    async with unanswered(hass, "recorder/get_statistics_metadata", monkeypatch):
        result = await _summary(
            core_client,
            entity_ids=["sensor.energy", "sensor.gas"],
            statistic_types="change",
        )

    warnings = result["warnings"]
    assert any(
        w.startswith("sensor.energy: statistics output unit is unknown")
        for w in warnings
    )
    assert not any(w.startswith("sensor.gas: statistics output unit") for w in warnings)
