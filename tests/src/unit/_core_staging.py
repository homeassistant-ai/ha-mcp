"""Staging, through Core's own WebSocket API, a command's failure or missing answer.

``served_by`` registers a command again with another handler, so Core serves
it in place of the component's. ``never_answers`` takes the command and sends
nothing back, so the client's command timeout ends the wait;
``revokes_the_token`` revokes the caller's token, so Core closes the
connection mid-command.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api as core_websocket_api
from homeassistant.core import HomeAssistant, callback


@callback
def never_answers(
    hass: HomeAssistant,
    connection: core_websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Takes the command and sends nothing back."""


@callback
def revokes_the_token(
    hass: HomeAssistant,
    connection: core_websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Revokes the caller's token, so Core closes the connection mid-command."""
    assert connection.refresh_token_id is not None
    token = hass.auth.async_get_refresh_token(connection.refresh_token_id)
    assert token is not None
    hass.auth.async_remove_refresh_token(token)


def served_by(
    hass: HomeAssistant,
    command: str,
    handler: core_websocket_api.WebSocketCommandHandler,
) -> None:
    """Core serves ``command`` with ``handler`` in place of the component's."""
    core_websocket_api.async_register_command(
        hass, command, handler, vol.Schema({}, extra=vol.ALLOW_EXTRA)
    )
