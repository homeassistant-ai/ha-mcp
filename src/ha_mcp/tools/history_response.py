"""Present recorder history with readable names and local event times."""

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
