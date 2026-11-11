"""Unit tests for history tools helper functions."""

import json
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools import tools_history
from ha_mcp.tools.tools_history import (
    _RELATIVE_TIME_UNIT_SECONDS,
    _parse_time_range,
    parse_relative_time,
)

NOW = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the history module's current time NOW."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            return NOW

    monkeypatch.setattr(tools_history, "datetime", FrozenDateTime)


class TestParseTimeRange:
    def test_relative_end_time_counts_back_like_start_time(self) -> None:
        """A relative end_time counts back from the same reference time as start_time."""
        start, end = _parse_time_range("2h", "1h", 24, reference_time=NOW)
        assert (start, end) == (NOW - timedelta(hours=2), NOW - timedelta(hours=1))

    @pytest.mark.usefixtures("frozen_now")
    def test_window_without_reference_time_counts_back_from_now(self) -> None:
        """Without a reference time, a look-back counts from the current time and the window ends now."""
        assert _parse_time_range("2h", None, 24) == (NOW - timedelta(hours=2), NOW)

    @pytest.mark.parametrize(
        ("start_time", "end_time"), [("bogus", None), ("2h", "bogus")]
    )
    def test_rejected_time_suggests_every_relative_unit(
        self, start_time: str, end_time: str | None
    ) -> None:
        """A rejected start_time or end_time suggests working examples for every relative unit."""
        with pytest.raises(ToolError) as exc_info:
            _parse_time_range(start_time, end_time, 24, reference_time=NOW)

        suggestions = json.loads(str(exc_info.value))["error"]["suggestions"]
        examples = [ex for s in suggestions for ex in re.findall(r"'([^']+)'", s)]
        for example in examples:
            parse_relative_time(example, reference_time=NOW)
        for unit in _RELATIVE_TIME_UNIT_SECONDS:
            assert any(re.fullmatch(rf"\d+{unit}", ex) for ex in examples), unit


class TestParseRelativeTime:
    """Test parse_relative_time function."""

    @pytest.mark.usefixtures("frozen_now")
    def test_without_reference_time_counts_back_from_now(self) -> None:
        """Without a reference time, a relative value counts back from the current time."""
        assert parse_relative_time("1h") == NOW - timedelta(hours=1)

    def test_none_returns_default_hours_ago(self) -> None:
        """None input returns datetime default_hours ago."""
        result = parse_relative_time(None, default_hours=24, reference_time=NOW)
        assert result == NOW - timedelta(hours=24)

    def test_none_with_custom_default_hours(self) -> None:
        """None with custom default_hours works correctly."""
        result = parse_relative_time(None, default_hours=48, reference_time=NOW)
        assert result == NOW - timedelta(hours=48)

    def test_hours_relative_format(self) -> None:
        """Hours relative format (e.g., '24h') parsed correctly."""
        result = parse_relative_time("24h", reference_time=NOW)
        assert result == NOW - timedelta(hours=24)

    def test_days_relative_format(self) -> None:
        """Days relative format (e.g., '7d') parsed correctly."""
        result = parse_relative_time("7d", reference_time=NOW)
        assert result == NOW - timedelta(days=7)

    def test_weeks_relative_format(self) -> None:
        """Weeks relative format (e.g., '2w') parsed correctly."""
        result = parse_relative_time("2w", reference_time=NOW)
        assert result == NOW - timedelta(weeks=2)

    def test_months_relative_format(self) -> None:
        """Months relative format (e.g., '1m') parsed correctly as 30 days."""
        result = parse_relative_time("1m", reference_time=NOW)
        assert result == NOW - timedelta(days=30)

    def test_months_multiple(self) -> None:
        """Multiple months (e.g., '6m') parsed correctly."""
        result = parse_relative_time("6m", reference_time=NOW)
        assert result == NOW - timedelta(days=180)

    def test_minutes_suffix_looks_back_minutes(self) -> None:
        """'30min' looks back 30 minutes instead of being rejected."""
        result = parse_relative_time("30min", reference_time=NOW)
        assert result == NOW - timedelta(minutes=30)

    def test_relative_format_uppercase(self) -> None:
        """Uppercase relative format (e.g., '24H') works."""
        result = parse_relative_time("24H", reference_time=NOW)
        assert result == NOW - timedelta(hours=24)

    def test_relative_format_with_whitespace(self) -> None:
        """Relative format with leading/trailing whitespace works."""
        result = parse_relative_time("  7d  ", reference_time=NOW)
        assert result == NOW - timedelta(days=7)

    def test_iso_format_with_z_suffix(self) -> None:
        """ISO format with Z suffix parsed correctly."""
        result = parse_relative_time("2025-01-25T12:00:00Z")
        expected = datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_iso_format_with_timezone(self) -> None:
        """ISO format with timezone offset parsed correctly."""
        result = parse_relative_time("2025-01-25T12:00:00+00:00")
        expected = datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_iso_format_without_timezone(self) -> None:
        """ISO format without timezone stays naive for ha_get_history to resolve."""
        result = parse_relative_time("2025-01-25T12:00:00")
        expected = datetime(2025, 1, 25, 12, 0, 0)
        assert result == expected

    def test_iso_format_date_only(self) -> None:
        """ISO format with date only parsed correctly."""
        result = parse_relative_time("2025-01-25")
        expected = datetime(2025, 1, 25, 0, 0, 0)
        assert result == expected

    def test_invalid_format_raises_error(self) -> None:
        """Invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time("invalid")

    @pytest.mark.parametrize("value", ["24x", "20mins"])
    def test_invalid_relative_unit_raises_error(self, value: str) -> None:
        """An unknown unit ('24x', or '20mins', which is not '20min') is rejected, not read as another window."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time(value)

    def test_negative_relative_raises_error(self) -> None:
        """Negative relative time (e.g., '-24h') raises ValueError."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time("-24h")

    def test_zero_hours(self) -> None:
        """Zero hours ('0h') returns the reference time."""
        assert parse_relative_time("0h", reference_time=NOW) == NOW

    def test_large_hours_value(self) -> None:
        """Large hours value (e.g., '168h' = 1 week) works."""
        result = parse_relative_time("168h", reference_time=NOW)
        assert result == NOW - timedelta(hours=168)

    @pytest.mark.parametrize("value", ["99999999d", "100000000m"])
    def test_overflowing_relative_value_raises_validation_error(
        self, value: str
    ) -> None:
        """A value too large for a datetime is rejected as out of range, not raised as OverflowError."""
        with pytest.raises(ValueError, match="is out of range"):
            parse_relative_time(value)


async def test_time_without_offset_is_home_assistant_local_time(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Like Core, ha_get_history reads a time without an offset in HA's zone."""
    assert await async_setup_component(hass, "history", {})
    await hass.config.async_set_time_zone("Europe/Berlin")
    with patch.object(
        core_client,
        "send_websocket_message",
        AsyncMock(wraps=core_client.send_websocket_message),
    ) as send:
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.temp",
            start_time="2026-07-01T12:00:00",
            end_time="2026-07-01T13:00:00Z",
        )
    (message,) = [
        c.args[0]
        for c in send.call_args_list
        if c.args[0]["type"] == "history/history_during_period"
    ]
    assert (
        datetime.fromisoformat(message["start_time"]),
        datetime.fromisoformat(message["end_time"]),
    ) == (datetime(2026, 7, 1, 10, tzinfo=UTC), datetime(2026, 7, 1, 13, tzinfo=UTC))


