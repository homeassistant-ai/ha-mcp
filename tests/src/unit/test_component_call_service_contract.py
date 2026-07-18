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
from ha_mcp.tools.tools_service import register_service_tools

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
