"""Routing tests for ``ha_get_state`` over the ``ha_mcp_tools`` component gate.

A bulk ``ha_get_state`` fans out to one REST GET per id today. When the component
advertises the ``states`` capability, both single- and bulk-mode calls resolve
every id from one ``ha_mcp_tools/states`` frame (``State.as_dict()`` per hit) and
skip the per-id REST fetch entirely. These tests pin that shared fast path, the
``MAX_ENTITIES`` cap (enforced regardless of backend), the missing-id error
contract (ENTITY_NOT_FOUND with the response-level ``ha_search()`` suggestion in
bulk; a raised ENTITY_NOT_FOUND in single mode), and the error-taxonomy
fallbacks (silent legacy fallback on ``unknown_command`` and other command
errors; legacy when the component has no ``states`` capability).

The WS client is an ``AsyncMock`` whose ``send_command`` dispatches on the
command type; the HA client is a spy that tallies the legacy ``get_entity_state``
fetches so a test can assert they never ran on the component path.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, tools_search
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_STATES = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["states"],
    "limits": {},
}


def _as_dict(entity_id: str, state: str, friendly: str) -> dict[str, Any]:
    """A REST-shaped ``State.as_dict()`` body (what the component returns per hit)."""
    return {
        "entity_id": entity_id,
        "state": state,
        "attributes": {"friendly_name": friendly},
        "last_changed": "2026-07-16T00:00:00+00:00",
        "last_updated": "2026-07-16T00:00:00+00:00",
        "context": {"id": "01ABC", "parent_id": None, "user_id": None},
    }


_LEGACY_STATES = {
    "light.a": _as_dict("light.a", "on", "A"),
    "sensor.b": _as_dict("sensor.b", "21", "B"),
}


def _component_states_result(
    found: dict[str, dict[str, Any]], missing: list[str]
) -> dict[str, Any]:
    return {"states": dict(found), "missing": list(missing)}


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy ``get_entity_state`` fetch."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_state_calls = 0

    async def get_config(self) -> dict[str, Any]:
        return {"time_zone": "UTC"}

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        self.get_state_calls += 1
        if entity_id in _LEGACY_STATES:
            return dict(_LEGACY_STATES[entity_id])
        raise HomeAssistantAPIError(
            f"API error: 404 - Entity {entity_id} not found", status_code=404
        )


def _build_get_state(client: Any) -> Any:
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
    register_search_tools(mcp, client, smart_tools=SmartSearchTools(client=client))
    return registered["ha_get_state"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _states_calls(ws: AsyncMock) -> list[Any]:
    return [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/states"
    ]


@pytest.mark.asyncio
async def test_bulk_served_by_component_with_missing_contract() -> None:
    """Bulk call: hits from the component, a missing id lands in errors with the
    response-level ha_search suggestion; the legacy REST fetch never runs."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_result=_CAPS_STATES,
        cmd_result=_component_states_result(
            {k: _LEGACY_STATES[k] for k in ("light.a", "sensor.b")},
            ["light.ghost"],
        ),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)

    with patch_ws(ws, tools_search):
        resp = await get_state(["light.a", "sensor.b", "light.ghost"])

    data = resp["data"]
    assert data["success"] is True
    assert set(data["states"]) == {"light.a", "sensor.b"}
    assert data["states"]["light.a"]["state"] == "on"
    assert data["error_count"] == 1
    assert data["errors"][0]["entity_id"] == "light.ghost"
    assert data["errors"][0]["error"]["code"] == "ENTITY_NOT_FOUND"
    assert any("ha_search()" in s for s in data["suggestions"])
    assert data["partial"] is True
    # One component frame, one cached info probe, zero legacy REST GETs.
    assert len(_states_calls(ws)) == 1
    assert client.get_state_calls == 0
    assert _states_calls(ws)[0].kwargs["entity_ids"] == [
        "light.a",
        "sensor.b",
        "light.ghost",
    ]


@pytest.mark.asyncio
async def test_single_entity_served_by_component() -> None:
    """A single string entity_id routes through the same component read."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_result=_CAPS_STATES,
        cmd_result=_component_states_result({"light.a": _LEGACY_STATES["light.a"]}, []),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)

    with patch_ws(ws, tools_search):
        resp = await get_state("light.a")

    assert resp["data"]["entity_id"] == "light.a"
    assert resp["data"]["state"] == "on"
    assert len(_states_calls(ws)) == 1
    assert client.get_state_calls == 0


@pytest.mark.asyncio
async def test_single_missing_via_component_raises_not_found() -> None:
    """A component-reported missing id raises the same ENTITY_NOT_FOUND (with its
    ha_search suggestion) the legacy single-entity 404 raises."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_result=_CAPS_STATES,
        cmd_result=_component_states_result({}, ["light.ghost"]),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)

    with patch_ws(ws, tools_search), pytest.raises(ToolError) as excinfo:
        await get_state("light.ghost")

    msg = str(excinfo.value)
    assert "ENTITY_NOT_FOUND" in msg
    assert "ha_search" in msg
    # The component authoritatively reported it missing — no legacy REST GET.
    assert client.get_state_calls == 0


@pytest.mark.asyncio
async def test_max_entities_enforced_regardless_of_backend() -> None:
    """Over-limit bulk call is rejected before any backend is touched."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_result=_CAPS_STATES,
        cmd_result=_component_states_result({}, []),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)
    too_many = [f"light.l{i}" for i in range(101)]

    with patch_ws(ws, tools_search), pytest.raises(ToolError) as excinfo:
        await get_state(too_many)

    assert "exceeds maximum of 100" in str(excinfo.value)
    # Neither the component nor the legacy path is consulted.
    assert not _states_calls(ws)
    assert client.get_state_calls == 0


@pytest.mark.asyncio
async def test_capsless_component_uses_legacy_per_id() -> None:
    """Old component (info unknown_command) → legacy per-id REST fetch."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_exc=HomeAssistantCommandError("no info", "unknown_command"),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)

    with patch_ws(ws, tools_search):
        resp = await get_state(["light.a", "sensor.b"])

    assert set(resp["data"]["states"]) == {"light.a", "sensor.b"}
    assert not _states_calls(ws)
    assert client.get_state_calls == 2


@pytest.mark.asyncio
async def test_unknown_command_falls_back_to_legacy_silently() -> None:
    """unknown_command on the states call → invalidate caps + silent legacy fetch."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_result=_CAPS_STATES,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)

    with patch_ws(ws, tools_search):
        resp = await get_state(["light.a"])

    assert resp["data"]["states"]["light.a"]["state"] == "on"
    assert client.get_state_calls == 1
    assert "warnings" not in resp["data"]


@pytest.mark.asyncio
async def test_command_error_falls_back_to_legacy_silently() -> None:
    """A non-unknown command error / timeout → silent legacy fetch (no warning)."""
    ws = make_ws(
        "ha_mcp_tools/states",
        info_result=_CAPS_STATES,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient()
    get_state = _build_get_state(client)

    with patch_ws(ws, tools_search):
        resp = await get_state(["light.a", "sensor.b"])

    assert set(resp["data"]["states"]) == {"light.a", "sensor.b"}
    assert client.get_state_calls == 2
    assert "warnings" not in resp["data"]
