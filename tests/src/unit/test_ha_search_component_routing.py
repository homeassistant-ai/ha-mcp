"""Routing tests for ``ha_search`` over the ``ha_mcp_tools`` component gate.

When the component advertises the ``search`` capability, ``ha_search`` serves
the whole query from one ``ha_mcp_tools/search`` WebSocket call and skips the
legacy REST/WS fetch pipeline entirely. These tests pin that fast path and the
error-taxonomy fallbacks (silent on ``unknown_command``; legacy + ``warnings[]``
on any other command error), plus response-shape parity between the two paths.

The WS client is an ``AsyncMock`` whose ``send_command`` dispatches on the
command type. The HA client is a spy that tallies the legacy fetches so a test
can assert they never ran on the component path.
"""

from __future__ import annotations

import contextlib
from collections import Counter
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import component_api, tools_search
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

_STATES = [
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen"},
    },
    {
        "entity_id": "sensor.kitchen_temp",
        "state": "21",
        "attributes": {"friendly_name": "Kitchen Temp"},
    },
]
_ENTITY_REGISTRY = {
    "success": True,
    "result": [
        {"entity_id": "light.kitchen", "entity_category": None},
        {"entity_id": "sensor.kitchen_temp", "entity_category": None},
    ],
}

_CAPS_SEARCH = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search"],
    "limits": {},
}


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy fetch ha_search makes."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_states_calls = 0
        self.ws_types: Counter[str] = Counter()

    async def get_states(self) -> list[dict[str, Any]]:
        self.get_states_calls += 1
        return [dict(s) for s in _STATES]

    async def get_config(self) -> dict[str, Any]:
        return {"time_zone": "UTC"}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/entity_registry/list":
            return _ENTITY_REGISTRY
        if msg_type == "config/device_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/entity_registry/get_entries":
            return {"success": True, "result": {}}
        return {"success": False}

    async def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("bulk config REST must not be hit on the component path")

    async def get_scene_config(self, scene_id: str) -> dict[str, Any]:
        return {"config": {}}

    async def get_script_config(self, script_id: str) -> dict[str, Any]:
        return {"config": {}}


def _make_ws(
    *,
    info_result: dict[str, Any] | None = None,
    info_exc: Exception | None = None,
    search_result: dict[str, Any] | None = None,
    search_exc: Exception | None = None,
) -> AsyncMock:
    ws = AsyncMock()

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            if info_exc is not None:
                raise info_exc
            return {"success": True, "result": info_result}
        if command_type == "ha_mcp_tools/search":
            if search_exc is not None:
                raise search_exc
            return {"success": True, "result": search_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws.send_command = AsyncMock(side_effect=_send)
    return ws


@contextlib.contextmanager
def _patch_ws(ws: AsyncMock) -> Any:
    """Patch both module references to ``get_websocket_client`` to yield ``ws``."""
    factory = AsyncMock(return_value=ws)
    with (
        patch.object(component_api, "get_websocket_client", factory),
        patch.object(tools_search, "get_websocket_client", factory),
    ):
        yield ws


def _build_ha_search(client: Any) -> Any:
    mcp = MagicMock()
    registered: dict[str, Any] = {}

    def capture_add_tool(method: Any) -> None:
        name = (
            method.__fastmcp__.name
            if hasattr(method, "__fastmcp__")
            else method.__name__
        )
        registered[name] = method

    mcp.add_tool = capture_add_tool
    register_search_tools(mcp, client, smart_tools=SmartSearchTools(client=client))
    return registered["ha_search"]


def _setup_visibility_disabled(tmp_path: Any, monkeypatch: Any) -> None:
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


def _entity_search_result() -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": "light.kitchen",
                "friendly_name": "Kitchen",
                "domain": "light",
                "state": "on",
                "score": 100,
                "match_type": "exact",
            }
        ],
        "entity_total_matches": 1,
        "entity_has_more": False,
        "config_total_matches": 0,
        "config_has_more": False,
        "partial": False,
    }


@pytest.mark.asyncio
async def test_component_fast_path_skips_legacy_fetches(tmp_path, monkeypatch) -> None:
    """When the component serves search, none of the legacy fetches are awaited."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = _make_ws(info_result=_CAPS_SEARCH, search_result=_entity_search_result())
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with _patch_ws(ws):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert resp["entities"][0]["entity_id"] == "light.kitchen"
    assert resp["entity_total_matches"] == 1
    # The legacy inventory is untouched: no /api/states, no registry list.
    assert client.get_states_calls == 0
    assert client.ws_types == Counter()
    # Exactly one component search command was issued.
    search_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/search"
    ]
    assert len(search_calls) == 1


@pytest.mark.asyncio
async def test_unknown_command_falls_back_silently(tmp_path, monkeypatch) -> None:
    """unknown_command on the search call → legacy path, no fallback warning."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = _make_ws(
        info_result=_CAPS_SEARCH,
        search_exc=HomeAssistantCommandError("Command failed: nope", "unknown_command"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with _patch_ws(ws):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    # Legacy inventory served the request.
    assert client.get_states_calls == 1
    assert client.ws_types["config/entity_registry/list"] == 1
    # Silent fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_raised_command_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A non-unknown command error → legacy path AND a warnings[] entry."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = _make_ws(
        info_result=_CAPS_SEARCH,
        search_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with _patch_ws(ws):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_caps_probed_once_across_searches(tmp_path, monkeypatch) -> None:
    """The info probe is cached: two searches, one ha_mcp_tools/info call."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = _make_ws(info_result=_CAPS_SEARCH, search_result=_entity_search_result())
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with _patch_ws(ws):
        await ha_search(query="kitchen")
        await ha_search(query="kitchen")

    info_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]
    assert len(info_calls) == 1


@pytest.mark.asyncio
async def test_component_and_legacy_response_shape_parity(
    tmp_path, monkeypatch
) -> None:
    """Same query resolves to the same envelope shape on both serving paths.

    A body-skipped entity query (query + domain_filter) is served (a) by the
    component and (b) by the legacy pipeline over equivalent fixture data; the
    two responses must agree on their key set, pagination axis, and partial
    semantics.
    """
    _setup_visibility_disabled(tmp_path, monkeypatch)

    ws_component = _make_ws(
        info_result=_CAPS_SEARCH, search_result=_entity_search_result()
    )
    client_component = RoutingClient()
    with _patch_ws(ws_component):
        component = await _build_ha_search(client_component)(
            query="kitchen", domain_filter="light"
        )

    # info → unknown_command yields no caps, so this run takes the legacy path.
    ws_legacy = _make_ws(
        info_exc=HomeAssistantCommandError("Command failed: no info", "unknown_command")
    )
    client_legacy = RoutingClient()
    with _patch_ws(ws_legacy):
        legacy = await _build_ha_search(client_legacy)(
            query="kitchen", domain_filter="light"
        )

    assert set(component.keys()) == set(legacy.keys())
    assert component["partial"] == legacy["partial"] is False
    assert component["entity_total_matches"] == legacy["entity_total_matches"] == 1
    assert component["count"] == legacy["count"] == 1
    assert component["has_more"] == legacy["has_more"] is False
    assert component["next_offset"] == legacy["next_offset"]
    # Both paths surface the entity-intent body-skip warning.
    skip = "config-body search skipped"
    assert any(skip in w for w in component["warnings"])
    assert any(skip in w for w in legacy["warnings"])
