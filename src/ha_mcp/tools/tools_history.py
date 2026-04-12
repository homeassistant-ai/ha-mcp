"""
Historical data access tools for Home Assistant MCP server.

This module provides tools for accessing historical data from Home Assistant's
recorder component via a single consolidated tool:

ha_get_history -- Retrieve historical data with source-selectable mode:
  - source="history" (default): Raw state changes, ~10 day retention
  - source="statistics": Pre-aggregated long-term statistics, permanent retention
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from ..errors import ErrorCode, create_error_response
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
)
from .util_helpers import (
    add_timezone_metadata,
    build_pagination_metadata,
    coerce_int_param,
    parse_string_list_param,
)

logger = logging.getLogger(__name__)


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


def parse_relative_time(time_str: str | None, default_hours: int = 24) -> datetime:
    """
    Parse a time string that can be either ISO format or relative (e.g., '24h', '7d').

    Args:
        time_str: Time string in ISO format or relative format (e.g., "24h", "7d", "2w", "1m" where 1m = 30 days)
        default_hours: Default hours to go back if time_str is None

    Returns:
        datetime object in UTC
    """
    if time_str is None:
        return datetime.now(UTC) - timedelta(hours=default_hours)

    # Check for relative time format
    relative_pattern = r"^(\d+)([hdwm])$"
    match = re.match(relative_pattern, time_str.lower().strip())

    if match:
        value = int(match.group(1))
        unit = match.group(2)

        if unit == "h":
            return datetime.now(UTC) - timedelta(hours=value)
        elif unit == "d":
            return datetime.now(UTC) - timedelta(days=value)
        elif unit == "w":
            return datetime.now(UTC) - timedelta(weeks=value)
        elif unit == "m":
            # Approximate month as 30 days
            return datetime.now(UTC) - timedelta(days=value * 30)

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
    except ValueError as e:
        raise ValueError(
            f"Invalid time format: {time_str}. Use ISO format or relative (e.g., '24h', '7d', '2w', '1m')"
        ) from e


# Source-dependent default look-back periods
_DEFAULT_START_HOURS_BY_SOURCE: dict[str, int] = {"history": 24, "statistics": 30 * 24}

# Default and maximum limits for history entries
_DEFAULT_HISTORY_LIMIT = 100
_MAX_HISTORY_LIMIT = 1000


class HistoryTools:
    """Historical data access tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_get_history",
        tags={"History & Statistics"},
        annotations={
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
            int | str | None,
            Field(
                description='Max entries per entity. Default: 100, Max: 1000. For source="history": state changes. For source="statistics": aggregated rows.',
                default=None,
            ),
        ] = None,
        offset: Annotated[
            int | str,
            Field(
                description="Number of entries to skip per entity for pagination. Default: 0. Use with limit and has_more/next_offset in the response.",
                default=0,
            ),
        ] = 0,
        # Statistics-specific (ignored when source="history")
        period: Annotated[
            str,
            Field(
                description='Aggregation period: "5minute", "hour", "day", "week", "month". Default: "day". Ignored when source="history"',
                default="day",
            ),
        ] = "day",
        statistic_types: Annotated[
            str | list[str] | None,
            Field(
                description='Statistics types: "mean", "min", "max", "sum", "state", "change". Default: all. Ignored when source="history"',
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Retrieve historical data from Home Assistant's recorder.

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

        **NOTE:** limit and offset apply per entity (not globally across all entities).
        All data is fetched from HA before slicing; limit/offset are client-side.
        Use has_more and next_offset from the response to paginate.

        **Example -- history (default):**
        ```python
        ha_get_history(entity_ids="sensor.bedroom_temperature", start_time="24h")
        ha_get_history(entity_ids=["sensor.temperature", "sensor.humidity"], start_time="7d", limit=500)
        ha_get_history(entity_ids="sensor.temperature", start_time="7d", limit=100, offset=100)
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
        try:
            # Parse entity_ids
            entity_id_list = _parse_entity_ids(entity_ids)

            # Source-dependent default hours
            default_hours = _DEFAULT_START_HOURS_BY_SOURCE[source]

            # Parse time parameters
            start_dt, end_dt = _parse_time_range(start_time, end_time, default_hours)

            # Connect to WebSocket (shared by both sources)
            ws_client, error = await get_connected_ws_client(
                self._client.base_url, self._client.token
            )
            if error or ws_client is None:
                raise_tool_error(error or create_error_response(
                    ErrorCode.CONNECTION_FAILED,
                    "Failed to connect to Home Assistant WebSocket",
                ))

            try:
                if source == "statistics":
                    return await _fetch_statistics(
                        ws_client, self._client, entity_id_list,
                        start_dt, end_dt, period, statistic_types,
                        limit, offset,
                    )
                else:
                    return await _fetch_history(
                        ws_client, self._client, entity_id_list,
                        start_dt, end_dt, minimal_response,
                        significant_changes_only, limit, offset,
                        _DEFAULT_HISTORY_LIMIT, _MAX_HISTORY_LIMIT,
                    )
            finally:
                if ws_client:
                    await ws_client.disconnect()

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


def register_history_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register historical data access tools with the MCP server."""
    register_tool_methods(mcp, HistoryTools(client))


def _parse_entity_ids(entity_ids: str | list[str]) -> list[str]:
    """Parse entity_ids parameter into a list of strings."""
    if isinstance(entity_ids, str):
        if entity_ids.startswith("["):
            parsed_ids = parse_string_list_param(entity_ids, "entity_ids")
            if parsed_ids is None:
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "entity_ids is required",
                    suggestions=["Provide at least one entity ID"],
                ))
            return parsed_ids
        elif "," in entity_ids:
            result = [e.strip() for e in entity_ids.split(",") if e.strip()]
            if not result:
                raise_tool_error(create_error_response(
                    ErrorCode.VALIDATION_MISSING_PARAMETER,
                    "entity_ids is required",
                    suggestions=["Provide at least one entity ID"],
                ))
            return result
        else:
            return [entity_ids.strip()]
    if not entity_ids:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_MISSING_PARAMETER,
            "entity_ids is required",
            suggestions=["Provide at least one entity ID"],
        ))

    return entity_ids


def _parse_time_range(
    start_time: str | None,
    end_time: str | None,
    default_hours: int,
) -> tuple[datetime, datetime]:
    """Parse start_time and end_time into datetime objects."""
    try:
        start_dt = parse_relative_time(start_time, default_hours=default_hours)
    except ValueError as e:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            str(e),
            context={"parameter": "start_time"},
            suggestions=[
                "Use ISO format: '2025-01-25T00:00:00Z'",
                "Use relative format: '24h', '7d', '2w', '1m'",
            ],
        ))

    if end_time:
        try:
            end_dt = parse_relative_time(end_time, default_hours=0)
        except ValueError as e:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                str(e),
                context={"parameter": "end_time"},
                suggestions=["Use ISO format: '2025-01-26T00:00:00Z'"],
            ))
    else:
        end_dt = datetime.now(UTC)

    return start_dt, end_dt


async def _fetch_history(
    ws_client: Any,
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    minimal_response: bool,
    significant_changes_only: bool,
    limit: int | str | None,
    offset: int | str,
    default_limit: int,
    max_limit: int,
) -> dict[str, Any]:
    """Execute the history/history_during_period WebSocket call."""
    try:
        effective_limit = coerce_int_param(
            limit,
            param_name="limit",
            default=default_limit,
            min_value=1,
            max_value=max_limit,
        ) or default_limit
    except ValueError as e:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            str(e),
            context={"parameter": "limit"},
            suggestions=["Provide limit as an integer (e.g., 100)"],
        ))

    try:
        effective_offset = coerce_int_param(
            offset,
            param_name="offset",
            default=0,
            min_value=0,
        )
    except ValueError as e:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            str(e),
            context={"parameter": "offset"},
            suggestions=["Provide offset as a non-negative integer (e.g., 0)"],
        ))

    command_params = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "entity_ids": entity_id_list,
        "minimal_response": minimal_response,
        "significant_changes_only": significant_changes_only,
        "no_attributes": minimal_response,
    }

    response = await ws_client.send_command(
        "history/history_during_period", **command_params
    )

    if not response.get("success"):
        error_msg = response.get("error", "Unknown error")
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Failed to retrieve history: {error_msg}",
            context={"entity_ids": entity_id_list},
            suggestions=[
                "Verify entity IDs exist using ha_search_entities()",
                "Check that entities are recorded (not excluded from recorder)",
                "Ensure time range is within recorder retention period (~10 days)",
            ],
        ))

    result_data = response.get("result", {})
    entities_history = []

    for entity_id in entity_id_list:
        entity_states = result_data.get(entity_id, [])
        paged_states = entity_states[effective_offset : effective_offset + effective_limit]

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
        entities_history.append({
            "entity_id": entity_id,
            "period": {
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
            },
            "states": formatted_states,
            **pagination,
        })

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
        },
    }

    return await add_timezone_metadata(client, history_data)


async def _fetch_statistics(
    ws_client: Any,
    client: Any,
    entity_id_list: list[str],
    start_dt: datetime,
    end_dt: datetime,
    period: str,
    statistic_types: str | list[str] | None,
    limit: int | str | None,
    offset: int | str,
) -> dict[str, Any]:
    """Execute the recorder/statistics_during_period WebSocket call."""
    # Coerce limit and offset
    try:
        effective_limit = coerce_int_param(
            limit,
            param_name="limit",
            default=_DEFAULT_HISTORY_LIMIT,
            min_value=1,
            max_value=_MAX_HISTORY_LIMIT,
        )
    except ValueError as e:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            str(e),
            context={"parameter": "limit"},
            suggestions=["Provide limit as an integer (e.g., 100)"],
        ))

    try:
        effective_offset = coerce_int_param(
            offset,
            param_name="offset",
            default=0,
            min_value=0,
        )
    except ValueError as e:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            str(e),
            context={"parameter": "offset"},
            suggestions=["Provide offset as a non-negative integer (e.g., 0)"],
        ))

    # Validate period
    valid_periods = ["5minute", "hour", "day", "week", "month"]
    if period not in valid_periods:
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid period: {period}",
            context={"period": period, "valid_periods": valid_periods},
            suggestions=[f"Use one of: {', '.join(valid_periods)}"],
        ))

    # Parse statistic_types
    stat_types_list: list[str] | None = None
    if statistic_types:
        if isinstance(statistic_types, str):
            if statistic_types.startswith("["):
                stat_types_list = parse_string_list_param(statistic_types, "statistic_types")
            elif "," in statistic_types:
                stat_types_list = [s.strip() for s in statistic_types.split(",") if s.strip()]
            else:
                stat_types_list = [statistic_types.strip()]
        else:
            stat_types_list = list(statistic_types)

        valid_types = ["mean", "min", "max", "sum", "state", "change"]
        if stat_types_list is None:
            stat_types_list = []
        invalid_types = [t for t in stat_types_list if t not in valid_types]
        if invalid_types:
            raise_tool_error(create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Invalid statistic types: {invalid_types}",
                context={"invalid_types": invalid_types, "valid_types": valid_types},
                suggestions=[f"Use one or more of: {', '.join(valid_types)}"],
            ))

    command_params: dict[str, Any] = {
        "start_time": start_dt.isoformat(),
        "end_time": end_dt.isoformat(),
        "statistic_ids": entity_id_list,
        "period": period,
    }
    if stat_types_list is not None:
        command_params["types"] = stat_types_list

    response = await ws_client.send_command(
        "recorder/statistics_during_period", **command_params
    )

    if not response.get("success"):
        error_msg = response.get("error", "Unknown error")
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Failed to retrieve statistics: {error_msg}",
            context={"entity_ids": entity_id_list},
            suggestions=[
                "Verify entities have state_class attribute (measurement, total, total_increasing)",
                "Use ha_search_entities() to check entity attributes",
                "Statistics are only available for entities that track numeric values",
            ],
        ))

    result_data = response.get("result", {})
    entities_statistics = []
    all_stat_types = stat_types_list or ["mean", "min", "max", "sum", "state", "change"]

    for entity_id in entity_id_list:
        entity_stats = result_data.get(entity_id, [])
        paged_stats = entity_stats[effective_offset : effective_offset + effective_limit]
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
        entities_statistics.append({
            "entity_id": entity_id,
            "period": period,
            "statistics": formatted_stats,
            "unit_of_measurement": unit,
            **pagination,
        })

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
    }

    if empty_entities:
        statistics_data["warnings"] = [
            f"No statistics found for: {', '.join(empty_entities)}. "
            "These entities may not have state_class attribute or may not have recorded data yet."
        ]

    return await add_timezone_metadata(client, statistics_data)
