"""Routing tests for ``ha_call_service`` over the ``ha_mcp_tools`` gate.

The legacy service-call path does a REST POST, then a hardcoded
``_SERVICE_TO_STATE`` guess + a WS-subscribe-and-sample verification. When the
component advertises ``call_service`` one in-process frame fires exactly one
``async_call`` and returns the REAL pre->post transition, so the consumer maps the
component's ``new_state`` into ``verified_state`` and the transition records feed the
same result projection. These tests pin: the component-served single call (mapped
into the legacy response shape), and the error-taxonomy fallbacks — capability miss,
``unknown_command`` (invalidate caps + legacy), a command error/timeout, and a
connection-establishment failure — all serving the legacy REST POST unchanged.

D9 (at-most-once, correctness-critical) has its own class: a component result with
``partial=True`` (dispatched, unconfirmed) must NEVER trigger a legacy re-POST, while
a ``None`` (pre-dispatch) DOES fall to exactly one legacy POST.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandNotSent,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_service
from ha_mcp.tools.tools_service import register_service_tools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_CALL = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["call_service"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": [],
    "limits": {},
}


def _state(entity_id: str, state: str, **attrs: Any) -> dict[str, Any]:
    """A ``State.as_dict()`` record — the shape the transition new_states carry."""
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": dict(attrs),
        "last_changed": "2026-07-16T00:00:00+00:00",
        "last_updated": "2026-07-16T00:00:00+00:00",
        "context": {"id": "01ABC", "parent_id": None, "user_id": None},
    }


def _confirmed_result(entity_id: str = "light.a") -> dict[str, Any]:
    return {
        "domain": "light",
        "service": "turn_on",
        "dispatched": True,
        "confirmed": True,
        "partial": False,
        "transitions": [
            {
                "entity_id": entity_id,
                "old_state": _state(entity_id, "off"),
                "new_state": _state(entity_id, "on", brightness=255),
                "changed": True,
                "attributes_changed": ["brightness"],
            }
        ],
    }


def _partial_result(entity_id: str = "light.a") -> dict[str, Any]:
    return {
        "domain": "light",
        "service": "turn_on",
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [
            {
                "entity_id": entity_id,
                "old_state": _state(entity_id, "off"),
                "new_state": _state(entity_id, "off"),
                "changed": False,
                "attributes_changed": [],
            }
        ],
    }


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy REST POST + initial-state GET."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.call_service_calls: list[dict[str, Any]] = []
        self.get_state_calls = 0

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        return_response: bool = False,
    ) -> Any:
        self.call_service_calls.append(
            {
                "domain": domain,
                "service": service,
                "service_data": dict(service_data),
                "return_response": return_response,
            }
        )
        entity_id = service_data.get("entity_id")
        return [_state(entity_id or "light.a", "on")]

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        self.get_state_calls += 1
        return {"entity_id": entity_id, "state": "off"}


def _build_call_service(client: Any) -> Any:
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp = MagicMock()
    mcp.add_tool = capture_add_tool
    register_service_tools(mcp, client, device_tools=MagicMock())
    return registered["ha_call_service"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.fixture(autouse=True)
def _no_slow_legacy_verify(monkeypatch: Any) -> Any:
    """Stub the legacy WS-subscribe verification so legacy-fallback tests stay fast.

    The component path never touches this; only the legacy REST fallback calls
    ``wait_for_state_change``, which would otherwise open a real WS / poll for 10s.
    """
    monkeypatch.setattr(
        tools_service,
        "wait_for_state_change",
        AsyncMock(return_value={"entity_id": "light.a", "state": "on"}),
    )


def _call_service_frames(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/call_service"
    ]


@pytest.mark.asyncio
async def test_capability_hit_routes_and_maps_shape() -> None:
    """A state-changing single call is component-served: real transition -> shape."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result=_confirmed_result("light.a"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light",
            service="turn_on",
            entity_id="light.a",
            data={"brightness": 255},
        )

    assert resp["success"] is True
    assert resp["domain"] == "light"
    assert resp["service"] == "turn_on"
    assert resp["entity_id"] == "light.a"
    # The confirmed transition new_state drives verified_state (real, not a guess).
    assert resp["verified_state"] == "on"
    assert "partial" not in resp
    # Compacted result is the transition's new_state record, filtered to the target.
    assert resp["result"] == [
        {"entity_id": "light.a", "state": "on", "attributes": {"brightness": 255}}
    ]
    # The legacy REST POST + initial-state GET were never touched.
    assert client.call_service_calls == []
    assert client.get_state_calls == 0
    # Exactly one call_service frame, with the fully-resolved wire payload (D6).
    frames = _call_service_frames(ws)
    assert len(frames) == 1
    kwargs = frames[0].kwargs
    assert kwargs["domain"] == "light"
    assert kwargs["service"] == "turn_on"
    assert kwargs["service_data"] == {"brightness": 255, "entity_id": "light.a"}
    assert kwargs["entity_ids"] == ["light.a"]
    assert kwargs["wait"] is True
    assert kwargs["return_response"] is False
    # The server hands the component its expected-state confirmation hint
    # (``_SERVICE_TO_STATE.get("turn_on") == "on"``).
    assert kwargs["expected_state"] == "on"


