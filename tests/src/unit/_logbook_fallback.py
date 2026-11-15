"""Shared helpers and fixtures for the logbook transport unit tests."""

from unittest.mock import AsyncMock

from ha_mcp.client.rest_client import HomeAssistantConnectionError

from ._log_tool_calls import call_kwargs

__all__ = ["call_kwargs", "disable_logbook_websocket"]


def disable_logbook_websocket(client: object) -> None:
    """Force the logbook WebSocket transport down so calls use REST."""
    client.send_websocket_message = AsyncMock(  # type: ignore[attr-defined]
        side_effect=HomeAssistantConnectionError("no WS in unit tests")
    )
    client.get_states = AsyncMock(return_value=[])  # type: ignore[attr-defined]
