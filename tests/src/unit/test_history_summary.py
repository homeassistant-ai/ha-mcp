"""Unit tests for ha_get_history(summary=True) (issue #2754)."""

import inspect
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.tools import tools_history
from ha_mcp.tools.history_response import summarize_states
from ha_mcp.tools.tools_history import HistoryTools

_START = 1_700_000_000.0


def _rows(*pairs: tuple[str, float]) -> list[dict]:
    """Compressed Core rows, oldest first, at offsets from _START."""
    return [{"s": state, "lu": _START + offset} for state, offset in pairs]


def _iso(offset: float) -> str:
    return datetime.fromtimestamp(_START + offset, UTC).isoformat()


class TestSummarizeStates:
    def test_totals_count_each_change_and_the_time_in_each_state(self) -> None:
        rows = _rows(("on", 0), ("off", 10), ("on", 30), ("unavailable", 45))

        result = summarize_states(rows, _START, _START + 100, limit=100)

        assert result == {
            "changes": 3,
            "first_change": _START + 10,
            "last_change": _START + 45,
            "distinct_states": 3,
            "states": {
                "unavailable": {"periods": 1, "duration_s": 55.0},
                "on": {"periods": 2, "duration_s": 25.0},
                "off": {"periods": 1, "duration_s": 20.0},
            },
        }

    def test_first_row_after_the_window_start_is_a_change(self) -> None:
        # No window-start snapshot: the entity appeared later, older history
        # was purged, or include_start_time_state=false.
        rows = _rows(("off", 10), ("on", 30))

        result = summarize_states(rows, _START, _START + 100, limit=100)

        assert result["changes"] == 2
        assert result["first_change"] == _START + 10

    def test_attribute_update_is_not_counted_as_a_state_change(self) -> None:
        rows = _rows(("heat", 0), ("heat", 5), ("off", 20))

        result = summarize_states(rows, _START, _START + 30, limit=100)

        assert result["changes"] == 1
        assert result["states"]["heat"] == {"periods": 1, "duration_s": 20.0}

    def test_limit_keeps_the_longest_states_and_reports_how_many_exist(self) -> None:
        rows = _rows(("1", 0), ("2", 1), ("3", 4), ("4", 10))

        result = summarize_states(rows, _START, _START + 20, limit=2)

        assert list(result["states"]) == ["4", "3"]
        assert result["distinct_states"] == 4

    def test_entity_without_rows_reports_no_changes(self) -> None:
        assert summarize_states([], _START, _START, limit=100) == {
            "changes": 0,
            "first_change": None,
            "last_change": None,
            "distinct_states": 0,
            "states": {},
        }


def _client(history: dict) -> MagicMock:
    client = MagicMock()
    client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
    client.send_websocket_message = AsyncMock(
        return_value={"success": True, "result": history}
    )
    return client


def _no_timezone_lookup(timezone: str = "UTC") -> Any:
    return patch(
        "ha_mcp.tools.tools_history.add_timezone_metadata",
        side_effect=lambda _c, d, **_kw: {
            "data": d,
            "metadata": {"home_assistant_timezone": timezone},
        },
    )


