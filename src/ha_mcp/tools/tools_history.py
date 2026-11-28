"""
Historical data access tools for Home Assistant MCP server.

This module provides tools for accessing historical data from Home Assistant's
recorder component via a single consolidated tool:

ha_get_history -- Retrieve historical data with source-selectable mode:
  - source="history" (default): recorded state changes
  - source="statistics": the recorder's aggregated statistics
"""

import logging
import math
import re
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp import Context
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..config import get_global_settings
from ..errors import ErrorCode, create_error_response, create_validation_error
from ..history_summary import summarize_states
from .coercion import JSON_STRING_COERCION
from .core_contract import include_core_contract, merge_core_options
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    safe_progress,
)
from .history_coverage import history_coverage_warnings
from .history_pages import history_fields, history_pages, resolve_entities
from .history_response import (
    format_history_response,
    summary_warnings,
)
from .response_helpers import (
    add_timezone_metadata,
    build_pagination_metadata,
    project_fields,
    require_ha_timezone,
)
from .statistics_calendar import (
    _CALENDAR_STATISTICS_PERIODS,
    _statistics_scan_window,
)
from .statistics_reads import fetch_statistics, fetch_statistics_totals
from .tool_hints import read_only_hints

logger = logging.getLogger(__name__)

RELATIVE_TIME_UNIT_SECONDS = {
    "min": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
    "w": 7 * 24 * 60 * 60,
    "m": 30 * 24 * 60 * 60,
}
_RELATIVE_FORMAT_HINT = "Relative format: '30min', '24h', '7d', '2w', '1m' (= 30 days)"


