"""The ``call_event`` write command: fire a custom event in-process."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant


def _do_call_event(hass: HomeAssistant, msg: dict[str, Any]) -> dict[str, Any]:
    """Fire one event and return its context and listener counts.

    The wrapper has already attached ``ha_mcp_context`` from the connection.
    ``async_fire`` is Core's own bus entry point: the event type is not placed
    in a URL, so Core accepts the same names its WebSocket ``fire_event``
    accepts. Listener counts are read in the same synchronous pass, before the
    event can be handled, and ``MATCH_ALL`` is reported separately so callers
    can distinguish exact listeners from recorder-style observers.
    """
    from homeassistant.const import MATCH_ALL

    event_type = msg["event_type"]
    context = msg["ha_mcp_context"]
    # Read the counts BEFORE the fire: a listener that unregisters itself on
    # dispatch (async_listen_once does) is still part of the audience this
    # event is delivered to, so it must not be missing from the count.
    listeners = hass.bus.async_listeners()
    hass.bus.async_fire(event_type, msg.get("event_data"), context=context)
    return {
        "event_type": event_type,
        "context": context.as_dict(),
        "listener_count": listeners.get(event_type, 0),
        "wildcard_listener_count": listeners.get(MATCH_ALL, 0),
    }
