"""Cross-seam contract test for the component ``call_service`` write capability.

Like ``test_component_readapi_contract.py``, this wires the REAL component write
functions (``_call_service_prep`` -> ``_do_call_service``, driven against a
FakeHass-backed ``hass.services`` / ``hass.bus``) underneath the mocked WS transport
and then invokes the REAL server ``ha_call_service`` consumer — so a shape drift on
either side of the write seam fails here rather than shipping a component-served
transition the consumer mis-maps. The component and consumer suites each verify
their own side against the design; this file verifies them against each other.

Two seams are pinned: a confirmed single call (the mapped consumer response matches
the legacy response shape — same keys, real ``verified_state``, projected result),
and the D1 domain block (surfaces as a structured error through the consumer, and —
directly — as a refusal out of the real component prep before any dispatch).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools import component_api, tools_service
from ha_mcp.tools.tools_service import ServiceTools, register_service_tools

from ._component_routing_helpers import patch_ws

# Importing the phase-2 async component suite installs the homeassistant.* stubs,
# pins ServiceNotFound / EVENT_STATE_CHANGED onto them, and exposes the fake
# services / bus / hass builder the real component prep runs against.
from .test_component_ws_phase2_async import (
    _call_hass,
    _FakeBus,
    _FakeCallServices,
    wsapi,
)
from .test_component_ws_search import FakeState


def _real_call_service_ws(hass: Any) -> AsyncMock:
    """A WS mock whose ``call_service`` frame is served by the REAL component prep.

    ``info`` returns the real ``_do_info()`` (so the caps probe sees ``call_service``
    advertised), and the ``call_service`` command runs the REAL ``_call_service_prep``
    (the D1 guard, the register-before-fire listener, the single ``async_call``, the
    bounded wait) against ``hass`` and formats it through the REAL
    ``_do_call_service`` — the seam under test is everything between that envelope and
    the consumer's mapped response.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        if command_type == wsapi.WS_CALL_SERVICE:
            msg = {"type": wsapi.WS_CALL_SERVICE, **kwargs}
            extra = await wsapi._call_service_prep(hass, msg)
            return {
                "success": True,
                "result": wsapi._do_call_service(hass, msg, **extra),
            }
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


class ContractClient:
    """Credentialed HA client whose legacy REST POST must NEVER fire when routed."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"

    async def call_service(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy REST POST must not run when component-served")

    async def get_entity_state(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy initial-state GET must not run when routed")


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


@pytest.mark.asyncio
async def test_confirmed_single_call_maps_to_legacy_shape() -> None:
    """A confirmed single call through the REAL component prep + REAL consumer maps
    into ha_call_service's response shape with a real verified_state."""
    old = FakeState("light.a", state="off", brightness=100)
    new = FakeState("light.a", state="on", brightness=255)
    bus = _FakeBus()
    services = _FakeCallServices(
        known={("light", "turn_on")},
        on_call=lambda: bus.fire("light.a", new),
    )
    hass = _call_hass([old], services, bus)
    ws = _real_call_service_ws(hass)
    client = ContractClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service):
        resp = await call_service(
            domain="light",
            service="turn_on",
            entity_id="light.a",
            data={"brightness": 255},
        )

    # The consumer response carries the legacy service-call keys...
    assert set(resp) >= {
        "success",
        "domain",
        "service",
        "entity_id",
        "parameters",
        "result",
        "message",
        "verified_state",
    }
    assert resp["success"] is True
    assert resp["domain"] == "light"
    assert resp["service"] == "turn_on"
    assert resp["entity_id"] == "light.a"
    assert resp["message"] == "Successfully executed light.turn_on"
    # ...and the REAL transition drives verified_state + the projected result.
    assert resp["verified_state"] == "on"
    assert "partial" not in resp
    (record,) = resp["result"]
    assert record["entity_id"] == "light.a"
    assert record["state"] == "on"
    assert record["attributes"]["brightness"] == 255
    # Exactly one blocking dispatch happened on the component side.
    assert services.call_count == 1
    assert services.calls[0]["service_data"] == {
        "brightness": 255,
        "entity_id": "light.a",
    }


@pytest.mark.asyncio
async def test_d1_domain_block_surfaces_as_structured_error() -> None:
    """The reserved ha_mcp_tools domain is refused as a structured error through the
    consumer (the server-side guard, defense-in-depth to the component's own D1
    block) before any component frame is sent."""
    bus = _FakeBus()
    services = _FakeCallServices(known={("ha_mcp_tools", "get_caller_token")})
    hass = _call_hass([], services, bus)
    ws = _real_call_service_ws(hass)
    client = ContractClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="ha_mcp_tools", service="get_caller_token")

    assert "ha_mcp_tools" in str(exc.value)
    # No dispatch and no component frame — the block fired before either.
    assert services.call_count == 0
    assert not any(
        c.args[0] == wsapi.WS_CALL_SERVICE for c in ws.send_command.call_args_list
    )