def parse_relative_time(
    time_str: str | None,
    default_hours: int = 24,
    *,
    reference_time: datetime | None = None,
) -> datetime:
    """Parse a time string that can be either ISO format or relative (e.g., '24h', '7d').

    Args:
        time_str: Time string in ISO format or relative format (e.g., "30min", "24h", "7d", "2w", "1m" (= 30 days))
        default_hours: Default hours to go back if time_str is None
        reference_time: Reference datetime for relative values and defaults.

    Returns:
        A datetime in the parsed value's timezone (naive if it has no offset); relative values use ``reference_time``'s.
    """
    now = reference_time or datetime.now(UTC)
    if time_str is None:
        return now - timedelta(hours=default_hours)

    relative_pattern = r"^(\d+)(min|[hdwm])$"
    match = re.match(relative_pattern, time_str.lower().strip())

    if match:
        value = int(match.group(1))
        unit = match.group(2)
        try:
            return now - timedelta(seconds=value * RELATIVE_TIME_UNIT_SECONDS[unit])
        except OverflowError as exc:
            raise ValueError(
                f"Invalid time format: {time_str} is out of range"
            ) from exc

    try:
        return datetime.fromisoformat(time_str)
    except (ValueError, OverflowError) as e:
        raise ValueError(
            f"Invalid time format: {time_str}. Use ISO format or relative (e.g., '30min', '24h', '7d', '2w', '1m' (= 30 days))"
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
_ESTIMATED_STATISTICS_PERIODS = frozenset(
    {"5minute", "hour", *_CALENDAR_STATISTICS_PERIODS}
)


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
    return await require_ha_timezone(
        client,
        needed_for="calendar statistics estimation needs",
        workaround="Or use period='hour' or period='5minute', which need no calendar alignment",
    )


class HistoryTools:
    """Historical data access tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_history",
        tags={"History & Statistics"},
        annotations=read_only_hints(
            "Get Entity History or Statistics", open_world=False
        ),
    )
    @log_tool_usage
    async def ha_get_history(
        self,
        entity_ids: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description="Entity ID(s) to query: a single ID, comma-separated string, or JSON array. Required unless target is given."
            ),
        ] = None,
        source: Annotated[
            Literal["history", "statistics"],
            Field(
                description=(
                    'Data source: "history" for recorded state changes at full '
                    'resolution, or "statistics" for the recorder\'s aggregated '
                    "statistics, where entity_ids are statistic IDs (an entity's ID, "
                    'or an external statistic\'s "domain:name").'
                ),
                default="history",
            ),
        ] = "history",
        start_time: Annotated[
            str | None,
            Field(
                description="Start time: ISO datetime (HA local time without an offset) or a look-back from now with unit min, h, d, w or m (= 30 days), e.g. '30min' = 30 minutes ago. Default: 24h ago for history, 30d ago for statistics",
                default=None,
            ),
        ] = None,
        end_time: Annotated[
            str | None,
            Field(
                description="End time: same formats as start_time. Omit it to end at the current time",
                default=None,
            ),
        ] = None,
        # History-specific (ignored when source="statistics")
        minimal_response: Annotated[
            bool,
            Field(
                description="Core's compact rows: after an entity's first row, rows carry only the state and its time, "
                "and attribute-only updates are dropped. Rows of the domains in need_attribute_domains (see include_schema=True) keep attributes. "
                'Ignored when source="statistics"',
                default=True,
            ),
        ] = True,
        no_attributes: Annotated[
            bool,
            Field(
                description="Read no state attributes at all, the first row and the need_attribute_domains rows included. Less recorder work."
            ),
        ] = False,
        significant_changes_only: Annotated[
            bool,
            Field(
                description="Filter to significant state changes only. Ignored when "
                'source="statistics"',
                default=True,
            ),
        ] = True,
        limit: Annotated[
            int | None,
            Field(
                description='Max entries per entity. Default: 100. For source="history": state '
                "rows; with summary=True, the states listed in summary.states, longest "
                'first. For source="statistics": aggregated rows. With multiple '
                "entity_ids, total rows returned can reach limit × len(entity_ids).",
                default=None,
                ge=1,
                le=1000,
            ),
        ] = None,
        offset: Annotated[
            int | None,
            Field(
                description="Number of entries to skip per entity for pagination.",
                default=None,
                ge=0,
            ),
        ] = None,
        # Statistics-specific (ignored when source="history")
        period: Annotated[
            str,
            Field(
                description="Aggregation period. See include_schema=True for the periods Core accepts."
                ' Ignored when source="history"',
                default="day",
            ),
        ] = "day",
        statistic_types: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                description='Native Core statistics types (for example "sum", "change", "last_reset"). Default: Core chooses all. Core accepts a different set with summary=True; include_schema=True shows the one for the call made. Ignored when source="history"',
                default=None,
            ),
        ] = None,
        order: Annotated[
            Literal["asc", "desc"],
            Field(
                default="desc",
                description=(
                    'Sort order for history entries. "desc": newest first. "asc": oldest '
                    'first. Ignored when source="statistics".'
                ),
            ),
        ] = "desc",
        summary: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "Return per-entity totals instead of state rows: changes "
                    "(switches to a different state; the state at the window start is "
                    "not one, but without that state, e.g. with core_options "
                    "{'include_start_time_state': false} or purged history, the first "
                    "recorded state counts as one), first_change and last_change (null "
                    "without changes), and per state its periods (separate spells) and "
                    "duration_s (seconds, up to end_time or now, whichever is "
                    "earlier). distinct_states counts every state, listed or not. "
                    'With source="statistics": one max, mean, min and change per '
                    "statistic over the whole window, computed by Core; "
                    "statistic_types selects among them, and period and limit "
                    "are ignored."
                ),
            ),
        ] = False,
        fields: Annotated[
            str | list[str] | None,
            JSON_STRING_COERCION,
            Field(
                default=None,
                description=(
                    "Return only the specified keys within data to reduce response "
                    "size. None = full response. History keys: success, source, entities, "
                    "period, query_params. Statistics keys: success, source, entities, "
                    "period_type, time_range, statistic_types, query_params (a "
                    "statistics summary has no period_type or statistic_types). "
                    "Warnings and timezone metadata remain outside data and are "
                    "always retained."
                ),
            ),
        ] = None,
        include_schema: Annotated[
            bool,
            Field(
                description="Include the running Core's native request schema when the component supports discovery; a history read also gets Core's need_attribute_domains."
            ),
        ] = False,
        core_options: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Additional native Core request fields, e.g. {'units': {'energy': 'MWh'}} for statistics; a statistics summary also takes Core's {'calendar': {'period': 'month'}} or 'rolling_window' in place of the window. Core validates them. Cannot override fields controlled by the tool's parameters or query limits."
            ),
        ] = None,
        target: Annotated[
            dict[str, Any] | None,
            JSON_STRING_COERCION,
            Field(
                description="Home Assistant target, combined with entity_ids: any of area_id, device_id, floor_id, label_id, entity_id, each an ID or a list. Core resolves it; IDs it cannot find come back in warnings."
            ),
        ] = None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Get historical data from Home Assistant's recorder.

        WHEN NOT TO USE:
        For current values, use ha_get_state. To inspect Energy Dashboard
        preferences and configured statistics, use
        ha_manage_energy_prefs(mode="get", include_statistics=True). To list or
        search the recorder's statistics (a meter's statistic ID by name or
        alias), see Core's issues with them, or change recorded data, use
        ha_manage_logs.

        WHEN TO USE:
        Use source="history" (default) to troubleshoot why a value changed, check
        event sequences, or analyze recent patterns. Use summary=True when the
        question needs a count or a duration ("was it ever unavailable?") rather
        than the rows. Use source="statistics" for long-term trends and period
        averages.

        CAVEATS:
        When the recorder holds less than the requested window, the rows it has
        come back with the reason in warnings.
        History rows rename Core's compact keys once: s -> state, a -> attributes,
        lu -> last_updated, lc -> last_changed. History event times are ISO strings
        in HA's timezone (UTC if unavailable). When Core omits lc, last_changed
        falls back to last_updated; this may not be the actual state-change time
        with significant_changes_only (default) or a window-start snapshot.
        Attributes and unknown fields pass through unchanged.
        Statistics timestamps use Unix milliseconds. Minimal history may omit attributes.
        No duplicate compact aliases are added. include_schema=True
        retains core_contract even when fields selects other data keys.

        Output units come from
        Core recorder metadata and reflect its display-unit conversion; unresolved
        units include a reason. statistics_metadata preserves Core's native fields.

        The component pages and summarizes history inside the recorder executor; without it
        the whole window is downloaded and a warning says so.
        With multiple entity_ids, offset must be 0 — use a single entity_id for
        offset > 0. Use has_more and next_offset from the response to paginate.
        summary=True does not combine with offset > 0.
        Administrators can optionally enable recorder workload guardrails in
        Advanced settings. When enabled, oversized entity/time workloads are
        rejected before the recorder query is issued; narrow the time range or
        entity list to stay within the budget. Calendar statistics may first
        read HA's configured timezone so the estimate follows local calendar
        boundaries.

        EXAMPLES:
        - ha_get_history(entity_ids="sensor.bedroom_temperature", start_time="24h")
        - Paginate oldest-first: ha_get_history(entity_ids="sensor.temperature", start_time="7d", limit=100, offset=100, order="asc")
        - Time per state: ha_get_history(entity_ids="binary_sensor.door", start_time="7d", summary=True)
        - ha_get_history(source="statistics", entity_ids="sensor.living_room_temperature", start_time="6m", period="month", statistic_types=["mean", "min", "max"])
        """
        parsed_fields = history_fields(fields)
        try:
            entity_id_list, target_warnings = await resolve_entities(
                self._client, entity_ids, target
            )
            _validate_offset(
                offset,
                entity_count=len(entity_id_list),
                summary=summary,
            )

            default_hours = _DEFAULT_START_HOURS_BY_SOURCE[source]
            now = datetime.now(UTC)
            start_dt, end_dt = _parse_time_range(
                start_time, end_time, default_hours, now
            )
            start_dt, end_dt = await _read_in_ha_timezone(
                self._client, start_dt, end_dt
            )
            query_settings = get_global_settings()
            if (
                source == "statistics"
                and summary
                and query_settings.enable_history_query_guardrails
                and ({"calendar", "rolling_window"} & (core_options or {}).keys())
            ):
                raise_tool_error(
                    create_validation_error(
                        "HA-MCP cannot estimate a native calendar or rolling summary window. "
                        "Use start_time and end_time with recorder workload guardrails enabled.",
                        parameter="core_options",
                    )
                )
            # A guarded statistics summary sends one fixed window and no period;
            # it is budgeted as an hourly scan of that window.
            guard_period = "hour" if source == "statistics" and summary else period
            statistics_timezone = await _prepare_guardrail_query(
                self._client,
                enabled=query_settings.enable_history_query_guardrails,
                source=source,
                period=guard_period,
                start_dt=start_dt,
                end_dt=end_dt,
                end_time_was_provided=end_time is not None,
            )
            _validate_query_workload(
                source=source,
                entity_ids=entity_id_list,
                start_dt=start_dt,
                end_dt=end_dt,
                reads_attributes=not (no_attributes or summary),
                significant_changes_only=significant_changes_only,
                period=guard_period,
                enforce_budget=query_settings.enable_history_query_guardrails,
                statistics_timezone=statistics_timezone,
            )

            logger.debug(
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
            # a dedicated connect/auth handshake per call. Statistics also fetches
            # native recorder metadata through the same pooled client.
            if source == "statistics" and summary:
                inner = await fetch_statistics_totals(
                    self._client,
                    entity_id_list,
                    start_dt,
                    end_dt,
                    statistic_types,
                    core_options,
                )
            elif source == "statistics":
                inner = await fetch_statistics(
                    self._client,
                    entity_id_list,
                    start_dt,
                    end_dt,
                    period,
                    statistic_types,
                    limit,
                    offset,
                    core_options=core_options,
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
                    # A summary counts states and times only.
                    no_attributes=no_attributes or summary,
                    core_options=core_options,
                    summary_until=min(end_dt, now) if summary else None,
                )
            if target_warnings:
                inner["warnings"] = target_warnings + inner.get("warnings", [])
            if include_schema:
                command = "history/history_during_period"
                if source == "statistics":
                    command = (
                        "recorder/statistic_during_period"
                        if summary
                        else "recorder/statistics_during_period"
                    )
                await include_core_contract(self._client, inner, command)
            await safe_progress(
                ctx,
                progress=3,
                total=3,
                message="recorder query complete",
            )
            # Wrap first so the outer {"data": ..., "metadata": ...} shape
            # is always present; then project the inner data dict and lift
            # the typo warning project_fields may add to it.
            _r = await add_timezone_metadata(
                self._client, inner, convert_timestamps=False
            )
            _r = format_history_response(_r)
            _r["data"] = project_fields(
                _r["data"],
                parsed_fields,
                extra_always_keep=frozenset({"core_contract"})
                if include_schema
                else None,
            )
            if warnings := _r["data"].pop("warnings", None):
                _r.setdefault("warnings", []).extend(warnings)
            return _r

        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001
            exception_to_structured_error(e)
            return (
                None  # exception_to_structured_error always raises; explicit for CodeQL
            )


def register_history_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register historical data access tools with the MCP server."""
    register_tool_methods(mcp, HistoryTools(client))


def _validate_offset(offset: int | None, *, entity_count: int, summary: bool) -> None:
    """Reject offsets the response could not honour."""
    if not offset:
        return
    # Summary first: dropping to one entity_id alone would still be rejected.
    if summary:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "summary=True covers the whole window and does not take an offset",
                context={"offset": offset},
                suggestions=[
                    "Omit offset, or set summary=False to page through state rows."
                ],
            )
        )
    # build_pagination_metadata applies per entity — limit=100 across
    # 5 entities returns up to 500 rows with no top-level has_more signal.
    if entity_count > 1:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "offset > 0 requires a single entity_id",
                context={"offset": offset, "entity_count": entity_count},
                suggestions=[
                    "Use a single entity_id when offset > 0, or use offset=0 for multi-entity requests."
                ],
            )
        )


def _parse_time_range(
    start_time: str | None,
    end_time: str | None,
    default_hours: int,
    reference_time: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Parse start_time and end_time into datetime objects."""
    reference_time = reference_time or datetime.now(UTC)
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
                    "ISO format: '2025-01-25T00:00:00Z'",
                    _RELATIVE_FORMAT_HINT,
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
                    suggestions=[
                        "ISO format: '2025-01-26T00:00:00Z'",
                        _RELATIVE_FORMAT_HINT,
                    ],
                )
            )
    else:
        end_dt = reference_time

    return start_dt, end_dt