@pytest.mark.asyncio
async def test_unmapped_service_sends_none_hint() -> None:
    """A state-changing service with no primary-state mapping (climate.set_temperature)
    is routed to the component with ``expected_state`` None — the component then keeps
    its any-first-event confirmation for that call."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result=_confirmed_result("climate.a"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        await call_service(
            domain="climate",
            service="set_temperature",
            entity_id="climate.a",
            data={"temperature": 21},
        )

    frames = _call_service_frames(ws)
    assert len(frames) == 1
    assert frames[0].kwargs["expected_state"] is None


@pytest.mark.asyncio
async def test_non_state_changing_call_uses_legacy_not_component() -> None:
    """A non-confirmed call (should_wait False) stays on the legacy REST path.

    The component route is taken ONLY when confirming a single entity: for a
    non-state-changing / fire-and-forget call it would return transitions=[] ->
    result:[], silently dropping HA's changed-states body (I2). So this call must NOT
    route to the component — it returns the legacy REST changed-states content instead.
    """
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="automation",
            service="trigger",
            entity_id="automation.morning",
        )

    assert resp["success"] is True
    assert "verified_state" not in resp
    assert "partial" not in resp
    # Legacy REST POST ran once and returned the changed-states body (not the
    # component's empty []).
    assert len(client.call_service_calls) == 1
    assert resp["result"]
    assert resp["result"][0]["entity_id"] == "automation.morning"
    assert resp["result"][0]["state"] == "on"
    # The component was never routed to — no call_service frame at all.
    assert not _call_service_frames(ws)


@pytest.mark.asyncio
async def test_verbose_uses_legacy_not_component() -> None:
    """M-verbose: a verbose call promises the FULL propagation chain (every downstream
    changed state), which the component route cannot deliver — so it routes to the
    legacy REST POST and never sends a component frame."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a", verbose=True
        )

    assert resp["success"] is True
    # Legacy REST POST ran once; the component was never routed to.
    assert len(client.call_service_calls) == 1
    assert not _call_service_frames(ws)


@pytest.mark.asyncio
async def test_comma_multi_target_uses_legacy_not_component() -> None:
    """A comma-separated entity_id ("light.a,light.b") is a valid multi-target, but the
    component confirms one LITERAL entity_id — it would wait for the nonexistent literal
    and report a false partial. So a comma routes to the legacy REST POST and never
    sends a component frame."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a,light.b"
        )

    assert resp["success"] is True
    # Legacy REST POST ran once; the component was never routed to.
    assert len(client.call_service_calls) == 1
    assert not _call_service_frames(ws)


@pytest.mark.asyncio
async def test_return_response_passed_through() -> None:
    """return_response threads to the component and its service_response is surfaced."""
    result = _confirmed_result("light.a")
    result["service_response"] = {"changed": [{"entity_id": "light.a"}]}
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL, cmd_result=result)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light",
            service="turn_on",
            entity_id="light.a",
            return_response=True,
        )

    assert resp["service_response"] == {"changed": [{"entity_id": "light.a"}]}
    assert _call_service_frames(ws)[0].kwargs["return_response"] is True
    assert client.call_service_calls == []


@pytest.mark.asyncio
async def test_no_capability_uses_legacy_post() -> None:
    """Component without call_service → legacy REST POST, no call_service frame."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1
    assert not _call_service_frames(ws)


