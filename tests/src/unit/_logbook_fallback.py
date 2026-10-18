"""Shared helpers that pin the logbook REST fallback in unit tests."""

from unittest.mock import AsyncMock

from ha_mcp.client.rest_client import HomeAssistantConnectionError


def disable_logbook_websocket(client: object) -> None:
    """Force the logbook WebSocket transport down so calls use REST."""
    client.send_websocket_message = AsyncMock(  # type: ignore[attr-defined]
        side_effect=HomeAssistantConnectionError("no WS in unit tests")
    )
    client.get_states = AsyncMock(return_value=[])  # type: ignore[attr-defined]
