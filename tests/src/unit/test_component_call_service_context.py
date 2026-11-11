"""Regression: component writes carry the caller's Home Assistant context.

``ha_mcp_tools/call_service`` and ``ha_mcp_tools/bulk_call_service`` mirror core's
``call_service`` WebSocket command. Core builds a ``Context`` from the authenticated
connection before dispatching::

    # homeassistant/components/websocket_api/commands.py
    context = connection.context(msg)
    response = await hass.services.async_call(..., context=context)

The component dispatched without one, so every write issued through the MCP server
landed with ``context.user_id`` unset. Home Assistant reads a context with neither a
user nor a parent as device-originated, so an MCP write was indistinguishable from a
bridge's own change in the logbook and could not be attributed to the account that
made it.

These tests pin the context across the whole write seam: the registered handler
derives it from the connection, the single-call prep forwards it to ``async_call``,
and every bulk op carries it too.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import HomeAssistant, ServiceCall
from pytest_homeassistant.common import MockUser, async_capture_events

from custom_components.ha_mcp_tools.websocket_api.constants import (
    WS_BULK_CALL_SERVICE,
    WS_CALL_SERVICE,
)

from ._component_core import ws_result

_DOMAIN = "ctx_test"
_SERVICE = "ping"


def _register_service(hass: HomeAssistant) -> None:
    async def _ping(call: ServiceCall) -> None:
        return None

    hass.services.async_register(_DOMAIN, _SERVICE, _ping)


class TestSingleCallContext:
    async def test_dispatch_carries_connection_user(
        self, hass: HomeAssistant, component_ws: Any, hass_admin_user: MockUser
    ) -> None:
        _register_service(hass)
        events = async_capture_events(hass, EVENT_CALL_SERVICE)
        await ws_result(
            component_ws,
            WS_CALL_SERVICE,
            domain=_DOMAIN,
            service=_SERVICE,
            entity_ids=[],
            wait=False,
        )
        ours = [e for e in events if e.data["domain"] == _DOMAIN]
        assert len(ours) == 1
        assert ours[0].context.user_id == hass_admin_user.id


class TestBulkCallContext:
    async def test_every_op_carries_connection_user(
        self, hass: HomeAssistant, component_ws: Any, hass_admin_user: MockUser
    ) -> None:
        _register_service(hass)
        events = async_capture_events(hass, EVENT_CALL_SERVICE)
        op = {"domain": _DOMAIN, "service": _SERVICE, "entity_ids": []}
        await ws_result(
            component_ws, WS_BULK_CALL_SERVICE, operations=[op, op], wait=False
        )
        ours = [e for e in events if e.data["domain"] == _DOMAIN]
        assert [e.context.user_id for e in ours] == [hass_admin_user.id] * 2
