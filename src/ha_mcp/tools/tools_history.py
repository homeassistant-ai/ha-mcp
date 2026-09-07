"""
Historical data access tools for Home Assistant MCP server.

This module provides tools for accessing historical data from Home Assistant's
recorder component via a single consolidated tool:

ha_get_history -- Retrieve historical data with source-selectable mode:
  - source="history" (default): Raw state changes, ~10 day retention
  - source="statistics": Pre-aggregated long-term statistics, permanent retention
"""

import logging
import math
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Annotated, Any, Literal

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response, create_validation_error
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_info,
    safe_progress,
)
from .util_helpers import (
    JSON_STRING_COERCION,
    add_timezone_metadata,
    build_pagination_metadata,
    fetch_ha_timezone,
    is_connection_error_message,
    parse_string_list_param,
    project_fields,
    resolve_local_timezone,
)

logger = logging.getLogger(__name__)

_RELATIVE_TIME_UNIT_SECONDS = {
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "m": 30 * 24 * 60 * 60,
}


def _convert_timestamp(value: Any) -> str | None:
    """Convert a timestamp value to ISO format string.

    Handles both Unix epoch floats (from WebSocket short-form responses)
    and string timestamps (from long-form responses).

    Args:
        value: Timestamp as Unix epoch float, ISO string, or None

    Returns:
        ISO format string or None if value is None/invalid
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    if isinstance(value, str):
        return value
    return None


def parse_relative_time(
    time_str: str | None,
    default_hours: int = 24,
    *,
    reference_time: datetime | None = None,
) -> datetime:
    """
    Parse a time string that can be either ISO format or relative (e.g., '24h', '7d').

    Args:
        time_str: Time string in ISO format or relative format (e.g., "24h", "7d", "2w", "1m" where 1m = 30 days)
        default_hours: Default hours to go back if time_str is None
        reference_time: Reference datetime for relative values and defaults.

    Returns:
        A datetime with the parsed value's timezone, or ``reference_time``'s
        timezone for relative values.
    """
    now = reference_time or datetime.now(UTC)
    if time_str is None:
        return now - timedelta(hours=default_hours)

    # Check for relative time format
    relative_pattern = r"^(\d+)([hdwm])$"
    match = re.match(relative_pattern, time_str.lower().strip())

    if match:
        value = int(match.group(1))
        unit = match.group(2)
        try:
            return now - timedelta(seconds=value * _RELATIVE_TIME_UNIT_SECONDS[unit])
        except OverflowError as exc:
            raise ValueError(
                f"Invalid time format: {time_str} is out of range"
            ) from exc

    # Try parsing as ISO format
    try:
        # Handle various ISO formats
        if time_str.endswith("Z"):
            time_str = time_str[:-1] + "+00:00"
        dt = datetime.fromisoformat(time_str)
        # Ensure timezone awareness
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, OverflowError) as e:
        raise ValueError(
            f"Invalid time format: {time_str}. Use ISO format or relative (e.g., '24h', '7d', '2w', '1m')"
        ) from e


# Source-dependent default look-back periods
_DEFAULT_START_HOURS_BY_SOURCE: dict[str, int] = {"history": 24, "statistics": 30 * 24}

# Default and maximum limits for history entries
_DEFAULT_HISTORY_LIMIT = 100
_MAX_HISTORY_LIMIT = 1000

# Home Assistant's recorder WebSocket APIs have no server-side row limit. These
# estimated safety budgets bound recorder scan work; they are not measured HA
# limits and deliberately favor ordinary default-window requests.
_MAX_HISTORY_ENTITIES = 10
_MAX_HISTORY_ENTITY_HOURS = 240.0
_MAX_STATISTICS_ENTITIES = 25
_MAX_SHORT_TERM_STATISTICS_ROWS = 10_000
_MAX_LONG_TERM_STATISTICS_ROWS = 20_000
_CALENDAR_STATISTICS_PERIODS = frozenset({"day", "week", "month", "year"})
_VALID_STATISTICS_PERIODS = frozenset(
    {"5minute", "hour", *_CALENDAR_STATISTICS_PERIODS}
)


async def _get_statistics_timezone(client: Any) -> tzinfo:
    """Resolve HA's timezone without accepting an unsafe UTC fallback."""
    timezone_name, fetch_failed = await fetch_ha_timezone(client)
    if fetch_failed:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                "Could not fetch the Home Assistant timezone required for safe calendar statistics estimation",
                suggestions=[
                    "Check the Home Assistant connection, then retry.",
                    "Use period='hour' or period='5minute', which do not require calendar alignment.",
                ],
            )
        )
    timezone, resolved_timezone_name = resolve_local_timezone(timezone_name)
    if resolved_timezone_name != timezone_name:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "Home Assistant reports a timezone that could not be resolved for safe calendar statistics estimation",
                context={"home_assistant_timezone": timezone_name},
                suggestions=[
                    "Configure a valid Home Assistant timezone, then retry.",
                    "Use period='hour' or period='5minute', which do not require calendar alignment.",
                ],
            )
        )
    return timezone


