"""Routing tests for ``ha_get_entity_exposure`` over the ``ha_mcp_tools`` gate.

The legacy tool returns a bare ``{entity_id: {assistant: bool}}`` map with no
names/areas, forcing a second ha_search to identify an exposed entity. When the
component advertises ``exposure`` one ``ha_mcp_tools/exposure`` frame returns the
byte-identical ``expose_entity/list`` map PLUS an additive ``entity_info``
enrichment (friendly_name/domain/area/floor/labels). These tests pin: the
component-served single + list shapes (legacy keys byte-identical, enrichment
additive), and the error-taxonomy fallbacks — capability miss, ``unknown_command``
(invalidate caps + legacy), and a command error/timeout — all serving the legacy
``homeassistant/expose_entity/list`` result unchanged.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, tools_voice_assistant
from ha_mcp.tools.tools_voice_assistant import register_voice_assistant_tools

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_EXPOSURE = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["exposure"],
    "limits": {},
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


def _build_exposure(client: Any) -> Any:
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
    register_voice_assistant_tools(mcp, client)
    return registered["ha_get_entity_exposure"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _exposure_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/exposure"
    ]


@pytest.mark.asyncio
async def test_single_entity_served_and_enriched() -> None:
    """Single mode: byte-identical legacy keys + additive name/area enrichment."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_result={
            "exposed_entities": {"light.a": {"conversation": True}},
            "entity_info": {"light.a": _INFO_A},
        },
    )
    client = RoutingClient(_LEGACY_MAP)
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure(entity_id="light.a")

    # Legacy keys, byte-identical.
    assert resp["exposed_to"] == {
        "conversation": True,
        "cloud.alexa": False,
        "cloud.google_assistant": False,
    }
    assert resp["is_exposed_anywhere"] is True
    assert resp["has_custom_settings"] is True
    # Additive enrichment merged on top.
    assert resp["friendly_name"] == "Lamp A"
    assert resp["domain"] == "light"
    assert resp["area"] == "Kitchen"
    assert resp["floor"] == "Main"
    assert resp["labels"] == ["Favorites"]
    assert resp["state"] == "on"
    # The legacy WS list was never touched.
    assert client.legacy_calls == 0
    assert len(_exposure_calls(ws)) == 1
    assert _exposure_calls(ws)[0].kwargs["entity_id"] == "light.a"


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
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure()

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
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure(assistant="cloud.alexa")

    # Only light.b is exposed to cloud.alexa.
    assert set(resp["exposed_entities"]) == {"light.b"}
    assert set(resp["entity_info"]) == {"light.b"}


@pytest.mark.asyncio
async def test_no_capability_uses_legacy_list() -> None:
    """Component without exposure → legacy expose_entity/list, no entity_info."""
    ws = make_ws("ha_mcp_tools/exposure", info_result=_CAPS_NONE)
    client = RoutingClient(_LEGACY_MAP)
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure()

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
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure(entity_id="light.a")

    assert resp["exposed_to"]["conversation"] is True
    assert "friendly_name" not in resp
    assert client.legacy_calls == 1
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_command_error_falls_back_to_legacy() -> None:
    """A non-unknown command error/timeout → legacy list, enrichment absent."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient(_LEGACY_MAP)
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure()

    assert resp["exposed_entities"] == _LEGACY_MAP
    assert "entity_info" not in resp
    assert client.legacy_calls == 1


@pytest.mark.asyncio
async def test_single_mode_missing_entity_info_omits_enrichment() -> None:
    """Single mode where the component's ``entity_info`` has no entry for the queried
    id (``None`` info) merges nothing: the byte-identical legacy exposure keys are
    returned and no enrichment key is invented, no crash (issue #1813 T6)."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_result={
            "exposed_entities": {"light.a": {"conversation": True}},
            "entity_info": {},  # no entry for light.a
        },
    )
    client = RoutingClient(_LEGACY_MAP)
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure(entity_id="light.a")

    assert resp["exposed_to"]["conversation"] is True
    for key in ("friendly_name", "domain", "area", "floor", "labels", "state"):
        assert key not in resp
    # The component answered authoritatively; the legacy WS list was never touched.
    assert client.legacy_calls == 0


@pytest.mark.asyncio
async def test_single_mode_stateless_entity_omits_live_state_keys() -> None:
    """A component ``entity_info`` carrying only registry keys (a disabled /
    legacy-only entity with no live state, so the component omits friendly_name /
    state) merges just those keys — the live-state keys stay absent rather than
    being emitted as null (issue #1813 T6)."""
    ws = make_ws(
        "ha_mcp_tools/exposure",
        info_result=_CAPS_EXPOSURE,
        cmd_result={
            "exposed_entities": {"light.a": {"conversation": True}},
            "entity_info": {
                "light.a": {
                    "domain": "light",
                    "area": "Kitchen",
                    "floor": "Main",
                    "labels": [],
                }
            },
        },
    )
    client = RoutingClient(_LEGACY_MAP)
    exposure = _build_exposure(client)

    with patch_ws(ws, tools_voice_assistant):
        resp = await exposure(entity_id="light.a")

    # Registry-derived keys merged...
    assert resp["domain"] == "light"
    assert resp["area"] == "Kitchen"
    assert resp["floor"] == "Main"
    # ...but the live-state keys the component omitted are NOT present.
    assert "friendly_name" not in resp
    assert "state" not in resp
