"""A fake Home Assistant client that answers WebSocket messages from a script."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock


def scripted_ws_client(*replies: dict[str, Any]) -> MagicMock:
    """Return a client whose ``send_websocket_message`` returns ``replies`` in order."""
    client = MagicMock()
    client.send_websocket_message = AsyncMock(side_effect=list(replies))
    return client