async def _prepare_guardrail_query(
    client: Any,
    *,
    enabled: bool,
    source: str,
    period: str,
    start_dt: datetime,
    end_dt: datetime,
    end_time_was_provided: bool,
) -> tzinfo:
    """Validate guarded ranges and resolve calendar statistics timezone."""
    _validate_time_range(
        start_dt,
        end_dt,
        end_time_was_provided=end_time_was_provided,
        reject_zero_length=enabled,
    )
    if not enabled:
        return UTC
    if source != "statistics" or period not in _CALENDAR_STATISTICS_PERIODS:
        return UTC
    _validate_calendar_statistics_range(start_dt, end_dt, period)
    return await _get_statistics_timezone(client)


class HistoryTools:
    """Historical data access tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_history",
        tags={"History & Statistics"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Entity History or Statistics",
        },
    )
    @log_tool_usage
    async def ha_get_history(
        self,
        entity_ids: Annotated[
            str | list[str],
            JSON_STRING_COERCION,
            Field(
                description="Entity ID(s) to query. Can be a single ID, comma-separated string, or JSON array."
            ),
        ],
        source: Annotated[
            Literal["history", "statistics"],
            Field(
                description=(
                    'Data source: "history" (default) for raw state changes (~10 day retention), '
                    'or "statistics" for pre-aggregated long-term data (permanent, requires state_class).'
                ),
                default="history",
            ),
        ] = "history",
        start_time: Annotated[
            str | None,
            Field(
                description="Start time: ISO datetime or relative (e.g., '24h', '7d', '30d'). Default: 24h ago for history, 30d ago for statistics",
                default=None,
            ),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(
                description="End time: ISO datetime. Default: now",
                default=None,
            ),
        ] = None,
        # History-specific (ignored when source="statistics")
        minimal_response: Annotated[
            bool,
            Field(
                description='Return only states/timestamps without attributes. Default: true. Ignored when source="statistics"',
                default=True,
            ),
        ] = True,
        significant_changes_only: Annotated[
            bool,
            Field(
                description='Filter to significant state changes only. Default: true. Ignored when source="statistics"',
                default=True,
            ),
        ] = True,
        limit: Annotated[
            int | None,
            Field(
                description='Max entries per entity. Default: 100, Max: 1000. For source="history": state changes. For source="statistics": aggregated rows. With multiple entity_ids, offset must be 0 and total rows returned can reach limit × len(entity_ids).',
                default=None,
                ge=1,
                le=1000,
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(
                description="Number of entries to skip per entity for pagination. Default: 0. Offset > 0 requires a single entity_id. Use with limit and has_more/next_offset in the response.",
                default=None,
                ge=0,
            ),
        ] = None,
        # Statistics-specific (ignored when source="history")
        period: Annotated[
            str,
            Field(
                description='Aggregation period: "5minute", "hour", "day", "week", "month", "year". Default: "day". Ignored when source="history"',
                default="day",
            ),
        ] = "day",
        statistic_types: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description='Statistics types: "mean", "min", "max", "sum", "state", "change". Default: all. Ignored when source="history"',
                default=None,
            ),
        ] = None,
        order: Annotated[
            Literal["asc", "desc"],
            Field(
                default="desc",
                description=(
                    'Sort order for history entries. "desc" (default): newest first. '
                    '"asc": oldest first (chronological, as returned by HA API). '
                    'Ignored when source="statistics".'
                ),
            ),
        ] = "desc",
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified top-level response keys to reduce "
                    "response size. None = full response (default). "
                    "History keys: success, source, entities, period, query_params. "
                    "Statistics keys: success, source, entities, period_type, time_range, "
                    "statistic_types, query_params, warnings."
                ),
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """
        Get historical data from Home Assistant's recorder.

        **Sources:**
        - "history" (default): Raw state changes, ~10 day retention, full resolution
        - "statistics": Pre-aggregated data, permanent retention, requires state_class

        **Shared params:** entity_ids, start_time, end_time, limit, offset
        **History params:** minimal_response, significant_changes_only
        **Statistics params:** period, statistic_types

        **Default time range:** 24h for history, 30 days for statistics

        **Use ha_get_history (default) when:**
        - Troubleshooting why a value changed ("Why was my bedroom cold last night?")
        - Checking event sequences ("Did my garage door open while I was away?")
        - Analyzing recent patterns ("What time does motion usually trigger?")

        **Use ha_get_history(source="statistics") when:**
        - Tracking long-term trends beyond 10 days ("Energy use this month vs last month?")
        - Computing period averages ("Average living room temperature over 6 months?")
        - Entities must have state_class (measurement, total, total_increasing)

        **WARNING:** limit and offset apply per entity (not globally across all entities).
        All data is fetched from HA before slicing; limit/offset are client-side.
        With multiple entity_ids, offset must be 0 — use a single entity_id for offset > 0.
        Use has_more and next_offset from the response to paginate.
        Administrators can optionally enable recorder workload guardrails in Advanced
        settings. When enabled, oversized entity/time workloads are rejected before the
        recorder query is issued; narrow the time range or entity list to stay within
        the budget. Calendar statistics may first read HA's configured timezone so the
        estimate follows local calendar boundaries.

        **Example -- history (default):**
        ```python
        ha_get_history(entity_ids="sensor.bedroom_temperature", start_time="24h")
        ha_get_history(entity_ids=["sensor.temperature", "sensor.humidity"], start_time="3d", limit=500)
        # Default order="desc" returns newest states first.
        # To paginate oldest-first, use order="asc":
        ha_get_history(entity_ids="sensor.temperature", start_time="7d", limit=100, offset=100, order="asc")
        ```

        **Example -- statistics:**
        ```python
        ha_get_history(source="statistics", entity_ids="sensor.total_energy_kwh", start_time="30d", period="day")
        ha_get_history(source="statistics", entity_ids="sensor.living_room_temperature",
                       start_time="6m", period="month", statistic_types=["mean", "min", "max"])
        ha_get_history(source="statistics", entity_ids="sensor.energy_kwh",
                       start_time="30d", period="5minute", limit=100, offset=200)
        ```
        """
        parsed_fields: list[str] | None = None
        if fields is not None:
            try:
                parsed_fields = parse_string_list_param(
                    fields, "fields", allow_csv=True
                )
            except ValueError as exc:
                raise_tool_error(create_validation_error(str(exc), parameter="fields"))
        try:
            # Parse entity_ids
            entity_id_list = _parse_entity_ids(entity_ids)

            # Offset > 0 is only supported for single-entity requests.
            # build_pagination_metadata applies per entity — limit=100 across
            # 5 entities returns up to 500 rows with no top-level has_more signal.
            _effective_offset = offset if offset is not None else 0
            if _effective_offset > 0 and len(entity_id_list) > 1:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "offset > 0 requires a single entity_id",
                        context={"offset": offset, "entity_count": len(entity_id_list)},
                        suggestions=[
                            "Use a single entity_id when offset > 0, or use offset=0 for multi-entity requests."
                        ],
                    )
                )

            # Source-dependent default hours
            default_hours = _DEFAULT_START_HOURS_BY_SOURCE[source]

            # Parse time parameters
            start_dt, end_dt = _parse_time_range(start_time, end_time, default_hours)
            query_settings = get_global_settings()
            statistics_timezone = await _prepare_guardrail_query(
                self._client,
                enabled=query_settings.enable_history_query_guardrails,
                source=source,
                period=period,
                start_dt=start_dt,
                end_dt=end_dt,
                end_time_was_provided=end_time is not None,
            )
            _validate_query_workload(
                source=source,
                entity_ids=entity_id_list,
                start_dt=start_dt,
                end_dt=end_dt,
                minimal_response=minimal_response,
                significant_changes_only=significant_changes_only,
                period=period,
                enforce_budget=query_settings.enable_history_query_guardrails,
                statistics_timezone=statistics_timezone,
            )

            await safe_info(
                ctx,
                f"ha_get_history starting: source={source} "
                f"entities={len(entity_id_list)} "
                f"window={start_dt.isoformat()}..{end_dt.isoformat()}",
            )
            await safe_progress(
                ctx,
                progress=0,
                total=3,
                message="connecting to Home Assistant WebSocket",
            )

            await safe_progress(
                ctx,
                progress=1,
                total=3,
                message=f"querying recorder ({source})",
            )

            # Route through the shared pooled WebSocket (issue #1813) instead of
            # a dedicated connect/auth handshake per call. Each source issues a
            # single request/response WS command; the pooled client owns the
            # connection lifecycle, so there is no per-call connect/disconnect.
            if source == "statistics":
                inner = await _fetch_statistics(
                    self._client,
                    entity_id_list,
                    start_dt,
                    end_dt,
                    period,
                    statistic_types,
                    limit,
                    offset,
                )
            else:
                inner = await _fetch_history(
                    self._client,
                    entity_id_list,
                    start_dt,
                    end_dt,
                    minimal_response,
                    significant_changes_only,
                    limit,
                    offset,
                    _DEFAULT_HISTORY_LIMIT,
                    _MAX_HISTORY_LIMIT,
                    order=order,
                )
            await safe_progress(
                ctx,
                progress=3,
                total=3,
                message="recorder query complete",
            )
            # Wrap first so the outer {"data": ..., "metadata": ...} shape
            # is always present; then project the inner data dict in-place
            # when caller requested field projection.
            _r = await add_timezone_metadata(self._client, inner)
            if parsed_fields is not None:
                _r["data"] = project_fields(_r["data"], parsed_fields)
            return _r

        except ToolError:
            raise
        except Exception as e:
            if source == "statistics":
                suggestions = [
                    "Check Home Assistant connection",
                    "Verify entities have state_class attribute",
                    "Ensure recorder component is enabled with statistics",
                ]
            else:
                suggestions = [
                    "Check Home Assistant connection",
                    "Verify entity IDs are correct",
                    "Ensure recorder component is enabled",
                ]
            exception_to_structured_error(e, suggestions=suggestions)
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )


def register_history_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register historical data access tools with the MCP server."""
    register_tool_methods(mcp, HistoryTools(client))


