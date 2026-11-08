"""Small parsing helpers for Home Assistant trace responses."""

from typing import Any


def _trace_start(trace: dict[str, Any]) -> str:
    """Return Core's start timestamp, or its finish, as a sortable string."""
    timestamp = trace.get("timestamp")
    if isinstance(timestamp, dict):
        start = timestamp.get("start")
        if isinstance(start, str):
            return start
        finish = timestamp.get("finish")
        if isinstance(finish, str):
            return finish
        return ""
    return timestamp if isinstance(timestamp, str) else ""


def _requested_sections(sections: str | None) -> set[str]:
    """Return the lower-cased section names requested by the caller."""
    requested: set[str] = set()
    if not sections:
        return requested
    for section in sections.split(","):
        normalized = section.strip().lower()
        if normalized:
            requested.add(normalized)
    return requested
