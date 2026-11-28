"""Read the Container log through Core's backlog before requesting its file."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .._vendor.websockets.exceptions import ConnectionClosed
from .error_log_journald import container_page, log_lines
from .exceptions import HomeAssistantAPIError, HomeAssistantError
from .models import ErrorLogPage

if TYPE_CHECKING:
    from .rest_client import HomeAssistantClient

_LOGGER = logging.getLogger(__name__)


async def backlog_page(
    client: HomeAssistantClient, lines: int, offset: int | str
) -> ErrorLogPage | None:
    from .websocket_client import get_websocket_client

    if isinstance(offset, str):
        raise HomeAssistantAPIError(
            "A cursor token cannot be continued without the HA-MCP component "
            "on this install; start a new read without it"
        )
    try:
        ws = await get_websocket_client(
            url=client.base_url, token=client.token, verify_ssl=client.verify_ssl
        )
        sub_id, queue = await ws.subscribe_command("system_log/subscribe_raw")
        try:
            event = (await queue.get()).get("event")
        finally:
            await ws.release_subscription(sub_id)
    except (HomeAssistantError, ConnectionClosed, OSError) as exc:
        _LOGGER.debug("Core raw-log backlog unavailable: %s", exc)
    else:
        if isinstance(event, list):
            backlog = "\n".join(event)
            # One older line proves the page's continuation without the file.
            # Core sends the file's last 100 lines. A shorter nonempty backlog
            # contains the whole file. An empty one may mean logging is disabled.
            if 0 < len(event) < 100 or len(log_lines(backlog)) > lines + offset:
                return container_page(backlog, lines, offset)
    return None