def _parse_entity_ids(entity_ids: str | list[str]) -> list[str]:
    """Parse entity_ids parameter into a list of strings."""
    if isinstance(entity_ids, str):
        if entity_ids.startswith("["):
            # Belt-and-suspenders: JSON_STRING_COERCION on the param already
            # parses a JSON-array string to a list upstream, so a string reaching
            # here is normally CSV/single. This branch stays as a fallback.
            parsed_ids = parse_string_list_param(entity_ids, "entity_ids")
            if parsed_ids is None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "entity_ids is required",
                        suggestions=["Provide at least one entity ID"],
                    )
                )
            return parsed_ids
        elif "," in entity_ids:
            result = [e.strip() for e in entity_ids.split(",") if e.strip()]
            if not result:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_MISSING_PARAMETER,
                        "entity_ids is required",
                        suggestions=["Provide at least one entity ID"],
                    )
                )
            return result
        else:
            return [entity_ids.strip()]
    if not entity_ids:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "entity_ids is required",
                suggestions=["Provide at least one entity ID"],
            )
        )

    return entity_ids


def _parse_time_range(
    start_time: str | None,
    end_time: str | None,
    default_hours: int,
) -> tuple[datetime, datetime]:
    """Parse start_time and end_time into datetime objects."""
    reference_time = datetime.now(UTC)
    try:
        start_dt = parse_relative_time(
            start_time,
            default_hours=default_hours,
            reference_time=reference_time,
        )
    except (ValueError, OverflowError) as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(e),
                context={"parameter": "start_time"},
                suggestions=[
                    "Use ISO format: '2025-01-25T00:00:00Z'",
                    "Use relative format: '24h', '7d', '2w', '1m'",
                ],
            )
        )

    if end_time:
        try:
            end_dt = parse_relative_time(
                end_time,
                default_hours=0,
                reference_time=reference_time,
            )
        except (ValueError, OverflowError) as e:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    str(e),
                    context={"parameter": "end_time"},
                    suggestions=["Use ISO format: '2025-01-26T00:00:00Z'"],
                )
            )
    else:
        end_dt = reference_time

    return start_dt, end_dt


