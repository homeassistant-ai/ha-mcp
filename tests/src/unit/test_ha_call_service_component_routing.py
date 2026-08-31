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
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
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


def _not_found_result(entity_id: str = "light.a") -> dict[str, Any]:
    """The component's shape for a target absent from the state machine.

    ``old_state`` is None (the component's ``pre.get(eid)`` read found nothing).
    ``should_confirm`` stays intent-level (True) even though the target was
    excluded from the actual wait, so ``partial`` is True here too — a
    ``validate_first=False``-style caller that doesn't inspect ``old_state``
    still sees the same dispatched-but-unconfirmed shape it always has.
    """
    return {
        "domain": "light",
        "service": "turn_on",
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [
            {
                "entity_id": entity_id,
                "old_state": None,
                "new_state": None,
                "changed": False,
                "attributes_changed": [],
            }
        ],
    }


def _unavailable_result(entity_id: str = "light.a") -> dict[str, Any]:
    """The component's shape for a target whose confirmation genuinely lapsed
    while STILL unavailable (both pre- and post-dispatch reads show it).

    Unlike a nonexistent target, "unavailable" stays IN the confirmation wait
    (it can legitimately reconnect and transition mid-dispatch) — this fixture
    represents the case where it did not, so ``partial`` is True exactly like
    any other genuine confirmation lapse. ``new_state`` is a real "unavailable"
    dict here, not ``None`` — a ``None`` ``new_state`` means the entity was
    REMOVED during the dispatch (a distinct, more specific outcome; see
    ``_removed_mid_dispatch_result``).
    """
    return {
        "domain": "light",
        "service": "turn_on",
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [
            {
                "entity_id": entity_id,
                "old_state": _state(entity_id, "unavailable"),
                "new_state": _state(entity_id, "unavailable"),
                "changed": False,
                "attributes_changed": [],
            }
        ],
    }


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy REST POST + initial-state GET.

    ``get_state_response`` / ``get_state_exception`` let a test override the
    default "always off" initial-state fetch to exercise the legacy
    entity_not_found / entity_unavailable fast-fail path.
    """

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.call_service_calls: list[dict[str, Any]] = []
        self.get_state_calls = 0
        self.get_state_response: dict[str, Any] | None = None
        self.get_state_exception: Exception | None = None

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
        if self.get_state_exception is not None:
            raise self.get_state_exception
        if self.get_state_response is not None:
            return self.get_state_response
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
async def test_comma_multi_target_survives_404_on_states_endpoint() -> None:
    """Regression: /api/states/<id> has no comma syntax and 404s on a literal
    joined "light.a,light.b" even when both individual entities are real — that
    404 must NOT be misread as ENTITY_NOT_FOUND for the (valid) multi-target
    service call. Caught by CI's E2E suite:
    TestCallServiceResultProjection::test_comma_separated_entity_id_filters_to_target_set.
    """
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL)
    client = RoutingClient()
    client.get_state_exception = HomeAssistantAPIError("not found", status_code=404)
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a,light.b"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1


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
async def test_return_response_null_still_emits_the_key() -> None:
    """A null service_response emits the key, matching the legacy REST path.

    The component only sets ``service_response`` for a non-null response, so a
    null one arrives as an absent key. Gating the mapping on ``is not None`` made
    the two paths answer the same call with different shapes — key omitted here,
    ``service_response: null`` on legacy (``_split_return_response_envelope``).
    """
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
            return_response=True,
        )

    assert "service_response" in resp
    assert resp["service_response"] is None


@pytest.mark.asyncio
async def test_component_reports_entity_not_found_immediately() -> None:
    """A component result whose target was never confirmable (old_state=None)
    raises a structured ENTITY_NOT_FOUND instead of the generic partial/timeout
    wording — and never falls back to the legacy REST path."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result=_not_found_result("light.cuisine"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.cuisine")

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "light.cuisine" in str(exc.value)
    assert client.call_service_calls == []
    assert client.get_state_calls == 0


