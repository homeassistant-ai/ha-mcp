"""Listener-count enrichment for the ``ha_call_event`` response.

The lookup itself is optional enrichment: when ``GET /api/events`` could not be
read (or failed) the caller passes ``None`` and this returns no fields, so an
unknown count can never be reported as zero listeners. When the mapping is
present, the response carries the exact event-type count, the match-all bucket
count, and — only when both are zero — a warning that nothing was listening.
The count is taken before dispatch, so the warning states registration, not
delivery.
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
    if exact + wildcard == 0:
        fields["warnings"] = [
            f"No event listeners were registered for {event_type!r} when the "
            "listener count was checked; the event was still fired."
        ]
    return fields