def _validate_time_range(
    start_dt: datetime,
    end_dt: datetime,
    *,
    end_time_was_provided: bool = True,
    reject_zero_length: bool = True,
) -> None:
    """Reject reversed ranges and, when requested, zero-length ranges."""
    if end_dt < start_dt or (reject_zero_length and end_dt == start_dt):
        if not end_time_was_provided and start_dt > end_dt:
            message = "start_time must not be in the future when end_time is omitted"
            suggestions = ["Choose a start_time at or before the current time."]
        else:
            message = "end_time must be later than start_time"
            suggestions = ["Choose an end_time later than start_time."]
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                message,
                context={
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                },
                suggestions=suggestions,
            )
        )


def _validate_calendar_statistics_range(
    start_dt: datetime, end_dt: datetime, period: str
) -> None:
    """Reject boundary years that calendar alignment cannot advance safely."""
    if start_dt.year <= datetime.min.year or end_dt.year >= datetime.max.year:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Time range is outside the safe calendar alignment bounds for period='{period}'",
                context={
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                    "period": period,
                },
                suggestions=[
                    "Choose start_time and end_time between years 2 and 9998."
                ],
            )
        )


def _validate_query_workload(
    *,
    source: str,
    entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
    period: str,
    enforce_budget: bool,
    statistics_timezone: tzinfo = UTC,
) -> None:
    """Reject recorder requests likely to monopolize Home Assistant resources."""
    if not enforce_budget:
        return

    _validate_time_range(start_dt, end_dt)
    if source == "history":
        violation = _history_workload_violation(
            entity_ids,
            start_dt,
            end_dt,
            minimal_response,
            significant_changes_only,
        )
    else:
        violation = _statistics_workload_violation(
            entity_ids, start_dt, end_dt, period, statistics_timezone
        )

    if violation is None:
        return
    context, suggestions = violation

    suggestions.append(
        "An administrator can disable history query guardrails in Advanced settings after evaluating the workload risk."
    )
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            "Recorder query exceeds the safe workload budget",
            context=context,
            suggestions=suggestions,
        )
    )


