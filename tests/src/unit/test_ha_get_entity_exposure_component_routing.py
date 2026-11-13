"""Routing tests for the exposure listing behind ``ha_get_entity``.

Core's ``expose_entity/list`` returns a bare ``{entity_id: {assistant: bool}}``
map with no names/areas, forcing a second ha_search to identify an exposed
entity. When the component advertises ``exposure`` one ``ha_mcp_tools/exposure``
frame returns the same map PLUS an additive ``entity_info`` enrichment
(friendly_name/domain/area/floor/labels). These tests pin: the component-served
list shape (Core's map unchanged, enrichment additive), and the routes to
Core's ``homeassistant/expose_entity/list`` — capability miss and
``unknown_command`` (which also invalidates the cached caps). The warning that
says why the listing lacks ``entity_info`` is pinned in
``test_entity_component_real_core.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
)
from ha_mcp.tools import component_api, entity_exposure

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
)

_CAPS_PRE_CHILD_SEMANTICS = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["exposure"],
    "limits": {},
}
_CAPS_EXPOSURE = {
    **_CAPS_PRE_CHILD_SEMANTICS,
    "capabilities": ["exposure", "device_registry_child_semantics"],
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": [],
    "limits": {},
}

_INFO_A = {
    "domain": "light",
    "area": "Kitchen",
    "floor": "Main",
    "labels": ["Favorites"],
    "friendly_name": "Lamp A",
    "state": "on",
}
_LEGACY_MAP = {"light.a": {"conversation": True}, "light.b": {"cloud.alexa": True}}


class RoutingClient:
    """Credentialed HA client spy: tallies the legacy expose_entity/list fetch."""

    def __init__(self, legacy_map: dict[str, Any]) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self._legacy_map = dict(legacy_map)
        self.legacy_calls = 0

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        if msg.get("type") == "homeassistant/expose_entity/list":
            self.legacy_calls += 1
            return {
                "success": True,
                "result": {"exposed_entities": dict(self._legacy_map)},
            }
        raise AssertionError(f"unexpected ws message {msg.get('type')!r}")


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


async def _listing(
    client: RoutingClient, assistant: str | None = None
) -> dict[str, Any]:
    """The exposure listing ``ha_get_entity`` returns without an entity."""
    exposed, info, _warning = await entity_exposure.read_exposure(client)
    return entity_exposure.list_exposures(exposed, info, assistant)


def _exposure_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/exposure"
    ]


@pytest.mark.asyncio
async def test_list_mode_served_with_entity_info() -> None:
    """List mode: legacy shape plus an entity_info map keyed by entity_id."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_result={
            "exposed_entities": _LEGACY_MAP,
            "entity_info": {"light.a": _INFO_A, "light.b": {"domain": "light"}},
        },
    )
    client = RoutingClient(_LEGACY_MAP)

    with patch_ws(ws, entity_exposure):
        resp = await _listing(client)

    assert resp["exposed_entities"] == _LEGACY_MAP
    assert resp["count"] == 2
    assert set(resp["entity_info"]) == {"light.a", "light.b"}
    assert resp["entity_info"]["light.a"]["area"] == "Kitchen"
    assert client.legacy_calls == 0
    # List mode sends no entity_id.
    assert "entity_id" not in _exposure_calls(ws)[0].kwargs


@pytest.mark.asyncio
async def test_list_mode_entity_info_follows_assistant_filter() -> None:
    """entity_info covers only the ids surviving the assistant filter."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_result={
            "exposed_entities": _LEGACY_MAP,
            "entity_info": {"light.a": _INFO_A, "light.b": {"domain": "light"}},
        },
    )
    client = RoutingClient(_LEGACY_MAP)

    with patch_ws(ws, entity_exposure):
        resp = await _listing(client, assistant="cloud.alexa")

    # Only light.b is exposed to cloud.alexa.
    assert set(resp["exposed_entities"]) == {"light.b"}
    assert set(resp["entity_info"]) == {"light.b"}


@pytest.mark.asyncio
async def test_no_capability_uses_legacy_list() -> None:
    """Component without exposure → legacy expose_entity/list, no entity_info."""
    ws = make_ws("ha_mcp_tools/exposure", info_result=_CAPS_NONE)
    client = RoutingClient(_LEGACY_MAP)

    with patch_ws(ws, entity_exposure):
        resp = await _listing(client)

    assert resp["exposed_entities"] == _LEGACY_MAP
    assert "entity_info" not in resp
    assert client.legacy_calls == 1
    assert not _exposure_calls(ws)


@pytest.mark.asyncio
async def test_pre_child_semantics_exposure_capability_uses_legacy_list() -> None:
    """An old component cannot publish stale device-derived entity placement."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_PRE_CHILD_SEMANTICS,
        cmd_result={
            "exposed_entities": _LEGACY_MAP,
            "entity_info": {"light.a": _INFO_A},
        },
    )
    client = RoutingClient(_LEGACY_MAP)

    with patch_ws(ws, entity_exposure):
        resp = await _listing(client)

    assert resp["exposed_entities"] == _LEGACY_MAP
    assert "entity_info" not in resp
    assert client.legacy_calls == 1
    assert not _exposure_calls(ws)


@pytest.mark.asyncio
async def test_unknown_command_invalidates_and_falls_back() -> None:
    """unknown_command on the exposure frame → invalidate caps + legacy list."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(_LEGACY_MAP)

    with patch_ws(ws, entity_exposure):
        resp = await _listing(client)

    assert resp["exposed_entities"] == _LEGACY_MAP
    assert "entity_info" not in resp
    assert client.legacy_calls == 1
    assert client not in component_api._CAPS_CACHE