@pytest.mark.parametrize(
    ("entity_count", "end_time", "options"),
    [
        # 10 entities x 25 elapsed hours = 250 entity-hours, over 240
        (10, "2026-10-25T12:00:00", {}),
        # 25 entities x 34 elapsed hours x 12 rows = 10200 rows, over 10000
        (25, "2026-10-25T21:00:00", {"source": "statistics", "period": "5minute"}),
    ],
)
async def test_workload_across_dst_end_counts_elapsed_hours(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_count: int,
    end_time: str,
    options: dict[str, Any],
) -> None:
    """A local window that ends DST is one hour longer than its wall clock."""
    await hass.config.async_set_time_zone("Europe/Berlin")
    with (
        patch.object(
            tools_history,
            "get_global_settings",
            return_value=MagicMock(enable_history_query_guardrails=True),
        ),
        pytest.raises(ToolError, match="safe workload budget"),
    ):
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids=[f"sensor.s{i}" for i in range(entity_count)],
            start_time="2026-10-24T12:00:00",
            end_time=end_time,
            **options,
        )


@pytest.mark.parametrize(
    ("guardrails", "start_time", "end_time"),
    [
        # 02:45 is not a New York time; it reads as 07:45 UTC, after 03:15 = 07:15 UTC
        (False, "2026-03-08T02:45:00", "2026-03-08T03:15:00"),
        # 02:30 and 03:30 are both 07:30 UTC: a zero-length window
        (True, "2026-03-08T02:30:00", "2026-03-08T03:30:00"),
    ],
)
async def test_window_across_spring_forward_gap_is_checked_as_instants(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    guardrails: bool,
    start_time: str,
    end_time: str,
) -> None:
    """A start in the spring-forward gap is not earlier than an end after the gap."""
    await hass.config.async_set_time_zone("America/New_York")
    with (
        patch.object(
            tools_history,
            "get_global_settings",
            return_value=MagicMock(enable_history_query_guardrails=guardrails),
        ),
        pytest.raises(ToolError, match="end_time must be later than start_time"),
    ):
        await tools_history.HistoryTools(core_client).ha_get_history(
            entity_ids="sensor.temp", start_time=start_time, end_time=end_time
        )
