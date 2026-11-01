"""Unit tests for the component-side ``ha_mcp_tools/call_event`` command.

The command fires the event on Core's own bus in-process and returns the
event's context plus the listener counts read in the same synchronous pass, so
the server's ``ha_call_event`` needs no separate ``GET /api/events`` before a
fire. These tests pin what the component owns: the bus call it makes, the
context it forwards, and the counts it reports.

Split out of ``test_component_ws_phase2_async.py``, which is over the
module-size ratchet's limit and cannot grow.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

# Stub the HA modules the component surface imports (the same pattern the
# sibling component test modules install). ``homeassistant.const`` needs
# ``MATCH_ALL``: ``_do_call_event`` imports it function-locally.
sys.modules.setdefault("homeassistant", SimpleNamespace())
for _mod in (
    "homeassistant.components",
    "homeassistant.components.persistent_notification",
    "homeassistant.config",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.config_validation",
    "homeassistant.helpers.storage",
    "homeassistant.loader",
):
    sys.modules.setdefault(_mod, MagicMock())

sys.modules.setdefault(
    "homeassistant.const",
    SimpleNamespace(EVENT_STATE_CHANGED="state_changed", MATCH_ALL="*"),
)

from ._component_ws_api import wsapi  # noqa: E402  (installs stubs, loads surface)


class _CallEventBus:
    def __init__(self):
        self.fired = []
        self.listeners = {}

    def async_fire(self, event_type, event_data, context):
        self.fired.append((event_type, event_data, context))
        # A one-shot listener unregisters while the event is dispatched. The
        # counts describe the audience the event was delivered to, so reading
        # them after the fire would lose this listener.
        self.listeners.clear()

    def async_listeners(self):
        return dict(self.listeners)


class _SelfRemovingListenerBus(_CallEventBus):
    """A bus whose listener unsubscribes as the event dispatches."""

    def async_fire(self, event_type, event_data, context):
        super().async_fire(event_type, event_data, context)
        self.listeners = {}


class _CallEventHass:
    def __init__(self, bus):
        self.bus = bus


class TestCallEventCommand:
    def test_counts_are_read_before_the_fire(self):
        """A listener that removes itself on dispatch still counts as listening."""
        hass = _CallEventHass(_SelfRemovingListenerBus())
        hass.bus.listeners = {"my_event": 2, "*": 1}

        result = wsapi._do_call_event(
            hass,
            {
                "event_type": "my_event",
                "ha_mcp_context": SimpleNamespace(as_dict=lambda: {"id": "ctx"}),
            },
        )

        assert result["listener_count"] == 2
        assert result["wildcard_listener_count"] == 1

    def test_fires_and_returns_context_and_counts(self):
        hass = _CallEventHass(_CallEventBus())
        hass.bus.listeners = {"my_event": 2, "*": 1}
        context = SimpleNamespace(as_dict=lambda: {"id": "ctx-1"})

        result = wsapi._do_call_event(
            hass,
            {
                "event_type": "my_event",
                "event_data": {"key": "value"},
                "ha_mcp_context": context,
            },
        )

        assert result == {
            "event_type": "my_event",
            "context": {"id": "ctx-1"},
            "listener_count": 2,
            "wildcard_listener_count": 1,
        }
        assert hass.bus.fired == [("my_event", {"key": "value"}, context)]

    def test_missing_counts_are_zero(self):
        hass = _CallEventHass(_CallEventBus())

        result = wsapi._do_call_event(
            hass,
            {
                "event_type": "new_event",
                "ha_mcp_context": SimpleNamespace(as_dict=lambda: {"id": "ctx-2"}),
            },
        )

        assert result["listener_count"] == 0
        assert result["wildcard_listener_count"] == 0