@pytest.mark.asyncio
async def test_unknown_command_invalidates_and_falls_back() -> None:
    """unknown_command on the frame → invalidate caps + exactly one legacy POST."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_command_error_falls_back_to_legacy() -> None:
    """A non-unknown command-ERROR response (pre-dispatch / mutate-then-raise
    residual) → exactly one legacy POST."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_exc=HomeAssistantCommandError("boom"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish Exception (after caps cached) → exactly one legacy POST."""
    caps_ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws_establish_failure(
        caps_ws,
        tools_service,
        HomeAssistantConnectionError("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1


@pytest.mark.asyncio
async def test_malformed_envelope_reports_partial_no_redispatch() -> None:
    """A SUCCESS envelope without a truthy 'dispatched' is produced ONLY after the
    single async_call fired, so the write already landed: report it partial and NEVER
    re-POST (I2 — a legacy re-POST would double-apply)."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result={"domain": "light", "service": "turn_on"},  # no 'dispatched' key
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert resp["partial"] is True
    # I2: a malformed SUCCESS envelope is ambiguous-dispatched → ZERO legacy re-POST.
    assert client.call_service_calls == []


@pytest.mark.asyncio
async def test_dispatched_not_true_reports_partial_no_redispatch() -> None:
    """A SUCCESS envelope carrying 'dispatched' present but not True is a received
    post-dispatch envelope we cannot trust to re-fire: ambiguous, ZERO legacy re-POST
    (I2 — presence of the key is not enough; the value must be True)."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result={"domain": "light", "service": "turn_on", "dispatched": None},
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert resp["partial"] is True
    assert client.call_service_calls == []


@pytest.mark.asyncio
async def test_never_sent_falls_to_exactly_one_post() -> None:
    """C1: a HomeAssistantCommandNotSent from send_command (the frame provably never
    left the process — the readiness entry-guard, the one never-sent site) → EXACTLY
    ONE legacy REST POST. The write never happened, so a legacy first fire cannot
    double-apply. A send() failure is NOT this subtype (it is ambiguous — see
    test_post_send_connection_drop_is_ambiguous_no_re_post)."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_exc=HomeAssistantCommandNotSent("WebSocket not authenticated"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1


@pytest.mark.asyncio
async def test_post_send_connection_drop_is_ambiguous_no_re_post() -> None:
    """C1 (the other direction): a PLAIN HomeAssistantConnectionError from send_command
    is a mid-await socket close AFTER the frame was sent (the close handler sets it on
    the pending future) — POST-SEND and ambiguous. It must report partial and NEVER
    re-POST; only HomeAssistantCommandNotSent (a subclass) signals never-sent, so a
    bare connection error is not misclassified as pre-send."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_exc=HomeAssistantConnectionError("socket closed mid-await"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert resp["partial"] is True
    # THE C1 boundary assertion: a post-send drop is ambiguous → ZERO legacy POST.
    assert client.call_service_calls == []


class TestD9AtMostOnce:
    """The single-call at-most-once boundary: None -> legacy; result -> never re-POST."""

    @pytest.mark.asyncio
    async def test_partial_result_does_not_re_post(self) -> None:
        """A component result with partial=True (dispatched, unconfirmed) reports
        partial and NEVER re-POSTs to the legacy REST path (double-fire guard)."""
        ws = make_ws(
            "ha_mcp_tools/call_service",
            info_result=_CAPS_CALL,
            cmd_result=_partial_result("light.a"),
        )
        client = RoutingClient()
        call_service = _build_call_service(client)

        with patch_ws(ws, tools_service):
            resp = await call_service(
                domain="light", service="turn_on", entity_id="light.a"
            )

        # Dispatched-but-unconfirmed → partial success, no verified_state.
        assert resp["success"] is True
        assert resp["partial"] is True
        assert "verified_state" not in resp
        # THE D9 assertion: the component dispatched, so the legacy REST POST is
        # NEVER fired — zero legacy calls despite the confirmation timeout.
        assert client.call_service_calls == []

    @pytest.mark.asyncio
    async def test_pre_dispatch_none_falls_to_exactly_one_post(self) -> None:
        """A None from the helper (component never dispatched — here a capability
        miss) falls to EXACTLY ONE legacy REST POST (a safe first fire)."""
        ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
        client = RoutingClient()
        call_service = _build_call_service(client)

        with patch_ws(ws, tools_service):
            await call_service(domain="light", service="turn_on", entity_id="light.a")

        assert len(client.call_service_calls) == 1
        assert not _call_service_frames(ws)

    @pytest.mark.asyncio
    async def test_post_send_timeout_is_ambiguous_no_re_post(self) -> None:
        """A response-wait timeout on the SENT frame is ambiguous-dispatched: the
        component may still be lawfully mid-write (async_call is unbounded), so the
        consumer reports partial and NEVER re-POSTs — the double-fire guard."""
        ws = make_ws(
            "ha_mcp_tools/call_service",
            info_result=_CAPS_CALL,
            cmd_exc=HomeAssistantCommandTimeout("timeout"),
        )
        client = RoutingClient()
        call_service = _build_call_service(client)

        with patch_ws(ws, tools_service):
            resp = await call_service(
                domain="light", service="turn_on", entity_id="light.a"
            )

        # Dispatched-but-unconfirmed partial success, and — THE D9 assertion — ZERO
        # legacy POST despite the timeout (the frame was sent; re-POST would double-fire).
        assert resp["success"] is True
        assert resp["partial"] is True
        assert client.call_service_calls == []

    @pytest.mark.asyncio
    async def test_establish_failure_falls_to_exactly_one_post(self) -> None:
        """A pre-send establishment failure (get_websocket_client raises) provably
        never dispatched → EXACTLY ONE legacy REST POST (a safe first fire)."""
        caps_ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL)
        client = RoutingClient()
        call_service = _build_call_service(client)

        with patch_ws_establish_failure(
            caps_ws,
            tools_service,
            HomeAssistantConnectionError(
                "Failed to connect to Home Assistant WebSocket"
            ),
        ):
            resp = await call_service(
                domain="light", service="turn_on", entity_id="light.a"
            )

        assert resp["success"] is True
        assert len(client.call_service_calls) == 1
