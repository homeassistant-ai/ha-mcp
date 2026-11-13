"""``ha_get_history(summary=True)`` totals over Core's recorded history (#2754).

Every state is recorded by Core's recorder at a fixed time in the past, and the
tool reads it back from Core's ``history/history_during_period``. The tool's own
clock is set by ``_query``'s ``now``: Core's clock keeps running, so a state
recorded after the tool's ``now`` comes back from Core as it would when Core's
clock runs ahead of the reading the tool took before its query.
"""

from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any, Self
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_history
from ha_mcp.tools.tools_history import HistoryTools

# 2023-11-14T22:13:20Z: Toronto is on EST then.
_START = 1_700_000_000.0
# Before every window, so Core answers the state at the window start.
_BEFORE = -100.0
# After every window: no recorded time lies in the tool's future.
_LATER = 10 * 86400.0


@pytest.fixture
async def recorder(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Core's recorder and history, in UTC unless a test sets another zone."""
    assert await async_setup_component(hass, "history", {})
    await hass.config.async_set_time_zone("UTC")


async def _record(
    hass: HomeAssistant, entity_id: str, *rows: tuple[str, float], **attrs: Any
) -> None:
    """Record ``(state, offset from _START)`` rows, oldest first."""
    for state, offset in rows:
        attributes = {key: value[offset] for key, value in attrs.items()}
        hass.states.async_set(entity_id, state, attributes, timestamp=_START + offset)
    await async_wait_recording_done(hass)


def _iso(offset: float) -> str:
    return datetime.fromtimestamp(_START + offset, UTC).isoformat()


async def _query(
    client: HomeAssistantClient,
    entity_ids: str,
    end: float,
    *,
    now: float = _LATER,
    **kwargs: Any,
) -> dict[str, Any]:
    """Query ``[_START, _START + end]`` while the tool's clock reads ``now``."""

    class ClockAt(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(_START + now, tz)

    with patch.object(tools_history, "datetime", ClockAt):
        return await HistoryTools(client).ha_get_history(
            entity_ids=entity_ids, start_time=_iso(0), end_time=_iso(end), **kwargs
        )


def _summary(result: dict[str, Any], entity_id: str) -> dict[str, Any]:
    """The entity's totals, with change times read back as offsets from _START."""
    (entity,) = [e for e in result["data"]["entities"] if e["entity_id"] == entity_id]
    totals: dict[str, Any] = dict(entity["summary"])
    for key in ("first_change", "last_change"):
        if totals[key] is not None:
            totals[key] = datetime.fromisoformat(totals[key]).timestamp() - _START
    return totals


@pytest.mark.usefixtures("recorder")
async def test_totals_count_each_change_and_the_time_in_each_state(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(
        hass,
        "switch.pump",
        ("on", _BEFORE),
        ("off", 10),
        ("on", 30),
        ("unavailable", 45),
    )

    result = await _query(core_client, "switch.pump", 100, summary=True)

    assert _summary(result, "switch.pump") == {
        "changes": 3,
        "first_change": 10,
        "last_change": 45,
        "distinct_states": 3,
        "states": {
            "unavailable": {"periods": 1, "duration_s": 55.0},
            "on": {"periods": 2, "duration_s": 25.0},
            "off": {"periods": 1, "duration_s": 20.0},
        },
    }


@pytest.mark.usefixtures("recorder")
async def test_first_state_after_the_window_start_is_a_change(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # No window-start state: the entity appeared later, older history was
    # purged, or include_start_time_state=false.
    await _record(hass, "switch.pump", ("off", 10), ("on", 30))

    result = await _query(core_client, "switch.pump", 100, summary=True)

    totals = _summary(result, "switch.pump")
    assert totals["changes"] == 2
    assert totals["first_change"] == 10


@pytest.mark.usefixtures("recorder")
async def test_attribute_update_is_not_counted_as_a_state_change(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # Core returns a climate entity's attribute-only updates as rows.
    await _record(
        hass,
        "climate.hall",
        ("heat", _BEFORE),
        ("heat", 5),
        ("off", 20),
        temperature={_BEFORE: 20, 5: 21, 20: 21},
    )

    result = await _query(core_client, "climate.hall", 30, summary=True)
    rows = await _query(core_client, "climate.hall", 30, order="asc")

    assert [row["state"] for row in rows["data"]["entities"][0]["states"]] == [
        "heat",
        "heat",
        "off",
    ]
    totals = _summary(result, "climate.hall")
    assert totals["changes"] == 1
    assert totals["states"]["heat"] == {"periods": 1, "duration_s": 20.0}


@pytest.mark.usefixtures("recorder")
async def test_limit_keeps_the_longest_states_and_reports_how_many_exist(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, "sensor.mode", ("1", _BEFORE), ("2", 1), ("3", 4), ("4", 10))

    result = await _query(core_client, "sensor.mode", 20, summary=True, limit=2)

    totals = _summary(result, "sensor.mode")
    assert list(totals["states"]) == ["4", "3"]
    assert totals["distinct_states"] == 4


@pytest.mark.usefixtures("recorder")
async def test_entity_without_recorded_states_reports_no_changes(
    core_client: HomeAssistantClient,
) -> None:
    result = await _query(core_client, "switch.typo", 100, summary=True)

    assert _summary(result, "switch.typo") == {
        "changes": 0,
        "first_change": None,
        "last_change": None,
        "distinct_states": 0,
        "states": {},
    }


@pytest.mark.usefixtures("recorder")
async def test_summary_replaces_rows_with_totals_in_ha_time(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await hass.config.async_set_time_zone("America/Toronto")
    await _record(hass, "binary_sensor.door", ("off", _BEFORE), ("on", 3600))

    result = await _query(core_client, "binary_sensor.door", 9000, summary=True)

    entity = result["data"]["entities"][0]
    assert "states" not in entity
    assert "total_count" not in entity
    changed_at = datetime.fromisoformat(entity["summary"]["first_change"])
    assert changed_at.timestamp() == _START + 3600
    assert changed_at.utcoffset() == timedelta(hours=-5)
    assert result["data"]["query_params"]["summary"] is True
    # The window is fully covered, so nothing is flagged.
    assert "warnings" not in result


@pytest.mark.usefixtures("recorder")
async def test_future_end_time_does_not_count_time_that_has_not_passed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, "switch.pump", ("on", _BEFORE))

    result = await _query(core_client, "switch.pump", 86400, now=600, summary=True)

    assert _summary(result, "switch.pump")["states"]["on"]["duration_s"] == 600.0


@pytest.mark.usefixtures("recorder")
async def test_states_from_after_now_are_not_counted_as_changes(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # Core answers a future end_time up to its own clock, which can run past
    # the `now` the summary stops at, by a fraction of a second or more.
    await _record(hass, "switch.pump", ("on", _BEFORE), ("off", 600.001), ("on", 800))

    result = await _query(core_client, "switch.pump", 86400, now=600, summary=True)

    totals = _summary(result, "switch.pump")
    assert totals["changes"] == 0
    assert totals["last_change"] is None
    assert totals["distinct_states"] == 1
    assert list(totals["states"]) == ["on"]


@pytest.mark.usefixtures("recorder")
async def test_entity_recorded_only_from_now_on_has_no_states(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, "switch.new", ("on", 600))

    result = await _query(core_client, "switch.new", 86400, now=600, summary=True)

    warnings = result["warnings"]
    assert any("switch.new" in w and "No recorded" in w for w in warnings)
    assert not any("switch.new" in w and "start at" in w for w in warnings)


@pytest.mark.usefixtures("recorder")
async def test_short_totals_are_flagged_in_warnings(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _record(hass, "binary_sensor.late", ("on", 1800))

    result = await _query(
        core_client, "binary_sensor.late,binary_sensor.typo", 3600, summary=True
    )

    warnings = result["warnings"]
    assert any("binary_sensor.typo" in w and "No recorded" in w for w in warnings)
    assert any("binary_sensor.late" in w and "start at" in w for w in warnings)
    assert not any("binary_sensor.late" in w and "No recorded" in w for w in warnings)


@pytest.mark.usefixtures("recorder")
async def test_states_cut_by_limit_are_flagged_in_warnings(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # The shortest states are cut first, so a brief "unavailable" can drop
    # out. sensor.power also starts late: a cut is flagged regardless. Core
    # answers window-start states only once it holds a state older than the
    # window, so the entities recorded before the window come first.
    await _record(
        hass,
        "sensor.speed",
        ("a", _BEFORE),
        ("b", 1),
        ("c", 2),
        ("d", 3),
        ("e", 4),
    )
    await _record(hass, "sensor.flat", ("5", _BEFORE))
    await _record(
        hass, "sensor.power", ("1", 10), ("2", 100), ("unavailable", 200), ("3", 201)
    )

    result = await _query(
        core_client,
        "sensor.power,sensor.speed,sensor.flat",
        3600,
        summary=True,
        limit=3,
    )

    cut = [w for w in result["warnings"] if "sensor.speed" in w]
    assert len(cut) == 1, result["warnings"]
    assert "sensor.power: 1 of 4" in cut[0]
    assert "sensor.speed: 2 of 5" in cut[0]
    assert "sensor.flat" not in cut[0]
    assert any("sensor.power" in w and "start at" in w for w in result["warnings"])


@pytest.mark.usefixtures("recorder")
async def test_without_summary_the_rows_are_returned_unchanged(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # Rows after `now` are dropped for a summary only.
    await _record(hass, "switch.pump", ("on", _BEFORE), ("off", 700))

    result = await _query(core_client, "switch.pump", 86400, now=600)

    entity = result["data"]["entities"][0]
    assert "summary" not in entity
    assert [row["state"] for row in entity["states"]] == ["off", "on"]
    assert result["data"]["query_params"]["summary"] is False
    assert "warnings" not in result


async def test_offset_with_summary_is_rejected_before_querying(
    core_client: HomeAssistantClient,
) -> None:
    # The summary error comes first: following the multi-entity suggestion
    # alone would still fail.
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            wraps=core_client.send_websocket_message,
        ) as sent,
        pytest.raises(ToolError, match="does not take an offset"),
    ):
        await HistoryTools(core_client).ha_get_history(
            entity_ids="switch.a,switch.b", offset=5, summary=True
        )
    sent.assert_not_awaited()


@pytest.mark.usefixtures("recorder")
async def test_statistics_offset_still_works_when_summary_is_set(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """summary applies to state history only; statistics still page by offset."""
    async_import_statistics(
        hass,
        {
            "mean_type": StatisticMeanType.NONE,
            "has_sum": True,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.energy",
            "unit_class": "energy",
            "unit_of_measurement": "kWh",
        },
        [
            {
                "start": datetime(2023, 11, 14, 23, tzinfo=UTC) + timedelta(hours=h),
                "sum": h,
            }
            for h in range(7)
        ],
    )
    await async_wait_recording_done(hass)

    result = await _query(
        core_client,
        "sensor.energy",
        86400,
        source="statistics",
        period="hour",
        offset=5,
        summary=True,
    )

    (entity,) = result["data"]["entities"]
    assert "summary" not in entity
    assert entity["count"] == 2
    assert result["data"]["query_params"]["offset"] == 5