@pytest.mark.asyncio
async def test_component_reports_entity_unavailable_immediately() -> None:
    """A component result whose target's captured pre-state was "unavailable"
    raises a structured ENTITY_UNAVAILABLE, distinct from ENTITY_NOT_FOUND.

    The error carries ``dispatched: True`` in its structured context (kingpanther13
    review): by the time this raises, the confirmation wait has already lapsed,
    which only happens AFTER the service call reached Home Assistant — a caller
    that only inspects structured fields must be able to tell this apart from a
    command that never went out, so it doesn't blindly retry a non-idempotent
    service and double-apply an already-landed write.
    """
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result=_unavailable_result("light.a"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.a")

    assert "ENTITY_UNAVAILABLE" in str(exc.value)
    assert "light.a" in str(exc.value)
    assert '"dispatched": true' in str(exc.value)
    assert client.call_service_calls == []
    assert client.get_state_calls == 0


@pytest.mark.asyncio
async def test_component_skips_magic_broadcast_target() -> None:
    """kingpanther13-flagged, live-verified regression: entity_id="all"
    (Home Assistant's ``ENTITY_MATCH_ALL`` broadcast target) is not a literal
    entity in the state machine, so its captured pre-state is null just like a
    genuinely nonexistent entity — but it must NOT be reported ENTITY_NOT_FOUND,
    since the broadcast dispatch may have (and typically does) land successfully.
    Falls through to the generic partial/timeout wording instead."""
    result = {
        "domain": "light",
        "service": "turn_off",
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [
            {
                "entity_id": "all",
                "old_state": None,
                "new_state": None,
                "changed": False,
                "attributes_changed": [],
            }
        ],
    }
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL, cmd_result=result)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(domain="light", service="turn_off", entity_id="all")

    assert resp["success"] is True
    assert resp["partial"] is True
    assert any(
        "state change could not be verified" in w for w in resp.get("warnings", [])
    )


@pytest.mark.asyncio
async def test_component_reports_not_found_when_removed_mid_dispatch() -> None:
    """kingpanther13-flagged regression: a target whose pre-state was
    "unavailable" but whose post-dispatch ``new_state`` is null was REMOVED
    during the dispatch — a more specific and more accurate outcome than "still
    unavailable" (which would falsely assert it still exists) — reports
    ENTITY_NOT_FOUND, not ENTITY_UNAVAILABLE."""
    result = {
        "domain": "light",
        "service": "turn_on",
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [
            {
                "entity_id": "light.a",
                "old_state": _state("light.a", "unavailable"),
                "new_state": None,
                "changed": False,
                "attributes_changed": [],
            }
        ],
    }
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL, cmd_result=result)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.a")

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "ENTITY_UNAVAILABLE" not in str(exc.value)


@pytest.mark.asyncio
async def test_empty_transitions_after_dispatch_does_not_report_not_found() -> None:
    """CodeRabbit-flagged regression: an unconfirmed component result with an
    EMPTY transitions list (the ``_dispatched_unconfirmed_result`` shape a
    post-dispatch formatting failure produces — the write already landed, the
    entity demonstrably exists) must NOT be misread as ENTITY_NOT_FOUND. A
    missing transition row proves nothing about existence, unlike a present row
    with a null ``old_state``."""
    result = {
        "domain": "light",
        "service": "turn_on",
        "dispatched": True,
        "confirmed": False,
        "partial": True,
        "transitions": [],
    }
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_CALL, cmd_result=result)
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert resp["partial"] is True
    assert any(
        "state change could not be verified" in w for w in resp.get("warnings", [])
    )