@pytest.mark.asyncio
async def test_nonexistent_entity_fails_fast_without_waiting() -> None:
    """A target absent from the state machine reports ENTITY_NOT_FOUND immediately
    through the REAL component prep + REAL consumer, with NO confirmation wait.

    The regression this pins: an invented entity ID (a local LLM hallucinating
    ``light.cuisine`` instead of a real kitchen light) used to fall through to the
    10s confirmation wait — which could never resolve, since a nonexistent entity
    never emits a state_changed event — leaving the caller's own timeout to expire
    mid-wait. The dispatch still fires (HA silently no-ops it; matching the
    pre-existing behavior for an unmatched service target), but no listener is
    ever registered for it, so the fast-fail is structural, not merely fast in
    this test's fake clock.
    """
    bus = _FakeBus()
    services = _FakeCallServices(known={("light", "turn_on")})
    # No FakeState for "light.cuisine" — hass.states.get() returns None for it.
    hass = _call_hass([], services, bus)
    ws = _real_call_service_ws(hass)
    client = ContractClient()
    call_service = _build_call_service(client)

    with patch_ws(ws, tools_service), pytest.raises(ToolError) as exc:
        await call_service(domain="light", service="turn_on", entity_id="light.cuisine")

    assert "ENTITY_NOT_FOUND" in str(exc.value)
    assert "light.cuisine" in str(exc.value)
    # The service was still dispatched (HA silently no-ops an unmatched target) —
    # only the pointless confirmation wait was skipped.
    assert services.call_count == 1
    # No EVENT_STATE_CHANGED listener was ever registered: the wait never started.
    assert bus.listeners == []


@pytest.mark.asyncio
async def test_unavailable_entity_still_confirms_on_reconnect() -> None:
    """An "unavailable" target is NOT excluded from confirmation like a nonexistent
    one: it can legitimately reconnect and transition during the blocking dispatch
    (e.g. a Zigbee light that wakes on the very command being sent), and that real
    transition must be reported as a normal success, never a false ENTITY_UNAVAILABLE.

    Driven directly against the component prep with an explicit short timeout;
    ``on_call`` fires the confirming event mid-dispatch, mirroring a device that
    reconnects in response to the command.
    """
    new = FakeState("light.b", state="on")
    bus = _FakeBus()
    services = _FakeCallServices(
        known={("light", "turn_on")},
        on_call=lambda: bus.fire("light.b", new),
    )
    hass = _call_hass([FakeState("light.b", state="unavailable")], services, bus)

    msg = {
        "type": wsapi.WS_CALL_SERVICE,
        "domain": "light",
        "service": "turn_on",
        "entity_ids": ["light.b"],
        "wait": True,
        "timeout": 5.0,
        "expected_state": "on",
    }
    extra = await wsapi._call_service_prep(hass, msg)
    result = wsapi._do_call_service(hass, msg, **extra)

    assert result["dispatched"] is True
    assert result["confirmed"] is True
    assert result["partial"] is False
    assert result["transitions"][0]["old_state"]["state"] == "unavailable"
    assert result["transitions"][0]["new_state"]["state"] == "on"
    # A listener WAS registered for it (unlike the nonexistent-entity case) —
    # that is exactly what let it catch the reconnect transition.
    assert bus.listeners


@pytest.mark.asyncio
async def test_unavailable_entity_reports_unavailable_after_genuine_lapse() -> None:
    """An "unavailable" target that never reconnects during the wait keeps a
    listener registered (unlike a nonexistent target) and, through the REAL
    consumer's response mapping, reports the distinct ENTITY_UNAVAILABLE once
    confirmation genuinely lapses — not the generic "state change could not be
    verified" wording.

    Drives the REAL component prep directly with a short explicit timeout (the
    full ``ha_call_service`` tool hardcodes 10s for this path, which would make
    a genuine-lapse test like this one actually take 10 real seconds), then
    feeds the resulting envelope through the REAL
    ``ServiceTools._build_component_call_response`` mapping — the same function
    the full consumer calls — so both sides of the seam are still exercised.
    """
    unavailable = FakeState("light.b", state="unavailable")
    bus = _FakeBus()
    services = _FakeCallServices(
        known={("light", "turn_on")}
    )  # no on_call: never fires
    hass = _call_hass([unavailable], services, bus)

    msg = {
        "type": wsapi.WS_CALL_SERVICE,
        "domain": "light",
        "service": "turn_on",
        "entity_ids": ["light.b"],
        "wait": True,
        "timeout": 0.05,
        "expected_state": "on",
    }
    extra = await wsapi._call_service_prep(hass, msg)
    component_result = wsapi._do_call_service(hass, msg, **extra)

    assert component_result["confirmed"] is False
    assert component_result["partial"] is True
    # A listener WAS registered (unlike the nonexistent-entity fast-fail case) —
    # the real component genuinely waited out the confirmation window.
    assert bus.listeners
    assert services.call_count == 1

    tools = ServiceTools(ContractClient(), device_tools=MagicMock())
    with pytest.raises(ToolError) as exc:
        tools._build_component_call_response(
            component_result,
            domain="light",
            service="turn_on",
            entity_id="light.b",
            data=None,
            should_wait=True,
            return_response=False,
            verbose=False,
            fields=None,
            attribute_keys=None,
        )

    assert "ENTITY_UNAVAILABLE" in str(exc.value)
    assert "light.b" in str(exc.value)


