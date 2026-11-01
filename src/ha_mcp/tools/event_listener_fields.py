"""Listener-count enrichment for the ``ha_call_event`` response.

The lookup itself is optional enrichment: when ``GET /api/events`` could not be
read (or failed) the caller passes ``None`` and this returns no fields, so an
unknown count can never be reported as zero listeners. When the mapping is
present, the response carries the exact event-type count and the match-all
bucket count; a warning is emitted whenever the exact count is zero. A
match-all listener (the recorder is one) is named in the warning rather than
allowed to hide the zero, because it may only store the event. The count is
taken before dispatch, so the warning states registration, not delivery.
"""

from typing import Any

from ..client.event_listeners import MATCH_ALL_EVENT_KEY


def event_listener_fields(
    listener_counts: dict[str, int] | None,
    event_type: str,
) -> dict[str, Any]:
    """Build the listener-count fields for a fired event's response."""
    if listener_counts is None:
        return {}
    exact = listener_counts.get(event_type, 0)
    wildcard = listener_counts.get(MATCH_ALL_EVENT_KEY, 0)
    fields: dict[str, Any] = {
        "listener_count": exact,
        "wildcard_listener_count": wildcard,
    }
    if exact == 0:
        if wildcard:
            fields["warnings"] = [
                f"No event listeners are registered for {event_type!r}. "
                f"{wildcard} match-all listener(s) are also registered; the "
                "recorder, when enabled, is one and only stores events."
            ]
        else:
            fields["warnings"] = [
                f"No event listeners are registered for {event_type!r}; "
                "the event was still fired."
            ]
    return fields