@pytest.mark.asyncio
async def test_partial_without_response_warns_that_null_is_not_proof() -> None:
    """A dispatched-unconfirmed call can lose a response the service produced.

    The component only sets ``service_response`` for a non-null response, so an
    absent key on the partial path is ambiguous: the service may have returned
    nothing, or the component may have discarded the response along with the
    confirmation. Emitting a bare ``service_response: null`` would assert the
    first, so the ambiguity is warned about instead.
    """
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result=_partial_result("light.a"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light",
            service="turn_on",
            entity_id="light.a",
            return_response=True,
        )

    assert resp["service_response"] is None
    assert any("does NOT prove" in w for w in resp["warnings"]), (
        f"a partial call with no response must flag the ambiguity: {resp!r}"
    )


@pytest.mark.asyncio
async def test_confirmed_null_response_does_not_warn() -> None:
    """A confirmed call's null response is unambiguous — no warning noise."""
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
            return_response=True,
        )

    assert resp["service_response"] is None
    assert not any("does NOT prove" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_no_return_response_omits_the_key() -> None:
    """Without return_response the component path emits no service_response key."""
    ws = make_ws(
        "ha_mcp_tools/call_service",
        info_result=_CAPS_CALL,
        cmd_result=_confirmed_result("light.a"),
    )
    client = RoutingClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert "service_response" not in resp


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
async def test_legacy_reports_entity_not_found_without_dispatch() -> None:
    """No component: a clean 404 on the initial-state GET raises ENTITY_NOT_FOUND
    immediately, BEFORE the legacy service POST and BEFORE the 10s WS-subscribe
    wait — the regression this pins is the pointless wait for a target that can
    never emit a confirming state change."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    client.get_state_exception = HomeAssistantAPIError("not found", status_code=404)
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.cuisine")

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "light.cuisine" in str(exc.value)
    # Dispatch never happened: the 404 settled the question before the POST.
    assert client.call_service_calls == []
    tools_service.wait_for_state_change.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_dispatches_unavailable_target_and_reports_after_genuine_lapse() -> (
    None
):
    """No component: a fetched "unavailable" state does NOT fast-fail before
    dispatch, unlike a 404 — dispatching is exactly what might reconnect the
    device, so the service call still fires. Only once ``wait_for_state_change``
    genuinely times out AND a live re-check confirms it is STILL unavailable
    does ENTITY_UNAVAILABLE get reported (CodeRabbit-flagged consistency fix,
    mirroring the component path's post-dispatch-state check)."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    client.get_state_response = {"entity_id": "light.a", "state": "unavailable"}
    call_service = _build_call_service(client)
    tools_service.wait_for_state_change.return_value = None  # genuine timeout

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.a")

    assert "ENTITY_UNAVAILABLE" in str(exc.value)
    assert "light.a" in str(exc.value)
    # dispatched: True in the structured context (kingpanther13 review) — the
    # caller must be able to tell this apart from a command that never went
    # out, so it doesn't blindly retry a non-idempotent service.
    assert '"dispatched": true' in str(exc.value)
    # Dispatch DID happen this time — the target may have reconnected because
    # of it, so failing before the POST would deny it that chance.
    assert len(client.call_service_calls) == 1
    tools_service.wait_for_state_change.assert_called_once()
    # Two fetches: the initial pre-dispatch read, plus the post-timeout live
    # re-check that actually decided the "still unavailable" classification.
    assert client.get_state_calls == 2


@pytest.mark.asyncio
async def test_legacy_skips_magic_broadcast_target() -> None:
    """kingpanther13-flagged, live-verified regression: entity_id="all"
    (Home Assistant's ``ENTITY_MATCH_ALL`` broadcast target) is not a literal
    entity, so ``/api/states/all`` 404s even though the broadcast itself is a
    valid, working target — that 404 must NOT fast-fail the call before
    dispatch (pre-PR, the broad ``except`` in the predecessor of
    ``_validate_entity_before_wait`` swallowed it and let the call through;
    this pins that the fix doesn't regress it back to a hard failure)."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    client.get_state_exception = HomeAssistantAPIError("not found", status_code=404)
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(domain="light", service="turn_off", entity_id="all")

    assert resp["success"] is True
    # Dispatch happened — the 404 on the magic target never fast-failed it.
    assert len(client.call_service_calls) == 1


@pytest.mark.asyncio
async def test_legacy_unavailable_target_reconnected_not_reported_unavailable() -> None:
    """A target "unavailable" at dispatch time but found in some OTHER
    (non-unavailable) state by the live re-check after a genuine
    ``wait_for_state_change`` timeout — it reconnected, just not to the exact
    expected hint — is NOT reported ENTITY_UNAVAILABLE; falls through to the
    existing generic partial/timeout warning."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    states = iter([{"state": "unavailable"}, {"state": "off"}])

    async def _get_state(entity_id: str) -> dict[str, Any]:
        client.get_state_calls += 1
        return next(states)

    client.get_entity_state = _get_state  # type: ignore[method-assign]
    call_service = _build_call_service(client)
    tools_service.wait_for_state_change.return_value = None  # genuine timeout

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert any(
        "state change could not be verified" in w for w in resp.get("warnings", [])
    )
    assert len(client.call_service_calls) == 1
    # Two fetches: the initial pre-dispatch read, plus the post-timeout live
    # re-check that found the reconnect and skipped the false ENTITY_UNAVAILABLE.
    assert client.get_state_calls == 2


@pytest.mark.asyncio
async def test_legacy_unavailable_target_removed_reports_not_found_not_unavailable() -> (
    None
):
    """CodeRabbit-flagged regression: a target "unavailable" at dispatch time
    that 404s on the post-timeout live re-check was REMOVED, a more specific
    outcome than "still unavailable" — reports ENTITY_NOT_FOUND, not
    ENTITY_UNAVAILABLE."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    responses: list[Any] = [
        {"state": "unavailable"},
        HomeAssistantAPIError("gone", status_code=404),
    ]

    async def _get_state(entity_id: str) -> dict[str, Any]:
        client.get_state_calls += 1
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client.get_entity_state = _get_state  # type: ignore[method-assign]
    call_service = _build_call_service(client)
    tools_service.wait_for_state_change.return_value = None  # genuine timeout

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.a")

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "ENTITY_UNAVAILABLE" not in str(exc.value)
    assert client.get_state_calls == 2


@pytest.mark.asyncio
async def test_legacy_unavailable_recheck_transient_error_falls_through() -> None:
    """CodeRabbit-flagged regression: a non-404 failure on the post-timeout live
    re-check is inconclusive, not proof of "still unavailable" — falls through
    to the existing generic verification-failed warning instead of a
    confidently-wrong ENTITY_UNAVAILABLE."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    responses: list[Any] = [
        {"state": "unavailable"},
        HomeAssistantConnectionError("network blip"),
    ]

    async def _get_state(entity_id: str) -> dict[str, Any]:
        client.get_state_calls += 1
        result = responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    client.get_entity_state = _get_state  # type: ignore[method-assign]
    call_service = _build_call_service(client)
    tools_service.wait_for_state_change.return_value = None  # genuine timeout

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert any(
        "state change could not be verified" in w for w in resp.get("warnings", [])
    )
    assert not any("unavailable" in w.lower() for w in resp.get("warnings", []))
    assert client.get_state_calls == 2


@pytest.mark.asyncio
async def test_legacy_transient_fetch_error_still_dispatches() -> None:
    """No component: a non-404 fetch failure is NOT conclusive — the call still
    dispatches and waits as before (best-effort degrade, unchanged behavior)."""
    ws = make_ws("ha_mcp_tools/call_service", info_result=_CAPS_NONE)
    client = RoutingClient()
    client.get_state_exception = HomeAssistantConnectionError("network blip")
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light", service="turn_on", entity_id="light.a"
        )

    assert resp["success"] is True
    assert len(client.call_service_calls) == 1


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