def _history_workload_violation(
    entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
) -> tuple[dict[str, Any], list[str]] | None:
    """Return raw-history violation details, or None when within budget."""
    # Estimated multipliers account for the larger recorder payload when
    # filtering and attribute minimization are disabled; they are not measured
    # row counts.
    detail_weight = 1
    if not significant_changes_only:
        detail_weight *= 4
    if not minimal_response:
        detail_weight *= 2
    entity_count = len(entity_ids)
    estimated_entity_hours = (
        (end_dt - start_dt).total_seconds() / 3600 * entity_count * detail_weight
    )
    if (
        entity_count <= _MAX_HISTORY_ENTITIES
        and estimated_entity_hours <= _MAX_HISTORY_ENTITY_HOURS
    ):
        return None
    context = {
        "entity_count": entity_count,
        "max_entities": _MAX_HISTORY_ENTITIES,
        "estimated_entity_hours": round(estimated_entity_hours, 2),
        "max_entity_hours": _MAX_HISTORY_ENTITY_HOURS,
        "detail_weight": detail_weight,
        "minimal_response": minimal_response,
        "significant_changes_only": significant_changes_only,
    }
    suggestions = ["Query fewer entities or use a shorter time range."]
    if not significant_changes_only:
        suggestions.append("Set significant_changes_only=true to reduce recorder work.")
    if not minimal_response:
        suggestions.append("Set minimal_response=true to omit full state attributes.")
    suggestions.append(
        "Use source='statistics' for long ranges when the entities support long-term statistics."
    )
    return context, suggestions


