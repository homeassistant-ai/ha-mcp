"""Change Core the moment it receives a command, then let Core answer it.

``on_receipt`` re-registers a command's own handler with its own schema: the
test's act runs first (a concurrent change made through Core, the client's
reads losing their answer, its token revoked), then Core's handler answers the
message as usual. ``at_entity_wait`` does it when a tool starts waiting for an
entity, which subscribes to ``entity_registry_updated`` before it reads the
entity's state.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError


@contextmanager
def on_receipt(
    hass: HomeAssistant,
    command: str,
    act: Callable[[dict[str, Any]], object],
    *,
    when: Callable[[dict[str, Any]], bool] = lambda msg: True,
) -> Iterator[None]:
    """Run ``act`` on each ``command`` message ``when`` accepts, before Core's
    handler answers it."""
    core_handler, schema = hass.data[websocket_api.DOMAIN][command]

    def act_then_answer(
        core: HomeAssistant,
        connection: websocket_api.ActiveConnection,
        msg: dict[str, Any],
    ) -> None:
        if when(msg):
            act(msg)
        core_handler(core, connection, msg)

    websocket_api.async_register_command(hass, command, act_then_answer, schema)
    try:
        yield
    finally:
        websocket_api.async_register_command(hass, command, core_handler, schema)


def at_entity_wait(
    hass: HomeAssistant, act: Callable[[], object]
) -> AbstractContextManager[None]:
    """Run ``act`` when a tool's entity wait subscribes, before it reads the state."""
    return on_receipt(
        hass,
        "subscribe_events",
        lambda msg: act(),
        when=lambda msg: msg.get("event_type") == "entity_registry_updated",
    )


def lose_state_reads(
    client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The client's state reads get no answer from now on."""
    monkeypatch.setattr(
        client,
        "get_entity_state",
        AsyncMock(side_effect=HomeAssistantConnectionError("network down")),
    )


def revoke(hass: HomeAssistant, access_token: str) -> None:
    """Core revokes the client's token: its socket closes, REST answers 401."""
    refresh_token = hass.auth.async_validate_access_token(access_token)
    assert refresh_token is not None
    hass.auth.async_remove_refresh_token(refresh_token)