async def _read_in_ha_timezone(
    client: Any, start_dt: datetime, end_dt: datetime
) -> tuple[datetime, datetime]:
    """Read offset-less times in Home Assistant's zone, as Core does, or refuse."""
    if None not in (start_dt.tzinfo, end_dt.tzinfo):
        return start_dt, end_dt
    try:
        tz = await require_ha_timezone(
            client,
            needed_for="a time without an offset needs",
            workaround="Or give each time with an offset",
        )
    except ToolError:
        # Two given times without an offset compare by wall clock without the
        # zone: a reversed range, the caller's own mistake, is named first.
        if start_dt.tzinfo is None and end_dt.tzinfo is None:
            _validate_time_range(
                start_dt.replace(tzinfo=UTC),
                end_dt.replace(tzinfo=UTC),
                reject_zero_length=False,
            )
        raise
    start_dt = start_dt.replace(tzinfo=start_dt.tzinfo or tz)
    end_dt = end_dt.replace(tzinfo=end_dt.tzinfo or tz)
    return start_dt, end_dt


def _validate_time_range(
    start_dt: datetime,
    end_dt: datetime,
    *,
    end_time_was_provided: bool = True,
    reject_zero_length: bool = True,
) -> None:
    """Reject reversed ranges and, when requested, zero-length ranges."""
    # Same-zone datetimes compare by wall clock; a start in the spring-forward
    # gap can be a later instant than an end after the gap.
    start_ts, end_ts = start_dt.timestamp(), end_dt.timestamp()
    if end_ts < start_ts or (reject_zero_length and end_ts == start_ts):
        if not end_time_was_provided and start_ts > end_ts:
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
    reads_attributes: bool,
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
            reads_attributes,
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
    reads_attributes: bool,
    significant_changes_only: bool,
) -> tuple[dict[str, Any], list[str]] | None:
    """Return raw-history violation details, or None when within budget."""
    # Estimated multipliers account for the larger recorder payload when
    # filtering is off or attributes are read; they are not measured row counts.
    # Core decides per domain which rows keep attributes, so any read of them
    # counts.
    detail_weight = 1
    if not significant_changes_only:
        detail_weight *= 4
    if reads_attributes:
        detail_weight *= 2
    entity_count = len(entity_ids)
    hours = (end_dt.timestamp() - start_dt.timestamp()) / 3600  # elapsed, DST-safe
    estimated_entity_hours = hours * entity_count * detail_weight
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
        "reads_attributes": reads_attributes,
        "significant_changes_only": significant_changes_only,
    }
    suggestions = ["Query fewer entities or use a shorter time range."]
    if not significant_changes_only:
        suggestions.append("Set significant_changes_only=true to reduce recorder work.")
    if reads_attributes:
        suggestions.append("Set no_attributes=true to read no state attributes.")
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
    if period not in _ESTIMATED_STATISTICS_PERIODS:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Cannot safely estimate recorder workload for period: {period}",
                context={"period": period},
                suggestions=[
                    "HA-MCP estimates "
                    + ", ".join(sorted(_ESTIMATED_STATISTICS_PERIODS))
                    + ". See include_schema=True for Core's accepted periods."
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
    seconds = scan_end.timestamp() - scan_start.timestamp()  # elapsed, DST-safe
    estimated_rows = math.ceil(seconds / scan_granularity_seconds) * entity_count
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
    core_options: dict[str, Any] | None = None,
    summary_until: datetime | None = None,
    no_attributes: bool = False,
) -> dict[str, Any]:
    """Read one page or summary per entity through ``history_pages``.

    *order* controls state-list ordering: ``"desc"`` (default) returns the
    newest states first; ``"asc"`` returns the oldest first. With
    *summary_until*, each entity carries ``summarize_states`` totals up to that
    time instead of its rows.

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
        "no_attributes": no_attributes,
    }

    command_params = merge_core_options(command_params, core_options)
    # The late-rows warning rests on this state; core_options may drop it.
    command_params.setdefault("include_start_time_state", True)
    pages, warnings = await history_pages(
        client,
        command_params,
        effective_limit,
        effective_offset,
        order,
        start_dt.timestamp(),
        summary_until.timestamp() if summary_until else None,
    )
    entities_history = []
    summaries: dict[str, dict[str, Any]] = {}
    start_ts = start_dt.timestamp()
    period = {"start": start_dt.isoformat(), "end": end_dt.isoformat()}

    for entity_id in entity_id_list:
        page = pages.get(entity_id, {})
        if summary_until is not None:
            summary = summaries[entity_id] = page.get("summary") or summarize_states(
                [], start_ts, summary_until.timestamp(), effective_limit
            )
            entities_history.append(
                {"entity_id": entity_id, "period": period, "summary": summary}
            )
            continue
        paged_states = page.get("states", [])

        pagination = build_pagination_metadata(
            total_count=page.get("total_count", 0),
            offset=effective_offset,
            limit=effective_limit,
            count=len(paged_states),
        )
        entities_history.append(
            {
                "entity_id": entity_id,
                "period": period,
                "states": paged_states,
                **pagination,
            }
        )

    history_data = {
        "success": True,
        "source": "history",
        "entities": entities_history,
        "period": period,
        "query_params": {
            "minimal_response": minimal_response,
            "no_attributes": no_attributes,
            "significant_changes_only": significant_changes_only,
            "limit": effective_limit,
            "offset": effective_offset,
            "order": order,
            "summary": summary_until is not None,
        },
    }
    # add_timezone_metadata drops an empty list
    history_data["warnings"] = (
        warnings
        + summary_warnings(summaries)
        + await history_coverage_warnings(
            client,
            {
                entity_id: pages.get(entity_id, {}).get("first")
                for entity_id in entity_id_list
            },
            start_ts,
            window_start_state=bool(command_params["include_start_time_state"]),
        )
    )

    return history_data
