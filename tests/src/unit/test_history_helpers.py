"""Unit tests for history tools helper functions."""

from datetime import UTC, datetime, timedelta

import pytest

from ha_mcp.tools.tools_history import _convert_timestamp, parse_relative_time


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

    def test_invalid_relative_unit_raises_error(self):
        """Invalid relative unit (e.g., '24x') raises ValueError."""
        with pytest.raises(ValueError, match="Invalid time format"):
            parse_relative_time("24x")

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


class TestConvertTimestamp:
    """Test _convert_timestamp function for issue #447 regression."""

    def test_none_returns_none(self):
        """None input returns None."""
        assert _convert_timestamp(None) is None

    def test_unix_epoch_int_converted_to_iso(self):
        """Unix epoch integer converted to ISO format string."""
        # 1700000000 = 2023-11-14T22:13:20+00:00 UTC
        timestamp = 1700000000
        result = _convert_timestamp(timestamp)
        assert result == "2023-11-14T22:13:20+00:00"

    def test_unix_epoch_float_converted_to_iso(self):
        """Unix epoch float with microseconds converted to ISO format."""
        # 1700000000.123456 = 2023-11-14T22:13:20.123456+00:00 UTC
        timestamp = 1700000000.123456
        result = _convert_timestamp(timestamp)
        # Should preserve microseconds
        assert result.startswith("2023-11-14T22:13:20.123456")
        assert result.endswith("+00:00")

    def test_iso_string_passed_through(self):
        """ISO format string passed through unchanged."""
        iso_string = "2026-01-17T12:00:00+00:00"
        result = _convert_timestamp(iso_string)
        assert result == iso_string

    def test_iso_string_with_z_passed_through(self):
        """ISO format string with Z suffix passed through."""
        iso_string = "2026-01-17T12:00:00Z"
        result = _convert_timestamp(iso_string)
        assert result == iso_string

    def test_invalid_type_returns_none(self):
        """Invalid type (not int/float/str/None) returns None."""
        assert _convert_timestamp([]) is None
        assert _convert_timestamp({}) is None
        assert _convert_timestamp(object()) is None

    def test_zero_timestamp(self):
        """Zero timestamp (epoch start) converts correctly."""
        result = _convert_timestamp(0)
        assert result == "1970-01-01T00:00:00+00:00"

    def test_negative_timestamp(self):
        """Negative timestamp (before epoch) converts correctly."""
        # 1969-12-31T23:00:00+00:00
        result = _convert_timestamp(-3600)
        assert result.startswith("1969-12-31T23:00:00")


class TestTimestampHandling:
    """Test timestamp handling logic for issue #447.

    Issue #447: ha_get_history returned null last_changed and missing last_updated.
    Root cause: HA WebSocket API omits 'lc' when it equals 'lu' (optimization).
    Fix: When 'lc' is missing, use 'lu' value for both timestamps.
    """

    def test_both_timestamps_present(self):
        """When both lc and lu present, use their respective values."""
        state = {
            "s": "on",
            "lc": 1700000000.0,  # Different from lu
            "lu": 1700000100.0,
            "a": {},
        }

        # Simulate the formatting logic from tools_history.py
        last_updated_raw = state.get("lu") or state.get("last_updated")
        last_changed_raw = state.get("lc") or state.get("last_changed")

        if last_changed_raw is None and last_updated_raw is not None:
            last_changed_raw = last_updated_raw

        last_changed = _convert_timestamp(last_changed_raw)
        last_updated = _convert_timestamp(last_updated_raw)

        assert last_changed is not None
        assert last_updated is not None
        assert last_changed == "2023-11-14T22:13:20+00:00"
        assert last_updated == "2023-11-14T22:15:00+00:00"

    def test_lc_omitted_when_equals_lu(self):
        """When lc is omitted (equals lu), last_changed should use lu value.

        This is the regression test for issue #447.
        HA WebSocket API omits 'lc' when state and timestamps are identical.
        """
        state = {
            "s": "on",
            # 'lc' is omitted when it equals 'lu'
            "lu": 1700000000.0,
            "a": {},
        }

        # Simulate the formatting logic from tools_history.py
        last_updated_raw = state.get("lu") or state.get("last_updated")
        last_changed_raw = state.get("lc") or state.get("last_changed")

        # Critical fix: when lc is missing, use lu
        if last_changed_raw is None and last_updated_raw is not None:
            last_changed_raw = last_updated_raw

        last_changed = _convert_timestamp(last_changed_raw)
        last_updated = _convert_timestamp(last_updated_raw)

        # Both should have the same value
        assert last_changed is not None, "last_changed should not be None (issue #447)"
        assert last_updated is not None
        assert last_changed == "2023-11-14T22:13:20+00:00"
        assert last_updated == "2023-11-14T22:13:20+00:00"
        assert last_changed == last_updated

    def test_long_form_timestamps(self):
        """Test long-form timestamp keys (last_changed/last_updated) work."""
        state = {
            "state": "on",
            "last_changed": "2026-01-17T12:00:00+00:00",
            "last_updated": "2026-01-17T12:00:00+00:00",
            "attributes": {},
        }

        last_updated_raw = state.get("lu") or state.get("last_updated")
        last_changed_raw = state.get("lc") or state.get("last_changed")

        if last_changed_raw is None and last_updated_raw is not None:
            last_changed_raw = last_updated_raw

        last_changed = _convert_timestamp(last_changed_raw)
        last_updated = _convert_timestamp(last_updated_raw)

        assert last_changed == "2026-01-17T12:00:00+00:00"
        assert last_updated == "2026-01-17T12:00:00+00:00"

    def test_long_form_lc_omitted(self):
        """Test long-form with last_changed omitted."""
        state = {
            "state": "on",
            # 'last_changed' omitted
            "last_updated": "2026-01-17T12:00:00+00:00",
            "attributes": {},
        }

        last_updated_raw = state.get("lu") or state.get("last_updated")
        last_changed_raw = state.get("lc") or state.get("last_changed")

        if last_changed_raw is None and last_updated_raw is not None:
            last_changed_raw = last_updated_raw

        last_changed = _convert_timestamp(last_changed_raw)
        last_updated = _convert_timestamp(last_updated_raw)

        assert last_changed is not None
        assert last_updated is not None
        assert last_changed == last_updated

    def test_no_timestamps_at_all(self):
        """Test edge case where no timestamps present (shouldn't happen in practice)."""
        state = {
            "s": "on",
            "a": {},
        }

        last_updated_raw = state.get("lu") or state.get("last_updated")
        last_changed_raw = state.get("lc") or state.get("last_changed")

        if last_changed_raw is None and last_updated_raw is not None:
            last_changed_raw = last_updated_raw

        last_changed = _convert_timestamp(last_changed_raw)
        last_updated = _convert_timestamp(last_updated_raw)

        # Both should be None in this edge case
        assert last_changed is None
        assert last_updated is None
