"""
Historical data access tools for Home Assistant MCP server.

This module provides tools for accessing historical data from Home Assistant's
recorder component. It includes:

1. ha_get_history - Retrieve raw state change history (short-term, full resolution)
2. ha_get_statistics - Retrieve pre-aggregated long-term statistics for trend analysis

These tools serve different but complementary purposes:
- History: Raw state changes, ~10 days retention, full resolution
- Statistics: Pre-aggregated data, permanent retention, hourly minimum resolution
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from .helpers import get_connected_ws_client, log_tool_usage
from .util_helpers import add_timezone_metadata, coerce_int_param, parse_string_list_param

logger = logging.getLogger(__name__)


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


def register_history_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register historical data access tools with the MCP server."""

    # Default and maximum limits for history entries
    DEFAULT_HISTORY_LIMIT = 100
    MAX_HISTORY_LIMIT = 1000

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["history"], "title": "Get Entity History"})
    @log_tool_usage
    async def ha_get_history(
        entity_ids: Annotated[
            str | list[str],
            Field(
                description="Entity ID(s) to query. Can be a single ID, comma-separated string, or JSON array."
            ),
        ],
        start_time: Annotated[
            str | None,
            Field(
                description="Start time: ISO datetime or relative (e.g., '24h', '7d', '2w'). Default: 24h ago",
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
        minimal_response: Annotated[
            bool,
            Field(
                description="Return only states/timestamps without attributes. Default: true",
                default=True,
            ),
        ] = True,
        significant_changes_only: Annotated[
            bool,
            Field(
                description="Filter to significant state changes only. Default: true",
                default=True,
            ),
        ] = True,
        limit: Annotated[
            int | str | None,
            Field(
                description="Max state changes per entity. Default: 100, Max: 1000",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Retrieve raw state change history for entities (last ~10 days).

        Returns the full-resolution state history from Home Assistant's recorder.
        This data shows every individual state change for the specified entities.

        **Data Characteristics:**
        - Full resolution: Every state transition captured
        - Retention: ~10 days (configurable via recorder.purge_keep_days)
        - Best for: Troubleshooting, pattern analysis, specific event queries

        **Parameters:**
        - entity_ids: Entity ID(s) to query (required)
        - start_time: Start of period - ISO datetime or relative ('24h', '7d', '2w'). Default: 24h ago
        - end_time: End of period - ISO datetime. Default: now
        - minimal_response: Omit attributes for smaller response. Default: true
        - significant_changes_only: Filter to actual state changes. Default: true
        - limit: Max entries per entity. Default: 100, Max: 1000

        **Use Cases:**
        - "Why was my bedroom cold last night?" - Query temperature sensor history
        - "Did my garage door open while I was away?" - Check cover state changes
        - "What time does motion usually trigger?" - Analyze binary sensor patterns
        - "Debug automation triggers" - See exact state change sequence

        **Example:**
        ```python
        # Get temperature history for the last 24 hours
        ha_get_history(entity_ids="sensor.bedroom_temperature")

        # Get multiple entity history for last 7 days
        ha_get_history(
            entity_ids=["sensor.temperature", "sensor.humidity"],
            start_time="7d",
            limit=500
        )

        # Get full attributes for debugging
        ha_get_history(
            entity_ids="light.living_room",
            start_time="2025-01-25T00:00:00Z",
            end_time="2025-01-26T00:00:00Z",
            minimal_response=False
        )
        ```

        **Note:** For long-term trends (>10 days), use ha_get_statistics() instead.

        **Returns:**
        - List of entities with their state history
        - Each entity includes: entity_id, period, states array, count
        """
        try:
            # Parse entity_ids - handle string, list, or comma-separated
            if isinstance(entity_ids, str):
                if entity_ids.startswith("["):
                    # JSON array string
                    parsed_ids = parse_string_list_param(entity_ids, "entity_ids")
                    if parsed_ids is None:
                        return {
                            "success": False,
                            "error": "entity_ids is required",
                            "suggestions": ["Provide at least one entity ID"],
                        }
                    entity_id_list = parsed_ids
                elif "," in entity_ids:
                    # Comma-separated string
                    entity_id_list = [e.strip() for e in entity_ids.split(",") if e.strip()]
                else:
                    # Single entity ID
                    entity_id_list = [entity_ids.strip()]
            else:
                entity_id_list = entity_ids

            if not entity_id_list:
                return {
                    "success": False,
                    "error": "entity_ids is required",
                    "suggestions": ["Provide at least one entity ID"],
                }

            # Parse time parameters
            try:
                start_dt = parse_relative_time(start_time, default_hours=24)
            except ValueError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "suggestions": [
                        "Use ISO format: '2025-01-25T00:00:00Z'",
                        "Use relative format: '24h', '7d', '2w', '1m'",
                    ],
                }

            if end_time:
                try:
                    end_dt = parse_relative_time(end_time, default_hours=0)
                except ValueError as e:
                    return {
                        "success": False,
                        "error": str(e),
                        "suggestions": ["Use ISO format: '2025-01-26T00:00:00Z'"],
                    }
            else:
                end_dt = datetime.now(UTC)

            # Apply limit constraints with string coercion for AI tools
            try:
                effective_limit = coerce_int_param(
                    limit,
                    param_name="limit",
                    default=DEFAULT_HISTORY_LIMIT,
                    min_value=1,
                    max_value=MAX_HISTORY_LIMIT,
                )
                if effective_limit is None:
                    effective_limit = DEFAULT_HISTORY_LIMIT
            except ValueError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "suggestions": ["Provide limit as an integer (e.g., 100)"],
                }

            # Connect to WebSocket
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            try:
                # Build WebSocket command for history
                # WebSocket command: history/history_during_period
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
                    return await add_timezone_metadata(
                        client,
                        {
                            "success": False,
                            "error": f"Failed to retrieve history: {error_msg}",
                            "entity_ids": entity_id_list,
                            "suggestions": [
                                "Verify entity IDs exist using ha_search_entities()",
                                "Check that entities are recorded (not excluded from recorder)",
                                "Ensure time range is within recorder retention period (~10 days)",
                            ],
                        },
                    )

                # Process results
                result_data = response.get("result", {})
                entities_history = []

                for entity_id in entity_id_list:
                    entity_states = result_data.get(entity_id, [])

                    # Apply limit per entity
                    limited_states = entity_states[:effective_limit]

                    # Format states for output
                    formatted_states = []
                    for state in limited_states:
                        state_entry = {
                            "state": state.get("s", state.get("state")),
                            "last_changed": state.get("lc", state.get("last_changed")),
                        }
                        if not minimal_response:
                            state_entry["attributes"] = state.get(
                                "a", state.get("attributes", {})
                            )
                        formatted_states.append(state_entry)

                    entities_history.append(
                        {
                            "entity_id": entity_id,
                            "period": {
                                "start": start_dt.isoformat(),
                                "end": end_dt.isoformat(),
                            },
                            "states": formatted_states,
                            "count": len(formatted_states),
                            "total_available": len(entity_states),
                            "truncated": len(entity_states) > effective_limit,
                        }
                    )

                history_data = {
                    "success": True,
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

            finally:
                if ws_client:
                    await ws_client.disconnect()

        except Exception as e:
            logger.error(f"Failed to get history: {e}")
            error_data = {
                "success": False,
                "error": f"Failed to retrieve history: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify entity IDs are correct",
                    "Ensure recorder component is enabled",
                ],
            }
            return await add_timezone_metadata(client, error_data)

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["history"], "title": "Get Statistics"})
    @log_tool_usage
    async def ha_get_statistics(
        entity_ids: Annotated[
            str | list[str],
            Field(
                description="Entity ID(s) to query. Must have state_class attribute. Can be single ID, comma-separated, or JSON array."
            ),
        ],
        start_time: Annotated[
            str | None,
            Field(
                description="Start time: ISO datetime or relative (e.g., '30d', '6m', '12m'). Default: 30d ago",
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
        period: Annotated[
            str,
            Field(
                description="Aggregation period: '5minute', 'hour', 'day', 'week', 'month'. Default: 'day'",
                default="day",
            ),
        ] = "day",
        statistic_types: Annotated[
            str | list[str] | None,
            Field(
                description="Statistics types: 'mean', 'min', 'max', 'sum', 'state', 'change'. Default: all",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Retrieve pre-aggregated long-term statistics for trend analysis.

        Returns aggregated statistical data from Home Assistant's long-term statistics
        table. This data is pre-computed and stored permanently, allowing analysis
        of historical trends beyond the standard ~10 day recorder retention.

        **Data Characteristics:**
        - Pre-aggregated: Hourly/daily/monthly statistics
        - Retention: Permanent - never purged
        - Entities: Only those with state_class (measurement, total, total_increasing)
        - Best for: Long-term trends, energy consumption, period comparisons

        **Parameters:**
        - entity_ids: Entity ID(s) with state_class attribute (required)
        - start_time: Start of period - ISO datetime or relative ('30d', '6m', '12m'). Default: 30d ago
        - end_time: End of period - ISO datetime. Default: now
        - period: Aggregation: '5minute', 'hour', 'day', 'week', 'month'. Default: 'day'
        - statistic_types: Types to include: 'mean', 'min', 'max', 'sum', 'state', 'change'. Default: all

        **Statistic Types Explained:**
        - mean: Average value over the period
        - min: Minimum value during the period
        - max: Maximum value during the period
        - sum: Running total (for total_increasing entities like energy)
        - state: Last known state value
        - change: Change from previous period

        **Use Cases:**
        - "How much electricity did I use this month vs last month?" - Monthly sum
        - "What's my average living room temperature?" - Daily/monthly mean
        - "Show daily energy consumption for the past 2 weeks" - Daily sum
        - "Has my solar production declined year over year?" - Monthly comparison

        **Example:**
        ```python
        # Get daily energy statistics for last 30 days
        ha_get_statistics(entity_ids="sensor.total_energy_kwh")

        # Get monthly temperature averages for 6 months
        ha_get_statistics(
            entity_ids="sensor.living_room_temperature",
            start_time="6m",
            period="month",
            statistic_types=["mean", "min", "max"]
        )

        # Compare multiple sensors
        ha_get_statistics(
            entity_ids=["sensor.solar_production", "sensor.grid_consumption"],
            start_time="12m",
            period="month",
            statistic_types=["sum"]
        )
        ```

        **Note:** Only entities with state_class attribute support statistics.
        Use ha_search_entities() to find entities and check their state_class.

        **Returns:**
        - List of entities with their statistics
        - Each includes: entity_id, period type, statistics array, unit_of_measurement
        """
        try:
            # Parse entity_ids
            if isinstance(entity_ids, str):
                if entity_ids.startswith("["):
                    parsed_ids = parse_string_list_param(entity_ids, "entity_ids")
                    if parsed_ids is None:
                        return {
                            "success": False,
                            "error": "entity_ids is required",
                            "suggestions": [
                                "Provide at least one entity ID with state_class attribute"
                            ],
                        }
                    entity_id_list = parsed_ids
                elif "," in entity_ids:
                    entity_id_list = [e.strip() for e in entity_ids.split(",") if e.strip()]
                else:
                    entity_id_list = [entity_ids.strip()]
            else:
                entity_id_list = entity_ids

            if not entity_id_list:
                return {
                    "success": False,
                    "error": "entity_ids is required",
                    "suggestions": [
                        "Provide at least one entity ID with state_class attribute"
                    ],
                }

            # Parse time parameters (default 30 days for statistics)
            try:
                start_dt = parse_relative_time(start_time, default_hours=30 * 24)
            except ValueError as e:
                return {
                    "success": False,
                    "error": str(e),
                    "suggestions": [
                        "Use ISO format: '2025-01-01T00:00:00Z'",
                        "Use relative format: '30d', '6m', '12m'",
                    ],
                }

            if end_time:
                try:
                    end_dt = parse_relative_time(end_time, default_hours=0)
                except ValueError as e:
                    return {
                        "success": False,
                        "error": str(e),
                        "suggestions": ["Use ISO format: '2025-01-31T23:59:59Z'"],
                    }
            else:
                end_dt = datetime.now(UTC)

            # Validate period
            valid_periods = ["5minute", "hour", "day", "week", "month"]
            if period not in valid_periods:
                return {
                    "success": False,
                    "error": f"Invalid period: {period}",
                    "valid_periods": valid_periods,
                    "suggestions": [f"Use one of: {', '.join(valid_periods)}"],
                }

            # Parse statistic_types
            stat_types_list = None
            if statistic_types:
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
                    stat_types_list = statistic_types

                # Validate statistic types
                valid_types = ["mean", "min", "max", "sum", "state", "change"]
                if stat_types_list is None:
                    stat_types_list = []
                invalid_types = [t for t in stat_types_list if t not in valid_types]
                if invalid_types:
                    return {
                        "success": False,
                        "error": f"Invalid statistic types: {invalid_types}",
                        "valid_types": valid_types,
                        "suggestions": [f"Use one or more of: {', '.join(valid_types)}"],
                    }

            # Connect to WebSocket
            ws_client, error = await get_connected_ws_client(
                client.base_url, client.token
            )
            if error or ws_client is None:
                return error or {
                    "success": False,
                    "error": "Failed to establish WebSocket connection",
                }

            try:
                # Build WebSocket command for statistics
                # WebSocket command: recorder/statistics_during_period
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
                    return await add_timezone_metadata(
                        client,
                        {
                            "success": False,
                            "error": f"Failed to retrieve statistics: {error_msg}",
                            "entity_ids": entity_id_list,
                            "suggestions": [
                                "Verify entities have state_class attribute (measurement, total, total_increasing)",
                                "Use ha_search_entities() to check entity attributes",
                                "Statistics are only available for entities that track numeric values",
                            ],
                        },
                    )

                # Process results
                result_data = response.get("result", {})
                entities_statistics = []

                for entity_id in entity_id_list:
                    entity_stats = result_data.get(entity_id, [])

                    # Format statistics for output
                    formatted_stats = []
                    unit = None

                    for stat in entity_stats:
                        stat_entry: dict[str, Any] = {
                            "start": stat.get("start"),
                        }

                        # Include requested statistic types (or all if not specified)
                        for stat_type in stat_types_list or [
                            "mean",
                            "min",
                            "max",
                            "sum",
                            "state",
                            "change",
                        ]:
                            if stat_type in stat:
                                stat_entry[stat_type] = stat[stat_type]

                        # Capture unit from first stat
                        if unit is None and "unit_of_measurement" in stat:
                            unit = stat["unit_of_measurement"]

                        formatted_stats.append(stat_entry)

                    entities_statistics.append(
                        {
                            "entity_id": entity_id,
                            "period": period,
                            "statistics": formatted_stats,
                            "count": len(formatted_stats),
                            "unit_of_measurement": unit,
                        }
                    )

                # Check if any entities had no statistics
                empty_entities: list[str] = [
                    str(e["entity_id"]) for e in entities_statistics if e["count"] == 0
                ]

                statistics_data: dict[str, Any] = {
                    "success": True,
                    "entities": entities_statistics,
                    "period_type": period,
                    "time_range": {
                        "start": start_dt.isoformat(),
                        "end": end_dt.isoformat(),
                    },
                    "statistic_types": stat_types_list
                    or ["mean", "min", "max", "sum", "state", "change"],
                }

                if empty_entities:
                    statistics_data["warnings"] = [
                        f"No statistics found for: {', '.join(empty_entities)}. "
                        "These entities may not have state_class attribute or may not have recorded data yet."
                    ]

                return await add_timezone_metadata(client, statistics_data)

            finally:
                if ws_client:
                    await ws_client.disconnect()

        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            error_data = {
                "success": False,
                "error": f"Failed to retrieve statistics: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Verify entities have state_class attribute",
                    "Ensure recorder component is enabled with statistics",
                ],
            }
            return await add_timezone_metadata(client, error_data)
