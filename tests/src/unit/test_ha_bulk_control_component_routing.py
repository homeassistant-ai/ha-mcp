"""Routing tests for ``ha_bulk_control`` (``bulk_device_control``) over the gate.

The legacy bulk path dispatches each op through ``control_device_smart``, which
POSTs then registers a uuid-keyed operation the caller polls via
``ha_get_operation_status``. When the component advertises ``bulk_call_service`` the
consumer resolves every op's domain/service SERVER-SIDE (D6), sends ONE
register-before-fire batch frame that confirms every op inline, and maps the per-op
transitions back into the legacy bulk response shape — ops come back already
verified, ``operation_ids`` empty and ``follow_up`` None. These tests pin the
component-served batch and the error-taxonomy fallbacks (capability miss,
``unknown_command`` + invalidate, command error/timeout, establish failure, and an
op that cannot be resolved server-side) all serving the unchanged legacy path.

D9 (at-most-once, per batch) has its own class: a component result (even with a
partial op) must NEVER re-dispatch through the legacy path; a ``None`` (nothing
dispatched) DOES fall to the legacy dispatch.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, device_control
from ha_mcp.tools.device_control import DeviceControlTools

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

_CAPS_BULK = {
    "schema_version": 1,
    "component_version": "1.3.0",
    "capabilities": ["bulk_call_service"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.3.0",
    "capabilities": [],
    "limits": {},
}


def _state(entity_id: str, state: str, **attrs: Any) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": dict(attrs),
        "last_changed": "2026-07-16T00:00:00+00:00",
        "last_updated": "2026-07-16T00:00:00+00:00",
        "context": {"id": "01ABC", "parent_id": None, "user_id": None},
    }


def _op_result(
    domain: str,
    service: str,
    entity_id: str,
    *,
    dispatched: bool = True,
    confirmed: bool = True,
    partial: bool = False,
    new_state: str = "on",
    error: str | None = None,
) -> dict[str, Any]:
    op: dict[str, Any] = {
        "domain": domain,
        "service": service,
        "entity_ids": [entity_id],
        "dispatched": dispatched,
        "confirmed": confirmed,
        "partial": partial,
        "transitions": [
            {
                "entity_id": entity_id,
                "old_state": _state(entity_id, "off"),
                "new_state": _state(entity_id, new_state),
                "changed": True,
                "attributes_changed": [],
            }
        ],
    }
    if error is not None:
        op["error"] = error
    return op


def _bulk_result(op_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "operations": op_results,
        "total": len(op_results),
        "dispatched": sum(1 for r in op_results if r["dispatched"]),
        "failed": sum(1 for r in op_results if r.get("error") is not None),
    }


class BulkRoutingClient:
    """Credentialed HA client spy: tallies the legacy per-op REST POST dispatch."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.call_service_calls: list[dict[str, Any]] = []

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any],
        return_response: bool = False,
    ) -> Any:
        self.call_service_calls.append(
            {"domain": domain, "service": service, "service_data": dict(service_data)}
        )
        return [_state(service_data.get("entity_id") or "light.a", "on")]

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        return {"entity_id": entity_id, "state": "off"}


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


