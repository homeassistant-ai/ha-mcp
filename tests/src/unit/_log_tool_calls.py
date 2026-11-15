"""Keyword arguments for ``LogTools.get_logs`` in the log tool unit tests."""

from typing import Any


def call_kwargs(**overrides: Any) -> dict[str, Any]:
    """Full keyword set for ``LogTools.get_logs`` with sensible defaults."""
    base: dict[str, Any] = {
        "source": "logbook",
        "limit": None,
        "search": None,
        "hours_back": None,
        "entity_id": None,
        "device_id": None,
        "context_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base