def _statistics_workload_violation(
    entity_ids: list[str],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    statistics_timezone: tzinfo,
) -> tuple[dict[str, Any], list[str]] | None:
    """Return statistics violation details, or None when within budget."""
    if period not in _VALID_STATISTICS_PERIODS:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid statistics period: {period}",
                context={"period": period},
                suggestions=[
                    "Use one of: '5minute', 'hour', 'day', 'week', 'month', 'year'."
                ],
            )
        )
    scan_start, scan_end = _statistics_scan_window(
        start_dt, end_dt, period, statistics_timezone
    )
    scan_granularity_seconds = 300 if period == "5minute" else 3600
    max_statistics_rows = (
        _MAX_SHORT_TERM_STATISTICS_ROWS
        if period == "5minute"
        else _MAX_LONG_TERM_STATISTICS_ROWS
    )
    entity_count = len(entity_ids)
    estimated_rows = (
        math.ceil((scan_end - scan_start).total_seconds() / scan_granularity_seconds)
        * entity_count
    )
    if (
        entity_count <= _MAX_STATISTICS_ENTITIES
        and estimated_rows <= max_statistics_rows
    ):
        return None
    context = {
        "entity_count": entity_count,
        "max_entities": _MAX_STATISTICS_ENTITIES,
        "estimated_rows": estimated_rows,
        "max_estimated_rows": max_statistics_rows,
        "period": period,
        "scan_granularity_minutes": scan_granularity_seconds // 60,
        "scan_start_time": scan_start.isoformat(),
        "scan_end_time": scan_end.isoformat(),
    }
    suggestions = ["Query fewer entities or use a shorter time range."]
    if period == "5minute":
        suggestions.append("Use period='hour' to scan the long-term statistics table.")
    return context, suggestions


def _statistics_scan_window(
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    local_timezone: tzinfo,
) -> tuple[datetime, datetime]:
    """Mirror Core's calendar alignment before its statistics table query."""
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


def _raise_recorder_ws_failure(
    kind: str,
    error_msg: str,
    entity_id_list: list[str],
    suggestions: list[str],
) -> None:
    """Raise the structured error for a failed recorder WS call.

    The pooled ``send_websocket_message`` collapses transport failures into
    ``{"success": False, "error": ...}`` — classify connection-shaped errors
    as CONNECTION_FAILED (retry/connectivity guidance) instead of presenting
    recorder-retention suggestions during an HA restart or WS outage.
    """
    if is_connection_error_message(error_msg):
        raise_tool_error(
            create_error_response(
                ErrorCode.CONNECTION_FAILED,
                f"Failed to retrieve {kind}: {error_msg}",
                context={"entity_ids": entity_id_list},
                suggestions=[
                    "Home Assistant may be restarting or unreachable — retry shortly",
                    "Check the connection to Home Assistant",
                ],
            )
        )
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Failed to retrieve {kind}: {error_msg}",
            context={"entity_ids": entity_id_list},
            suggestions=suggestions,
        )
    )


