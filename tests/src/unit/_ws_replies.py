"""A WebSocket client fake that answers each message with the next scripted reply."""

from typing import Any
from unittest.mock import AsyncMock, MagicMock


def scripted_ws_client(*replies: dict[str, Any]) -> MagicMock:
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=list(replies))
    return client