class _ClockAt(datetime):
    """A datetime whose now() is fixed ten minutes after _START."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        return datetime.fromtimestamp(_START + 600, tz)


async def _query_at_clock(
    history: dict[str, list[dict]], **kwargs: Any
) -> dict[str, Any]:
    """Query a day-long window while the clock stands ten minutes into it."""
    tool = HistoryTools(_client(history)).ha_get_history
    with (
        _no_timezone_lookup(),
        patch("ha_mcp.tools.tools_history.datetime", _ClockAt),
    ):
        return await tool(
            entity_ids=",".join(history),
            start_time=_iso(0),
            end_time=_iso(86400),
            **kwargs,
        )


class TestSummaryThroughTool:
    @pytest.mark.asyncio
    async def test_summary_replaces_rows_with_limited_totals_in_ha_time(self) -> None:
        rows = _rows(("off", 0), ("on", 3600))
        tool = HistoryTools(_client({"binary_sensor.door": rows})).ha_get_history

        with _no_timezone_lookup("America/Toronto"):
            result = await tool(
                entity_ids="binary_sensor.door",
                start_time=_iso(0),
                end_time=_iso(9000),
                summary=True,
                limit=1,
            )

        entity = result["data"]["entities"][0]
        assert "states" not in entity
        assert "total_count" not in entity
        totals = entity["summary"]
        changed_at = datetime.fromisoformat(totals["first_change"])
        assert changed_at.timestamp() == _START + 3600
        # _START falls in November 2023, when Toronto is on EST.
        assert changed_at.utcoffset() == timedelta(hours=-5)
        assert list(totals["states"]) == ["on"]
        assert totals["distinct_states"] == 2
        assert result["data"]["query_params"]["summary"] is True
        # limit=1 adds a cut warning; the fully covered window adds no other.
        assert not any(
            "start at" in w or "No recorded" in w for w in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_future_end_time_does_not_count_time_that_has_not_passed(
        self,
    ) -> None:
        result = await _query_at_clock({"switch.pump": _rows(("on", 0))}, summary=True)

        duration = result["data"]["entities"][0]["summary"]["states"]["on"]
        assert duration["duration_s"] == 600.0

    @pytest.mark.asyncio
    async def test_rows_from_after_now_are_not_counted_as_changes(self) -> None:
        # Core answers a future end_time up to its own clock, which can run
        # past the `now` the summary stops at, by a fraction of a second or more.
        history = {"switch.pump": _rows(("on", 0), ("off", 600.001), ("on", 800))}

        result = await _query_at_clock(history, summary=True)

        totals = result["data"]["entities"][0]["summary"]
        assert totals["changes"] == 0
        assert totals["last_change"] is None
        assert totals["distinct_states"] == 1
        assert list(totals["states"]) == ["on"]

    @pytest.mark.asyncio
    async def test_entity_recorded_only_from_now_on_has_no_states(self) -> None:
        result = await _query_at_clock({"switch.new": _rows(("on", 600))}, summary=True)

        warnings = result["warnings"]
        assert any("switch.new" in w and "No recorded" in w for w in warnings)
        assert not any("switch.new" in w and "start at" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_short_totals_are_flagged_in_warnings(self) -> None:
        history = {"binary_sensor.late": _rows(("on", 1800))}
        tool = HistoryTools(_client(history)).ha_get_history

        with _no_timezone_lookup():
            result = await tool(
                entity_ids="binary_sensor.late,binary_sensor.typo",
                start_time=_iso(0),
                end_time=_iso(3600),
                summary=True,
            )

        warnings = result["warnings"]
        assert any("binary_sensor.typo" in w and "No recorded" in w for w in warnings)
        assert any("binary_sensor.late" in w and "start at" in w for w in warnings)
        assert not any(
            "binary_sensor.late" in w and "No recorded" in w for w in warnings
        )

    @pytest.mark.asyncio
    async def test_states_cut_by_limit_are_flagged_in_warnings(self) -> None:
        # The shortest states are cut first, so a brief "unavailable" can drop
        # out. sensor.power also starts late: a cut is flagged regardless.
        history = {
            "sensor.power": _rows(
                ("1", 10), ("2", 100), ("unavailable", 200), ("3", 201)
            ),
            "sensor.speed": _rows(("a", 0), ("b", 1), ("c", 2), ("d", 3), ("e", 4)),
            "sensor.flat": _rows(("5", 0)),
        }
        tool = HistoryTools(_client(history)).ha_get_history

        with _no_timezone_lookup():
            result = await tool(
                entity_ids="sensor.power,sensor.speed,sensor.flat",
                start_time=_iso(0),
                end_time=_iso(3600),
                summary=True,
                limit=3,
            )

        cut = [w for w in result["warnings"] if "sensor.speed" in w]
        assert len(cut) == 1, result["warnings"]
        assert "sensor.power: 1 of 4" in cut[0]
        assert "sensor.speed: 2 of 5" in cut[0]
        assert "sensor.flat" not in cut[0]
        assert any("sensor.power" in w and "start at" in w for w in result["warnings"])

    @pytest.mark.asyncio
    async def test_without_summary_the_rows_are_returned_unchanged(self) -> None:
        # Rows after `now` are dropped for a summary only.
        history = {"switch.pump": _rows(("on", 0), ("off", 700))}

        result = await _query_at_clock(history)

        entity = result["data"]["entities"][0]
        assert "summary" not in entity
        assert [row["state"] for row in entity["states"]] == ["off", "on"]
        assert result["data"]["query_params"]["summary"] is False
        assert "warnings" not in result

    @pytest.mark.asyncio
    async def test_offset_with_summary_is_rejected_before_querying(self) -> None:
        client = _client({})
        tool = HistoryTools(client).ha_get_history

        # The summary error comes first: following the multi-entity
        # suggestion alone would still fail.
        with pytest.raises(ToolError, match="does not take an offset"):
            await tool(entity_ids="switch.a,switch.b", offset=5, summary=True)
        client.send_websocket_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_statistics_offset_still_works_when_summary_is_set(self) -> None:
        signature = inspect.signature(tools_history._fetch_statistics)
        tool = HistoryTools(_client({})).ha_get_history

        with (
            _no_timezone_lookup(),
            patch(
                "ha_mcp.tools.tools_history._fetch_statistics",
                new_callable=AsyncMock,
                return_value={"success": True, "source": "statistics", "entities": []},
            ) as fetch_statistics,
        ):
            await tool(
                entity_ids="sensor.energy",
                source="statistics",
                offset=5,
                summary=True,
            )

        fetch_statistics.assert_awaited_once()
        call = fetch_statistics.await_args
        assert signature.bind(*call.args, **call.kwargs).arguments["offset"] == 5