@pytest.mark.asyncio
async def test_mixed_idempotent_and_missing_target_does_not_wait_full_timeout() -> None:
    """CodeRabbit-flagged regression: an idempotently-confirmed valid target mixed
    with a nonexistent one must not stall the whole call to the full timeout.

    ``light.a`` is already at its expected hint (immediate-match, no wait needed);
    ``light.missing`` is excluded from confirmation entirely. Driven directly
    against the component prep (multi-entity ``entity_ids`` is not something the
    real single-entity ``ha_call_service``/``ha_bulk_control`` consumers send today,
    but the prep itself must stay correct for any WS caller of this capability) with
    a timeout long enough to fail the test if the bug regresses, but the assertion
    is on WALL-CLOCK TIME, not just the outcome, so a regression to the full wait
    is caught even though it would eventually still report the right shape.
    """
    import time

    already_on = FakeState("light.a", state="on")
    bus = _FakeBus()
    services = _FakeCallServices(known={("light", "turn_on")})
    hass = _call_hass([already_on], services, bus)

    msg = {
        "type": wsapi.WS_CALL_SERVICE,
        "domain": "light",
        "service": "turn_on",
        "entity_ids": ["light.a", "light.missing"],
        "wait": True,
        "timeout": 5.0,
        "expected_state": "on",
    }
    start = time.monotonic()
    extra = await wsapi._call_service_prep(hass, msg)
    elapsed = time.monotonic() - start
    result = wsapi._do_call_service(hass, msg, **extra)

    assert elapsed < 1.0, (
        f"prep took {elapsed:.2f}s — the immediate-matched target should not "
        "have waited out the missing target's exclusion"
    )
    assert result["dispatched"] is True
    # light.a's own transition confirmed via immediate-match; light.missing never
    # existed, so the OP as a whole is not fully confirmed.
    assert result["confirmed"] is False
    assert result["partial"] is True
    transitions_by_id = {t["entity_id"]: t for t in result["transitions"]}
    assert transitions_by_id["light.a"]["old_state"]["state"] == "on"
    assert transitions_by_id["light.missing"]["old_state"] is None
    # Only ONE listener registered — for light.a; light.missing was excluded.
    assert len(bus.listeners) == 1


@pytest.mark.asyncio
async def test_genuine_confirmation_lapse_still_reports_partial() -> None:
    """A REAL, available target whose confirming event never arrives keeps the
    existing bounded partial/timeout contract — the fast-fail path must not
    swallow a genuine (if rare) confirmation lapse on a valid entity.

    Driven directly against the component prep (not the full consumer, which
    hardcodes a 10s wait) with an explicit short timeout so the test stays fast.
    """
    off = FakeState("light.c", state="off")
    bus = _FakeBus()
    services = _FakeCallServices(
        known={("light", "turn_on")}
    )  # no on_call: never fires
    hass = _call_hass([off], services, bus)

    msg = {
        "type": wsapi.WS_CALL_SERVICE,
        "domain": "light",
        "service": "turn_on",
        "entity_ids": ["light.c"],
        "wait": True,
        "timeout": 0.05,
        "expected_state": "on",
    }
    extra = await wsapi._call_service_prep(hass, msg)
    result = wsapi._do_call_service(hass, msg, **extra)

    assert result["dispatched"] is True
    assert result["confirmed"] is False
    assert result["partial"] is True
    assert result["transitions"][0]["old_state"]["state"] == "off"
    # A confirmable target DOES get a listener registered (unlike the fast-fail
    # cases above) — it just never fires in this test.
    assert bus.listeners
    assert services.call_count == 1


@pytest.mark.asyncio
async def test_component_prep_itself_refuses_ha_mcp_tools() -> None:
    """The authoritative, component-side D1 block: driving the REAL prep directly with
    domain=ha_mcp_tools refuses BEFORE any dispatch (the block that holds no matter
    which path reaches the component — not merely the server guard catching it first)."""
    bus = _FakeBus()
    services = _FakeCallServices(known={("ha_mcp_tools", "get_caller_token")})
    hass = _call_hass([], services, bus)

    with pytest.raises(Exception) as exc:
        await wsapi._call_service_prep(
            hass,
            {
                "type": wsapi.WS_CALL_SERVICE,
                "domain": "ha_mcp_tools",
                "service": "get_caller_token",
            },
        )

    assert "not callable" in str(exc.value)
    # async_call was never reached — no dispatch, no listener.
    assert services.call_count == 0
    assert bus.listeners == []
