"""Listener-count enrichment for the ``ha_call_event`` response.

The lookup itself is optional enrichment: when ``GET /api/events`` failed the
caller passes ``None`` and this returns no fields, so a failed lookup can never
report zero listeners. When the mapping is present, the response carries the
exact event-type count, the match-all bucket count, and — only when both are
zero — a warning that no subscriber received the event.
"""

from typing import Any

# Home Assistant's match-all listener bucket in GET /api/events.
_MATCH_ALL_EVENT_KEY = "*"


def event_listener_fields(
    listener_counts: dict[str, int] | None,
    event_type: str,
) -> dict[str, Any]:
    """Build the listener-count fields for a fired event's response."""
    if listener_counts is None:
        return {}
    exact = listener_counts.get(event_type, 0)
    wildcard = listener_counts.get(_MATCH_ALL_EVENT_KEY, 0)
    fields: dict[str, Any] = {
        "listener_count": exact,
        "wildcard_listener_count": wildcard,
    }
    if exact + wildcard == 0:
        fields["warnings"] = [
            f"No event listeners are registered for {event_type!r}; "
            "the event was fired but no subscriber received it."
        ]
    return fields