@pytest.fixture(autouse=True)
def _isolate_legacy_infra(monkeypatch: Any) -> Any:
    """Neutralize the legacy dispatch's global side effects for fallback tests.

    The component path returns before any of these run; only the legacy fallback
    starts the WS listener and registers operations, which the routing tests do not
    assert on (they assert only that legacy dispatch DID or did NOT happen).
    """
    monkeypatch.setattr(
        device_control, "start_websocket_listener", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(
        device_control, "store_pending_operation", lambda **kwargs: "op-legacy"
    )
    monkeypatch.setattr(device_control, "fail_pending_operation", lambda *a, **k: None)


_TWO_OPS = [
    {"entity_id": "light.a", "action": "on"},
    {"entity_id": "switch.b", "action": "on"},
]


def _bulk_frames(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/bulk_call_service"
    ]


@pytest.mark.asyncio
async def test_capability_hit_routes_and_maps_shape() -> None:
    """A batch is component-served: server-resolved rows out, per-op transitions in."""
    ws = make_ws(
        "ha_mcp_tools/bulk_call_service",
        info_result=_CAPS_BULK,
        cmd_result=_bulk_result(
            [
                _op_result("light", "turn_on", "light.a"),
                _op_result("switch", "turn_on", "switch.b"),
            ]
        ),
    )
    client = BulkRoutingClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(operations=list(_TWO_OPS), parallel=True)

    assert resp["total_operations"] == 2
    assert resp["successful_commands"] == 2
    assert resp["failed_commands"] == 0
    # Ops confirmed inline: no polling handles, no follow_up.
    assert resp["operation_ids"] == []
    assert resp["follow_up"] is None
    first = resp["results"][0]
    assert first["entity_id"] == "light.a"
    assert first["action"] == "on"
    assert first["command_sent"] is True
    assert first["confirmed"] is True
    assert first["final_state"] == "on"
    assert first["verification_method"] == "component_state_change"
    # Legacy per-op dispatch never ran.
    assert client.call_service_calls == []
    # One batch frame, carrying the fully-resolved rows (D6: verb resolution
    # server-side) + one shared wait.
    frames = _bulk_frames(ws)
    assert len(frames) == 1
    kwargs = frames[0].kwargs
    assert kwargs["parallel"] is True
    assert kwargs["wait"] is True
    rows = kwargs["operations"]
    assert rows[0] == {
        "domain": "light",
        "service": "turn_on",
        "service_data": {"entity_id": "light.a"},
        "entity_ids": ["light.a"],
    }
    assert rows[1]["service"] == "turn_on"
    assert rows[1]["entity_ids"] == ["switch.b"]


@pytest.mark.asyncio
async def test_no_capability_uses_legacy_dispatch() -> None:
    """Component without bulk_call_service → legacy per-op dispatch, no batch frame."""
    ws = make_ws("ha_mcp_tools/bulk_call_service", info_result=_CAPS_NONE)
    client = BulkRoutingClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(operations=list(_TWO_OPS), parallel=True)

    assert resp["total_operations"] == 2
    # Legacy dispatch fired one REST POST per op.
    assert len(client.call_service_calls) == 2
    assert not _bulk_frames(ws)


@pytest.mark.asyncio
async def test_unknown_command_invalidates_and_falls_back() -> None:
    """unknown_command on the batch frame → invalidate caps + legacy dispatch."""
    ws = make_ws(
        "ha_mcp_tools/bulk_call_service",
        info_result=_CAPS_BULK,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = BulkRoutingClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(operations=list(_TWO_OPS), parallel=True)

    assert resp["total_operations"] == 2
    assert len(client.call_service_calls) == 2
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_command_error_falls_back_to_legacy() -> None:
    """A non-unknown command error/timeout on the batch → legacy dispatch."""
    ws = make_ws(
        "ha_mcp_tools/bulk_call_service",
        info_result=_CAPS_BULK,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = BulkRoutingClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        await tools.bulk_device_control(operations=list(_TWO_OPS), parallel=True)

    assert len(client.call_service_calls) == 2


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_to_legacy() -> None:
    """A plain establish Exception (after caps cached) → legacy dispatch."""
    caps_ws = make_ws("ha_mcp_tools/bulk_call_service", info_result=_CAPS_BULK)
    client = BulkRoutingClient()
    tools = DeviceControlTools(client)

    with patch_ws_establish_failure(
        caps_ws,
        device_control,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        await tools.bulk_device_control(operations=list(_TWO_OPS), parallel=True)

    assert len(client.call_service_calls) == 2


@pytest.mark.asyncio
async def test_unresolvable_op_aborts_whole_batch_to_legacy() -> None:
    """An op the server cannot resolve (invalid action) sends NO batch frame and
    aborts the WHOLE batch to legacy, which surfaces the per-op error."""
    ws = make_ws("ha_mcp_tools/bulk_call_service", info_result=_CAPS_BULK)
    client = BulkRoutingClient()
    tools = DeviceControlTools(client)

    with patch_ws(ws, device_control):
        resp = await tools.bulk_device_control(
            operations=[{"entity_id": "light.a", "action": "frobnicate"}],
            parallel=True,
        )

    # The invalid action never reached the component (no batch frame), and the
    # legacy path produced the structured per-op failure instead.
    assert not _bulk_frames(ws)
    assert resp["failed_commands"] == 1
    assert resp["successful_commands"] == 0


class TestD9AtMostOnce:
    """The per-batch at-most-once boundary: result -> never re-dispatch; None -> legacy."""

    @pytest.mark.asyncio
    async def test_partial_op_result_does_not_re_dispatch(self) -> None:
        """A component result containing a partial (dispatched, unconfirmed) op is
        used as-is and NEVER re-dispatched through the legacy path."""
        ws = make_ws(
            "ha_mcp_tools/bulk_call_service",
            info_result=_CAPS_BULK,
            cmd_result=_bulk_result(
                [
                    _op_result("light", "turn_on", "light.a"),
                    _op_result(
                        "switch",
                        "turn_on",
                        "switch.b",
                        confirmed=False,
                        partial=True,
                        new_state="off",
                    ),
                ]
            ),
        )
        client = BulkRoutingClient()
        tools = DeviceControlTools(client)

        with patch_ws(ws, device_control):
            resp = await tools.bulk_device_control(
                operations=list(_TWO_OPS), parallel=True
            )

        # Both ops dispatched (confirmed + partial) → both counted successful.
        assert resp["successful_commands"] == 2
        assert resp["results"][1]["partial"] is True
        # THE D9 assertion: the component dispatched, so NOT ONE legacy re-dispatch
        # fires despite the partial op.
        assert client.call_service_calls == []

    @pytest.mark.asyncio
    async def test_failed_op_result_not_re_dispatched(self) -> None:
        """A per-op dispatch failure in the batch result is reported (NOT
        re-dispatched via legacy): the batch already fired."""
        ws = make_ws(
            "ha_mcp_tools/bulk_call_service",
            info_result=_CAPS_BULK,
            cmd_result=_bulk_result(
                [
                    _op_result("light", "turn_on", "light.a"),
                    _op_result(
                        "switch",
                        "turn_on",
                        "switch.b",
                        dispatched=False,
                        confirmed=False,
                        error="HomeAssistantError: boom",
                    ),
                ]
            ),
        )
        client = BulkRoutingClient()
        tools = DeviceControlTools(client)

        with patch_ws(ws, device_control):
            resp = await tools.bulk_device_control(
                operations=list(_TWO_OPS), parallel=True
            )

        assert resp["successful_commands"] == 1
        assert resp["failed_commands"] == 1
        # No legacy re-dispatch of any op — the batch is authoritative (D9).
        assert client.call_service_calls == []

    @pytest.mark.asyncio
    async def test_pre_dispatch_none_falls_to_legacy(self) -> None:
        """A None (capability miss — nothing dispatched) falls to legacy dispatch."""
        ws = make_ws("ha_mcp_tools/bulk_call_service", info_result=_CAPS_NONE)
        client = BulkRoutingClient()
        tools = DeviceControlTools(client)

        with patch_ws(ws, device_control):
            await tools.bulk_device_control(operations=list(_TWO_OPS), parallel=True)

        assert len(client.call_service_calls) == 2
        assert not _bulk_frames(ws)
