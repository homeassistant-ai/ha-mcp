"""Present recorder history with readable names, local event times and per-state totals."""

from datetime import UTC, datetime, tzinfo
from typing import Any

from .response_helpers import resolve_local_timezone

_FIELD_NAMES = {
    "s": "state",
    "a": "attributes",
    "lu": "last_updated",
    "lc": "last_changed",
}


def _local_timestamp(value: Any, timezone: tzinfo) -> Any:
    """Format supported Core event times without discarding unfamiliar values."""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            parsed = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
        else:
            return value
        return parsed.astimezone(timezone).isoformat()
    except (ValueError, OverflowError, OSError):
        return value


def _readable_row(row: dict[str, Any], timezone: tzinfo) -> dict[str, Any]:
    """Expand known compact keys once; retain unknown fields and name collisions."""
    result = {}
    for key, value in row.items():
        name = _FIELD_NAMES.get(key, key)
        if name in row:
            result[key] = value
        else:
            result[name] = (
                _local_timestamp(value, timezone) if key in {"lu", "lc"} else value
            )
    # Match Core's fallback; omitted lc does not prove a state change at lu.
    if "lc" not in row and "last_changed" not in row and "lu" in row:
        result["last_changed"] = _local_timestamp(row["lu"], timezone)
    return result


def format_history_response(response: dict[str, Any]) -> dict[str, Any]:
    """Localize only history event times, leaving attributes and query bounds intact."""
    if response["data"].get("source") != "history":
        return response
    metadata = response["metadata"]
    timezone, name = resolve_local_timezone(
        metadata.get("home_assistant_timezone", "UTC")
    )
    for entity in response["data"].get("entities", []):
        if totals := entity.get("summary"):
            for key in ("first_change", "last_change"):
                totals[key] = _local_timestamp(totals[key], timezone)
            continue
        entity["states"] = [_readable_row(row, timezone) for row in entity["states"]]
    metadata.update(
        home_assistant_timezone=name,
        timestamp_format=f"ISO 8601 ({name})",
        note=(
            f"History event times use {name}. When Core omits lc, last_changed "
            "falls back to last_updated; it may not be the actual state-change time "
            "with significant_changes_only or a window-start snapshot. "
            "Attributes, unknown fields and query bounds are unchanged."
            " UTC is used if HA's timezone is unavailable."
        ),
    )
    return response


def summarize_states(
    states: list[dict[str, Any]],
    window_start_ts: float,
    window_end_ts: float,
    limit: int,
) -> dict[str, Any]:
    """Reduce Core's oldest-first compressed rows to periods and seconds per state.

    Each row lasts until the next row's ``lu`` and the last one until the window
    end; rows after the window end must be dropped first. A row repeating the
    previous state (an attribute-only update, which Core may return) extends
    the current period instead of opening one. Core stamps its window-start
    snapshot with the window start, so the first row counts as a change only
    when it is later than the window start.
    """
    totals: dict[str, dict[str, Any]] = {}
    change_times: list[float] = []
    bounds = [row["lu"] for row in states] + [window_end_ts]
    for index, row in enumerate(states):
        state = row["s"]
        entry = totals.setdefault(state, {"periods": 0, "duration_s": 0.0})
        if index == 0 or state != states[index - 1]["s"]:
            entry["periods"] += 1
            if index or row["lu"] > window_start_ts:
                change_times.append(row["lu"])
        entry["duration_s"] += bounds[index + 1] - bounds[index]
    ranked = sorted(
        totals.items(), key=lambda item: item[1]["duration_s"], reverse=True
    )
    return {
        "changes": len(change_times),
        "first_change": change_times[0] if change_times else None,
        "last_change": change_times[-1] if change_times else None,
        "distinct_states": len(totals),
        "states": {
            state: {
                "periods": entry["periods"],
                "duration_s": round(entry["duration_s"], 3),
            }
            for state, entry in ranked[:limit]
        },
    }


def summary_warnings(
    result: dict[str, list[dict[str, Any]]],
    summaries: dict[str, dict[str, Any]],
    window_start_ts: float,
) -> list[str]:
    """Name the entities whose totals cover less than the requested window or list only some of their states."""
    missing: list[str] = []
    late: list[str] = []
    cut: list[str] = []
    for entity_id, summary in summaries.items():
        states = result.get(entity_id)
        if not states:
            missing.append(entity_id)
        elif states[0]["lu"] > window_start_ts:
            late.append(entity_id)
        if left_out := summary["distinct_states"] - len(summary["states"]):
            cut.append(f"{entity_id}: {left_out} of {summary['distinct_states']}")
    warnings = []
    if missing:
        warnings.append(
            f"No recorded states for: {', '.join(missing)} in this window. Check "
            "the entity IDs, the time range and that the recorder includes them; "
            "their summaries count nothing."
        )
    if late:
        warnings.append(
            f"Totals for {', '.join(late)} start at the first recorded state, "
            "after start_time: Core returned no earlier state (entity added "
            "later, older history purged, or core_options "
            "{'include_start_time_state': false})."
        )
    if cut:
        warnings.append(
            "limit left the shortest states out of summary.states, per entity: "
            f"{', '.join(cut)}. A brief state such as unavailable may be among "
            "them; raise limit (max 1000) or shorten the time range to list every "
            "state."
        )
    return warnings
