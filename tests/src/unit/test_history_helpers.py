"""Unit tests for history tools helper functions."""

from datetime import UTC, datetime, timedelta

import pytest

from ha_mcp.tools.tools_history import _parse_time_range, parse_relative_time


class TestParseTimeRange:
    def test_relative_end_time_looks_back_from_now(self):
        """A relative end_time is read like start_time, not rejected as non-ISO."""
        now = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
        start, end = _parse_time_range("2h", "30min", 24, reference_time=now)
        assert (start, end) == (now - timedelta(hours=2), now - timedelta(minutes=30))


class TestParseRelativeTime:
    """Test parse_relative_time function."""

    def test_none_returns_default_hours_ago(self):
        """None input returns datetime default_hours ago."""
        result = parse_relative_time(None, default_hours=24)
        expected = datetime.now(UTC) - timedelta(hours=24)
        # Allow 1 second tolerance for test execution time
        assert abs((result - expected).total_seconds()) < 1

    def test_none_with_custom_default_hours(self):
        """None with custom default_hours works correctly."""
        result = parse_relative_time(None, default_hours=48)
        expected = datetime.now(UTC) - timedelta(hours=48)
        assert abs((result - expected).total_seconds()) < 1

    def test_hours_relative_format(self):
        """Hours relative format (e.g., '24h') parsed correctly."""
        result = parse_relative_time("24h")
        expected = datetime.now(UTC) - timedelta(hours=24)
        assert abs((result - expected).total_seconds()) < 1

    def test_days_relative_format(self):
        """Days relative format (e.g., '7d') parsed correctly."""
        result = parse_relative_time("7d")
        expected = datetime.now(UTC) - timedelta(days=7)
        assert abs((result - expected).total_seconds()) < 1

    def test_weeks_relative_format(self):
        """Weeks relative format (e.g., '2w') parsed correctly."""
        result = parse_relative_time("2w")
        expected = datetime.now(UTC) - timedelta(weeks=2)
        assert abs((result - expected).total_seconds()) < 1

    def test_months_relative_format(self):
        """Months relative format (e.g., '1m') parsed correctly as 30 days."""
        result = parse_relative_time("1m")
        expected = datetime.now(UTC) - timedelta(days=30)
        assert abs((result - expected).total_seconds()) < 1

    def test_months_multiple(self):
        """Multiple months (e.g., '6m') parsed correctly."""
        result = parse_relative_time("6m")
        expected = datetime.now(UTC) - timedelta(days=180)
        assert abs((result - expected).total_seconds()) < 1

    def test_minutes_suffix_looks_back_minutes(self):
        """'30min' looks back 30 minutes instead of being rejected; 'm' keeps meaning 30 days."""
        reference = datetime(2026, 10, 16, 20, 0, tzinfo=UTC)
        result = parse_relative_time("30min", reference_time=reference)
        assert result == reference - timedelta(minutes=30)

    def test_relative_format_uppercase(self):
        """Uppercase relative format (e.g., '24H') works."""
        result = parse_relative_time("24H")
        expected = datetime.now(UTC) - timedelta(hours=24)
        assert abs((result - expected).total_seconds()) < 1

    def test_relative_format_with_whitespace(self):
        """Relative format with leading/trailing whitespace works."""
        result = parse_relative_time("  7d  ")
        expected = datetime.now(UTC) - timedelta(days=7)
        assert abs((result - expected).total_seconds()) < 1

    def test_iso_format_with_z_suffix(self):
        """ISO format with Z suffix parsed correctly."""
        result = parse_relative_time("2025-01-25T12:00:00Z")
        expected = datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_iso_format_with_timezone(self):
        """ISO format with timezone offset parsed correctly."""
        result = parse_relative_time("2025-01-25T12:00:00+00:00")
        expected = datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_iso_format_without_timezone(self):
        """ISO format without timezone gets UTC added."""
        result = parse_relative_time("2025-01-25T12:00:00")
        expected = datetime(2025, 1, 25, 12, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_iso_format_date_only(self):
        """ISO format with date only parsed correctly."""
        result = parse_relative_time("2025-01-25")
        expected = datetime(2025, 1, 25, 0, 0, 0, tzinfo=UTC)
        assert result == expected

    def test_invalid_format_raises_error(self):
        """Invalid format raises ValueError."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time("invalid")

    def test_negative_relative_raises_error(self):
        """Negative relative time (e.g., '-24h') raises ValueError."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time("-24h")

    def test_zero_hours(self):
        """Zero hours ('0h') returns current time."""
        result = parse_relative_time("0h")
        expected = datetime.now(UTC)
        assert abs((result - expected).total_seconds()) < 1

    def test_large_hours_value(self):
        """Large hours value (e.g., '168h' = 1 week) works."""
        result = parse_relative_time("168h")
        expected = datetime.now(UTC) - timedelta(hours=168)
        assert abs((result - expected).total_seconds()) < 1

    @pytest.mark.parametrize("value", ["24x", "20mins", "99999999d", "100000000m"])
    def test_unusable_relative_value_raises_validation_error(self, value):
        """An unknown unit ('20mins' is not '20min') or an out-of-range value is rejected, never read as another window."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time(value)
