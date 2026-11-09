"""The read behind :meth:`HomeAssistantClient.get_config_entry`.

Core's WebSocket ``config_entries/get_single`` answers ``{"config_entry":
fragment}`` or ``not_found``. Core has no REST read for one entry, so its REST
list is the fallback when no WebSocket answer comes back, or when the token's
user is not an admin (``get_single`` is admin-only, the REST list is not).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .rest_client import HomeAssistantAPIError, HomeAssistantConnectionError

if TYPE_CHECKING:
    from .rest_client import HomeAssistantClient

logger = logging.getLogger(__name__)


async def read_config_entry(
    client: HomeAssistantClient, entry_id: str
) -> dict[str, Any]:
    reply: dict[str, Any] | None = None
    try:
        reply = await client.send_websocket_message(
            {"type": "config_entries/get_single", "entry_id": entry_id},
            expected_error_codes=frozenset({"not_found", "unauthorized"}),
        )
    except HomeAssistantConnectionError as e:
        logger.debug(f"config_entries/get_single unanswered, using REST: {e}")
    found: dict[str, Any] | None = None
    if reply is None or reply.get("error_code") == "unauthorized":
        entries = await client.list_config_entries()
        found = next((e for e in entries if e.get("entry_id") == entry_id), None)
    elif reply.get("success"):
        result = reply.get("result")
        found = result.get("config_entry") if isinstance(result, dict) else None
        if not isinstance(found, dict):
            # Core answered, so this is not a missing entry: no 404, no REST.
            raise HomeAssistantAPIError(
                f"config_entries/get_single answered without a config entry: {result!r}",
                response_data=reply,
            )
    elif reply.get("error_code") != "not_found":
        raise HomeAssistantAPIError(
            f"config_entries/get_single failed: {reply.get('error')}",
            response_data=reply,
        )
    if found is None:
        raise HomeAssistantAPIError(
            f"Config entry not found: {entry_id}",
            status_code=404,
        )
    return found
