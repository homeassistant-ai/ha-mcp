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

from .test_component_ws_phase2_async import (
    _call_hass,
    _FakeBus,
    _FakeCallServices,
    wsapi,
)
from .test_component_ws_search import (
    _REAL_VOL,
    FakeHass,
    _FakeConnection,
    _FakeWSApi,
)

_USER_ID = "7c2f1e9a4b5d4c8e9f0a1b2c3d4e5f60"
_KNOWN = {("input_boolean", "turn_on")}


def _registered(monkeypatch: Any, command: str) -> Any:
    """Register the real commands through the functional WS fake; return one handler."""
    fake = _FakeWSApi()
    monkeypatch.setattr(wsapi, "websocket_api", fake)
    monkeypatch.setattr(wsapi, "vol", _REAL_VOL)
    wsapi.async_register_commands(FakeHass())
    return fake.registered[command]


def _write_hass(services: _FakeCallServices) -> Any:
    return _call_hass({}, services, _FakeBus())


class TestSingleCallContext:
    def test_dispatch_carries_connection_user(self, monkeypatch: Any) -> None:
        handler = _registered(monkeypatch, wsapi.WS_CALL_SERVICE)
        services = _FakeCallServices(known=_KNOWN)
        handler(
            _write_hass(services),
            _FakeConnection(user_id=_USER_ID),
            {
                "id": 1,
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "input_boolean",
                "service": "turn_on",
                "entity_ids": [],
                "wait": False,
            },
        )
        assert len(services.calls) == 1
        context = services.calls[0]["context"]
        assert context is not None
        assert context.user_id == _USER_ID


class TestBulkCallContext:
    def test_every_op_carries_connection_user(self, monkeypatch: Any) -> None:
        handler = _registered(monkeypatch, wsapi.WS_BULK_CALL_SERVICE)
        services = _FakeCallServices(known=_KNOWN)
        handler(
            _write_hass(services),
            _FakeConnection(user_id=_USER_ID),
            {
                "id": 2,
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {"domain": "input_boolean", "service": "turn_on", "entity_ids": []},
                    {"domain": "input_boolean", "service": "turn_on", "entity_ids": []},
                ],
                "wait": False,
            },
        )
        assert len(services.calls) == 2
        assert [c["context"].user_id for c in services.calls] == [_USER_ID, _USER_ID]
