"""Event-bus listener count lookup for the Home Assistant REST client.

``HomeAssistantClient`` is well over the module-size ratchet line, so this
read method lives in its own mixin rather than growing that module. It reads
Core's ``GET /api/events`` and returns ``{event_type: listener_count}``.
"""

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _EventEndpoint(Protocol):
    """The slice of ``HomeAssistantClient`` this lookup needs."""

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any: ...


class EventListenersMixin:
    """Adds ``get_event_listeners`` to the REST client."""

    async def get_event_listeners(self: _EventEndpoint) -> dict[str, int]:
        """Return registered listener counts keyed by event type.

        Home Assistant serves ``GET /api/events`` as a list of
        ``{"event": str, "listener_count": int}`` rows. An unexpected reply
        shape yields an empty mapping: callers use this as optional
        enrichment, so a missing count must not fail the operation that
        follows.
        """
        logger.debug("Fetching event listeners")
        result = await self._request("GET", "/events")
        if not isinstance(result, list):
            return {}
        counts: dict[str, int] = {}
        for row in result:
            if (
                isinstance(row, dict)
                and isinstance(row.get("event"), str)
                and isinstance(row.get("listener_count"), int)
            ):
                counts[row["event"]] = row["listener_count"]
        return counts
