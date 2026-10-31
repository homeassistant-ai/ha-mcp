"""Event-bus listener count lookup for the Home Assistant REST client.

``HomeAssistantClient`` is well over the module-size ratchet line, so this read
lives here as a free function taking the client rather than growing that
module. It reads Core's ``GET /api/events`` and returns
``{event_type: listener_count}``.
"""

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _EventEndpoint(Protocol):
    """The slice of ``HomeAssistantClient`` this lookup needs."""

    async def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        """Issue one REST request and return its decoded body."""


async def get_event_listeners(client: _EventEndpoint) -> dict[str, int] | None:
    """Return registered listener counts keyed by event type.

    Home Assistant serves ``GET /api/events`` as a list of
    ``{"event": str, "listener_count": int}`` rows. An empty mapping means the
    bus reported no listeners; ``None`` means the reply could not be read as a
    listener list, which callers must not report as "no listeners" because the
    counts are optional enrichment for the operation that follows.
    """
    logger.debug("Fetching event listeners")
    result = await client._request("GET", "/events")
    if not isinstance(result, list):
        logger.debug("Unexpected event listener reply shape: %s", type(result).__name__)
        return None
    counts: dict[str, int] = {}
    for row in result:
        if not isinstance(row, dict):
            continue
        event = row.get("event")
        count = row.get("listener_count")
        # bool is an int subclass and a negative count cannot exist; neither is
        # a listener count, so neither may reach the response.
        if isinstance(event, str) and type(count) is int and count >= 0:
            counts[event] = count
    return counts
