"""Shared helpers and fixtures for the logbook transport unit tests."""

from typing import Any
from unittest.mock import AsyncMock

from ha_mcp.client.rest_client import HomeAssistantConnectionError


def call_kwargs(**overrides: Any) -> dict[str, Any]:
    """Full keyword set for ``LogTools.get_logs`` with sensible defaults."""
    base: dict[str, Any] = {
        "source": "logbook",
        "limit": None,
        "search": None,
        "hours_back": 1,
        "entity_id": None,
        "device_id": None,
        "context_id": None,
        "end_time": None,
        "offset": 0,
        "compact": True,
        "level": None,
        "slug": None,
    }
    base.update(overrides)
    return base


def disable_logbook_websocket(client: object) -> None:
    """Force the logbook WebSocket transport down so calls use REST."""
    client.send_websocket_message = AsyncMock(  # type: ignore[attr-defined]
        side_effect=HomeAssistantConnectionError("no WS in unit tests")
    )
    client.get_states = AsyncMock(return_value=[])  # type: ignore[attr-defined]
