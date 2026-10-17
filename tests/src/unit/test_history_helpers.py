"""Unit tests for history tools helper functions."""

import json
import re
from datetime import UTC, datetime, timedelta, tzinfo

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
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
        """ISO format without timezone gets UTC added."""
        result = parse_relative_time("2025-01-25T12:00:00")
        expected = datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_iso_format_date_only(self) -> None:
        """ISO format with date only parsed correctly."""
        result = parse_relative_time("2025-01-25")
        expected = datetime(2025, 1, 25, 0, 0, 0, tzinfo=UTC)
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
