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
