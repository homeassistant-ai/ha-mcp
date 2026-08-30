"""Cross-seam contract test for the component ``bulk_call_service`` write capability.

Wires the REAL component batch functions (``_bulk_call_service_prep`` ->
``_do_bulk_call_service``, driven against a FakeHass-backed ``hass.services`` /
``hass.bus``) underneath the mocked WS transport and invokes the REAL
``bulk_device_control`` consumer — so a shape drift on either side of the batch write
seam fails here. Two seams are pinned: a confirmed two-op batch (the mapped consumer
response matches the legacy bulk shape — successful counts, inline-confirmed per-op
results, no operation-id polling handles), and the D1 batch domain block (the real
component prep fail-closes the WHOLE batch on a reserved-domain op before any
dispatch — the authoritative block that holds regardless of which path reaches it).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.tools import component_api, device_control
from ha_mcp.tools.device_control import DeviceControlTools

from ._component_routing_helpers import patch_ws

# Importing the phase-2 async component suite installs the homeassistant.* stubs,
# pins ServiceNotFound / EVENT_STATE_CHANGED onto them, and exposes the fake bulk
# services / bus / hass builder the real component batch prep runs against.
from .test_component_ws_phase2_async import (
    _call_hass,
    _FakeBulkServices,
    _FakeBus,
    wsapi,
)
from .test_component_ws_search import FakeState


def _real_bulk_ws(hass: Any) -> AsyncMock:
    """A WS mock whose ``bulk_call_service`` frame is served by the REAL batch prep.

    ``info`` returns the real ``_do_info()`` (so the caps probe sees
    ``bulk_call_service`` advertised), and the batch command runs the REAL
    ``_bulk_call_service_prep`` (the all-guards-first D1 pass, the one register-before-
    fire sweep, the dispatch fan-out, the one shared bounded wait) against ``hass``
    and formats it through the REAL ``_do_bulk_call_service``.
    """
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": wsapi._do_info()}
        if command_type == wsapi.WS_BULK_CALL_SERVICE:
            msg = {"type": wsapi.WS_BULK_CALL_SERVICE, **kwargs}
            extra = await wsapi._bulk_call_service_prep(hass, msg)
            return {
                "success": True,
                "result": wsapi._do_bulk_call_service(hass, msg, **extra),
            }
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


class ContractClient:
    """Credentialed HA client whose legacy per-op dispatch must NEVER fire when routed."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"

    async def call_service(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "legacy per-op dispatch must not run when component-served"
        )

    async def get_entity_state(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("legacy validate must not run when component-served")


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.mark.asyncio
async def test_confirmed_batch_maps_to_legacy_shape() -> None:
    """A confirmed two-op batch through the REAL component prep + REAL consumer maps
    into the legacy bulk response shape with inline-confirmed per-op results."""
    new_a = FakeState("light.a", state="on")
    new_b = FakeState("switch.b", state="on")
    bus = _FakeBus()
    services = _FakeBulkServices(
        behaviors={
            ("light", "turn_on"): {"on_call": lambda: bus.fire("light.a", new_a)},
            ("switch", "turn_on"): {"on_call": lambda: bus.fire("switch.b", new_b)},
        }
    )
    hass = _call_hass(
        [FakeState("light.a", state="off"), FakeState("switch.b", state="off")],
        services,
        bus,
    )
    ws = _real_bulk_ws(hass)
    client = ContractClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(
            operations=[
                {"entity_id": "light.a", "action": "on"},
                {"entity_id": "switch.b", "action": "on"},
            ],
            parallel=True,
        )

    # Legacy bulk shape, all component-served + confirmed inline.
    assert resp["total_operations"] == 2
    assert resp["successful_commands"] == 2
    assert resp["failed_commands"] == 0
    assert resp["operation_ids"] == []
    assert resp["follow_up"] is None
    light_res, switch_res = resp["results"]
    assert light_res["entity_id"] == "light.a"
    assert light_res["command_sent"] is True
    assert light_res["confirmed"] is True
    assert light_res["final_state"] == "on"
    assert light_res["service_call"] == {
        "domain": "light",
        "service": "turn_on",
        "data": {"entity_id": "light.a"},
    }
    assert switch_res["confirmed"] is True
    # Both ops dispatched exactly once on the component side.
    assert services.call_count == 2


@pytest.mark.asyncio
async def test_nonexistent_entity_in_batch_fails_fast_without_waiting() -> None:
    """A batch with one valid op and one op targeting a nonexistent entity: the
    valid op confirms normally, the invalid one maps to ENTITY_NOT_FOUND, and —
    the regression this pins — only ONE transition listener is ever registered
    (for the valid op), proving the invalid op's target was excluded from the
    shared wait rather than burning it to time out.
    """
    new_a = FakeState("light.a", state="on")
    bus = _FakeBus()
    services = _FakeBulkServices(
        known={("light", "turn_on")},
        behaviors={
            ("light", "turn_on"): {"on_call": lambda: bus.fire("light.a", new_a)}
        },
    )
    # No FakeState for "light.missing" — hass.states.get() returns None for it.
    hass = _call_hass([FakeState("light.a", state="off")], services, bus)
    ws = _real_bulk_ws(hass)
    client = ContractClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(
            operations=[
                {"entity_id": "light.a", "action": "on"},
                {"entity_id": "light.missing", "action": "on"},
            ],
            parallel=True,
        )

    assert resp["total_operations"] == 2
    assert resp["successful_commands"] == 1
    assert resp["failed_commands"] == 1
    valid_res, missing_res = resp["results"]
    assert valid_res["entity_id"] == "light.a"
    assert valid_res["confirmed"] is True
    assert missing_res["entity_id"] == "light.missing"
    assert missing_res["error"]["code"] == "ENTITY_NOT_FOUND"
    # Both ops still dispatched (HA silently no-ops the unmatched target) — only
    # ONE listener was registered: the valid op's. The invalid op's target was
    # excluded from the shared confirmation wait entirely.
    assert services.call_count == 2
    assert len(bus.listeners) == 1


@pytest.mark.asyncio
async def test_unavailable_entity_in_batch_reports_unavailable_after_genuine_lapse() -> (
    None
):
    """A batch op targeting an "unavailable" entity that never reconnects during
    the wait maps to a distinct ENTITY_UNAVAILABLE failure — but, unlike a
    nonexistent target, a listener IS registered for it (it stays eligible to
    confirm; see ``_confirmable_entity_ids``). A short per-op ``timeout_seconds``
    keeps this genuine-lapse case fast without touching production defaults.
    """
    unavailable = FakeState("light.b", state="unavailable")
    bus = _FakeBus()
    services = _FakeBulkServices(
        known={("light", "turn_on")}
    )  # no on_call: never fires
    hass = _call_hass([unavailable], services, bus)
    ws = _real_bulk_ws(hass)
    client = ContractClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(
            operations=[
                {"entity_id": "light.b", "action": "on", "timeout_seconds": 0.05}
            ],
            parallel=True,
        )

    assert resp["failed_commands"] == 1
    (result,) = resp["results"]
    assert result["error"]["code"] == "ENTITY_UNAVAILABLE"
    assert services.call_count == 1
    assert bus.listeners


@pytest.mark.asyncio
async def test_unavailable_entity_in_batch_confirms_on_reconnect() -> None:
    """An "unavailable" batch target that reconnects and transitions during the
    blocking dispatch is reported as a normal successful op, never a false
    ENTITY_UNAVAILABLE — the same reconnect case pinned for the single-call path,
    exercised through the batch consumer.
    """
    new = FakeState("light.b", state="on")
    bus = _FakeBus()
    services = _FakeBulkServices(
        known={("light", "turn_on")},
        behaviors={("light", "turn_on"): {"on_call": lambda: bus.fire("light.b", new)}},
    )
    hass = _call_hass([FakeState("light.b", state="unavailable")], services, bus)
    ws = _real_bulk_ws(hass)
    client = ContractClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(
            operations=[{"entity_id": "light.b", "action": "on"}],
            parallel=True,
        )

    assert resp["failed_commands"] == 0
    assert resp["successful_commands"] == 1
    (result,) = resp["results"]
    assert result["confirmed"] is True
    assert result["final_state"] == "on"
    assert bus.listeners


@pytest.mark.asyncio
async def test_component_bulk_prep_fail_closes_on_reserved_domain() -> None:
    """The authoritative D1 batch block: driving the REAL batch prep with an op whose
    resolved domain is ha_mcp_tools fail-closes the WHOLE batch BEFORE any dispatch —
    even the valid op ahead of it never fires (all-guards-first)."""
    bus = _FakeBus()
    services = _FakeBulkServices(
        known={("light", "turn_on"), ("ha_mcp_tools", "get_caller_token")}
    )
    hass = _call_hass([FakeState("light.a", state="off")], services, bus)

    with pytest.raises(Exception) as exc:
        await wsapi._bulk_call_service_prep(
            hass,
            {
                "type": wsapi.WS_BULK_CALL_SERVICE,
                "operations": [
                    {
                        "domain": "light",
                        "service": "turn_on",
                        "entity_ids": ["light.a"],
                    },
                    {"domain": "ha_mcp_tools", "service": "get_caller_token"},
                ],
            },
        )

    assert "not callable" in str(exc.value)
    # Zero dispatches for the whole batch — not even the valid op ahead of the
    # refused one — and no register-before-fire listener.
    assert services.call_count == 0
    assert bus.listeners == []
