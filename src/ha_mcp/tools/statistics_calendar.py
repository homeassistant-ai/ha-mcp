"""Estimate the calendar-aligned window Core scans for day, week, month and year statistics."""

from datetime import UTC, datetime, timedelta, tzinfo

_CALENDAR_STATISTICS_PERIODS = frozenset({"day", "week", "month", "year"})


def _statistics_scan_window(
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    local_timezone: tzinfo,
) -> tuple[datetime, datetime]:
    """Estimate Core's scan expansion for HA-MCP workload limits (not API validation)."""
    if period not in _CALENDAR_STATISTICS_PERIODS:
        return start_dt, end_dt

    local_start = start_dt.astimezone(local_timezone)
    local_end = end_dt.astimezone(local_timezone)

    if period == "day":
        scan_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        scan_end = local_end.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
    elif period == "week":
        scan_start = local_start.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=local_start.weekday())
        scan_end = (
            local_end.replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=local_end.weekday())
            + timedelta(days=7)
        )
    elif period == "month":
        scan_start = local_start.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        scan_end = _next_month_start(local_end)
    else:
        scan_start = local_start.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        scan_end = local_end.replace(
            year=local_end.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    return scan_start.astimezone(UTC), scan_end.astimezone(UTC)


def _next_month_start(value: datetime) -> datetime:
    """Return local midnight on the first day of the following month."""
    if value.month == 12:
        return value.replace(
            year=value.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return value.replace(
        month=value.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