async def _fetch_history(
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
    limit: int | None,
    offset: int | None,
    default_limit: int,
    max_limit: int,
    order: str = "desc",
) -> dict[str, Any]:
    """Execute the history/history_during_period WebSocket call.

    *order* controls state-list ordering: ``"desc"`` (default) returns the
    newest states first; ``"asc"`` returns the oldest first.

    Returns the unwrapped history dict; the caller is responsible for projection
    and wrapping with ``add_timezone_metadata``.
    """
    effective_limit = min(limit, max_limit) if limit is not None else default_limit
    effective_offset = offset if offset is not None else 0

    command_params = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "entity_ids": entity_id_list,
        "minimal_response": minimal_response,
        "significant_changes_only": significant_changes_only,
        "no_attributes": minimal_response,
    }

    response = await client.send_websocket_message(
        {"type": "history/history_during_period", **command_params}
    )

    if not response.get("success"):
        _raise_recorder_ws_failure(
            "history",
            response.get("error", "Unknown error"),
            entity_id_list,
            suggestions=[
                "Verify entity IDs exist using ha_search()",
                "Check that entities are recorded (not excluded from recorder)",
                "Ensure time range is within recorder retention period (~10 days)",
            ],
        )

    result_data = response.get("result", {})
    entities_history = []

    for entity_id in entity_id_list:
        entity_states = result_data.get(entity_id, [])
        if order == "desc":
            entity_states = list(reversed(entity_states))
        paged_states = entity_states[
            effective_offset : effective_offset + effective_limit
        ]

        formatted_states = []
        for state in paged_states:
            last_updated_raw = state.get("lu", state.get("last_updated"))
            last_changed_raw = state.get("lc", state.get("last_changed"))
            if last_changed_raw is None and last_updated_raw is not None:
                last_changed_raw = last_updated_raw

            state_entry = {
                "state": state.get("s", state.get("state")),
                "last_changed": _convert_timestamp(last_changed_raw),
                "last_updated": _convert_timestamp(last_updated_raw),
            }
            if not minimal_response:
                state_entry["attributes"] = state.get("a", state.get("attributes", {}))
            formatted_states.append(state_entry)

        pagination = build_pagination_metadata(
            total_count=len(entity_states),
            offset=effective_offset,
            limit=effective_limit,
            count=len(formatted_states),
        )
        entities_history.append(
            {
                "entity_id": entity_id,
                "period": {
                    "start": start_dt.isoformat(),
                    "end": end_dt.isoformat(),
                },
                "states": formatted_states,
                **pagination,
            }
        )

    history_data = {
        "success": True,
        "source": "history",
        "entities": entities_history,
        "period": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        },
        "query_params": {
            "minimal_response": minimal_response,
            "significant_changes_only": significant_changes_only,
            "limit": effective_limit,
            "offset": effective_offset,
            "order": order,
        },
    }

    return history_data


def _parse_statistic_types(
    statistic_types: str | list[str] | None,
) -> list[str] | None:
    """Parse and validate the statistic_types param into a list (or None for all)."""
    stat_types_list: list[str] | None = None
    if statistic_types is not None:
        if isinstance(statistic_types, str):
            if statistic_types.startswith("["):
                stat_types_list = parse_string_list_param(
                    statistic_types, "statistic_types"
                )
            elif "," in statistic_types:
                stat_types_list = [
                    s.strip() for s in statistic_types.split(",") if s.strip()
                ]
            else:
                stat_types_list = [statistic_types.strip()]
        else:
            stat_types_list = list(statistic_types)

        valid_types = ["mean", "min", "max", "sum", "state", "change"]
        assert stat_types_list is not None
        if not stat_types_list:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    "statistic_types cannot be an empty list. "
                    "Omit the parameter to retrieve all types, or specify at least one valid type.",
                    context={"parameter": "statistic_types", "value": statistic_types},
                    suggestions=[f"Use one or more of: {', '.join(valid_types)}"],
                )
            )
        invalid_types = [t for t in stat_types_list if t not in valid_types]
        if invalid_types:
            raise_tool_error(
                create_error_response(
                    ErrorCode.VALIDATION_INVALID_PARAMETER,
                    f"Invalid statistic types: {invalid_types}",
                    context={
                        "invalid_types": invalid_types,
                        "valid_types": valid_types,
                    },
                    suggestions=[f"Use one or more of: {', '.join(valid_types)}"],
                )
            )
    return stat_types_list


def _format_entity_statistics(
    result_data: dict[str, Any],
    entity_id_list: list[str],
    all_stat_types: list[str],
    period: str,
    effective_offset: int,
    effective_limit: int,
) -> list[dict[str, Any]]:
    """Format the per-entity statistics rows from a statistics_during_period result."""
    entities_statistics = []
    for entity_id in entity_id_list:
        entity_stats = result_data.get(entity_id, [])
        paged_stats = entity_stats[
            effective_offset : effective_offset + effective_limit
        ]
        formatted_stats = []
        unit = None

        for stat in paged_stats:
            stat_entry: dict[str, Any] = {"start": stat.get("start")}
            for stat_type in all_stat_types:
                if stat_type in stat:
                    stat_entry[stat_type] = stat[stat_type]
            if unit is None and "unit_of_measurement" in stat:
                unit = stat["unit_of_measurement"]
            formatted_stats.append(stat_entry)

        pagination = build_pagination_metadata(
            total_count=len(entity_stats),
            offset=effective_offset,
            limit=effective_limit,
            count=len(formatted_stats),
        )
        entities_statistics.append(
            {
                "entity_id": entity_id,
                "period": period,
                "statistics": formatted_stats,
                "unit_of_measurement": unit,
                **pagination,
            }
        )
    return entities_statistics


async def _fetch_statistics(
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    statistic_types: str | list[str] | None,
    limit: int | None,
    offset: int | None,
) -> dict[str, Any]:
    """Execute the recorder/statistics_during_period WebSocket call.

    Returns the unwrapped statistics dict; the caller is responsible for projection
    and wrapping with ``add_timezone_metadata``.
    """
    effective_limit = limit if limit is not None else _DEFAULT_HISTORY_LIMIT
    effective_offset = offset if offset is not None else 0

    # Validate period
    valid_periods = ["5minute", "hour", "day", "week", "month", "year"]
    if period not in valid_periods:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid period: {period}",
                context={"period": period, "valid_periods": valid_periods},
                suggestions=[f"Use one of: {', '.join(valid_periods)}"],
            )
        )

    stat_types_list = _parse_statistic_types(statistic_types)

    command_params: dict[str, Any] = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "statistic_ids": entity_id_list,
        "period": period,
    }
    if stat_types_list is not None:
        command_params["types"] = stat_types_list

    response = await client.send_websocket_message(
        {"type": "recorder/statistics_during_period", **command_params}
    )

    if not response.get("success"):
        _raise_recorder_ws_failure(
            "statistics",
            response.get("error", "Unknown error"),
            entity_id_list,
            suggestions=[
                "Verify entities have state_class attribute (measurement, total, total_increasing)",
                "Use ha_search() to check entity attributes",
                "Statistics are only available for entities that track numeric values",
            ],
        )

    result_data = response.get("result", {})
    all_stat_types = stat_types_list or ["mean", "min", "max", "sum", "state", "change"]
    entities_statistics = _format_entity_statistics(
        result_data,
        entity_id_list,
        all_stat_types,
        period,
        effective_offset,
        effective_limit,
    )

    empty_entities: list[str] = [
        str(e["entity_id"]) for e in entities_statistics if e["count"] == 0
    ]

    statistics_data: dict[str, Any] = {
        "success": True,
        "source": "statistics",
        "entities": entities_statistics,
        "period_type": period,
        "time_range": {
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
        },
        "statistic_types": all_stat_types,
        "query_params": {
            "statistic_types": stat_types_list,
            "limit": effective_limit,
            "offset": effective_offset,
        },
    }

    if empty_entities:
        statistics_data["warnings"] = [
            f"No statistics found for: {', '.join(empty_entities)}. "
            "These entities may not have state_class attribute or may not have recorded data yet."
        ]

    return statistics_data
