"""Routing tests for ``ha_search`` under the ``search_visibility`` capability.

An ACTIVE entity-visibility filter used to force the legacy path unconditionally
(a plain ``search`` component applies no filtering, so it would leak hidden
entities). The ``search_visibility`` capability closes that gap: a component
that advertises it accepts the serialized hide config as the ``visibility``
param and excludes hidden entities itself, so a visibility-active install can
still take the fast path. These tests pin the four-way gate:

- filter active + ``search_visibility`` → component WITH the ``visibility`` param
- filter active + only ``search`` → legacy (the pre-capability behaviour)
- filter inactive → component WITHOUT the param (old components keep working)
- component error on the visibility path → legacy fallback (filter still applied)
- unloadable config → fail-closed to legacy (never route unfiltered)
- ``unknown_command`` on the visibility search → invalidate caps + silent legacy
  fallback
- a transport failure on the visibility search (both a
  ``HomeAssistantConnectionError`` off the frame and a plain ``Exception`` from
  ``get_websocket_client()`` failing to establish the socket) → legacy fallback
  with a ``served via legacy path`` warning (the legacy path reads ``/api/states``
  over REST + the swallowing registry bridge, so it does not die identically)

The parity of what the component then DOES with that param lives in
``test_component_search_visibility_contract.py``; here the component WS is a
canned ``AsyncMock`` and the assertions are purely about the routing decision
and the request the server sends.
"""

from __future__ import annotations

from collections import Counter

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.tools import component_api, tools_search
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import VISIBILITY_FILENAME, save_visibility_config

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)
from .test_ha_search_component_routing import (
    RoutingClient,
    _build_ha_search,
    _entity_search_result,
)

# The nine hide dimensions ``VisibilityConfig.to_wire`` emits — the exact param
# shape the server hands a ``search_visibility``-capable component.
_WIRE_KEYS = {
    "exclude_categories",
    "exclude_hidden",
    "deny_entity_ids",
    "exclude_areas",
    "exclude_labels",
    "allow_entity_ids",
    "allow_areas",
    "allow_labels",
    "respect_assist_exposure",
}

_CAPS_SEARCH = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search"],
    "limits": {},
}
_CAPS_SEARCH_VIS = {
    "schema_version": 1,
    "component_version": "1.2.0",
    "capabilities": ["search", "search_visibility"],
    "limits": {},
}


def _write_active_deny(tmp_path, monkeypatch) -> None:
    """An enabled config whose only active dimension denies ``light.kitchen``."""
    save_visibility_config(
        tmp_path,
        VisibilityConfig(
            enabled=True,
            exclude_categories=[],
            deny_entity_ids=["light.kitchen"],
        ),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


def _write_disabled(tmp_path, monkeypatch) -> None:
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


def _search_calls(ws):
    return [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/search"
    ]


@pytest.mark.asyncio
async def test_active_filter_with_capability_routes_component_with_param(
    tmp_path, monkeypatch
) -> None:
    """Active filter + ``search_visibility`` → component fast path, ``visibility`` sent."""
    _write_active_deny(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH_VIS,
        cmd_result=_entity_search_result(),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    # Component served the whole thing — no legacy inventory fetches.
    assert client.get_states_calls == 0
    assert client.ws_types == Counter()

    calls = _search_calls(ws)
    assert len(calls) == 1
    visibility = calls[0].kwargs.get("visibility")
    assert visibility is not None, "visibility param must ride the component request"
    # Exactly the nine hide dimensions — no ``enabled`` / ``version`` leakage.
    assert set(visibility) == _WIRE_KEYS
    assert visibility["deny_entity_ids"] == ["light.kitchen"]
    assert visibility["exclude_categories"] == []


@pytest.mark.asyncio
async def test_active_filter_without_capability_uses_legacy(
    tmp_path, monkeypatch
) -> None:
    """Active filter + only ``search`` → legacy path, no component search command."""
    _write_active_deny(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_result=_entity_search_result(),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    # The component search command must never run without the capability.
    assert not _search_calls(ws)
    # Legacy inventory served the request and dropped the denied entity.
    assert client.get_states_calls == 1
    entity_ids = {e["entity_id"] for e in resp["entities"]}
    assert "light.kitchen" not in entity_ids
    assert "sensor.kitchen_temp" in entity_ids


@pytest.mark.asyncio
async def test_inactive_filter_routes_component_without_param(
    tmp_path, monkeypatch
) -> None:
    """No active filter → component fast path, and NO ``visibility`` param.

    Even a ``search_visibility``-capable component receives no param when the
    filter is inactive, so there is nothing for it to (wrongly) exclude and an
    old ``search``-only component is unaffected.
    """
    _write_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH_VIS,
        cmd_result=_entity_search_result(),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 0
    calls = _search_calls(ws)
    assert len(calls) == 1
    assert "visibility" not in calls[0].kwargs


@pytest.mark.asyncio
async def test_component_error_on_visibility_path_falls_back_to_legacy(
    tmp_path, monkeypatch
) -> None:
    """A component error on the visibility path → legacy, which re-applies the filter."""
    _write_active_deny(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH_VIS,
        cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])
    # Fallback still honours the filter (legacy excludes the denied entity).
    entity_ids = {e["entity_id"] for e in resp["entities"]}
    assert "light.kitchen" not in entity_ids


@pytest.mark.asyncio
async def test_unknown_command_falls_back_silently_and_invalidates(
    tmp_path, monkeypatch
) -> None:
    """unknown_command on the visibility search → invalidate caps + silent legacy
    fallback (no warning), same taxonomy as the plain ``search`` path."""
    _write_active_deny(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH_VIS,
        cmd_exc=HomeAssistantCommandError("Command failed: nope", "unknown_command"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    # Legacy inventory served the request and still honours the filter.
    assert client.get_states_calls == 1
    entity_ids = {e["entity_id"] for e in resp["entities"]}
    assert "light.kitchen" not in entity_ids
    # Silent fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))
    # Caps dropped from the cache so the next call re-probes.
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_connection_error_on_visibility_path_falls_back_to_legacy(
    tmp_path, monkeypatch
) -> None:
    """A WS-down error on the visibility search frame falls back to legacy (with a
    warning), still honouring the filter — the legacy path reads REST states + the
    swallowing registry bridge, so it does not die identically."""
    _write_active_deny(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH_VIS,
        cmd_exc=HomeAssistantConnectionError("ws down"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])
    entity_ids = {e["entity_id"] for e in resp["entities"]}
    assert "light.kitchen" not in entity_ids
    # A transient connection error is not a downgrade — caps stay cached.
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_ws_establish_failure_on_visibility_path_falls_back_to_legacy(
    tmp_path, monkeypatch
) -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) falls back to legacy with a warning, filter still applied."""
    _write_active_deny(tmp_path, monkeypatch)
    caps_ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH_VIS)
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws_establish_failure(
        caps_ws,
        tools_search,
        HomeAssistantConnectionError("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])
    entity_ids = {e["entity_id"] for e in resp["entities"]}
    assert "light.kitchen" not in entity_ids
    assert client in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_unloadable_config_fails_closed_to_legacy(tmp_path, monkeypatch) -> None:
    """A malformed config file → fail-closed to legacy, never an unfiltered component.

    ``visibility_filter_active`` fails closed to True on a load error and
    ``load_visibility_wire`` returns None (no config to serialize), so the
    capability branch cannot route — the request stays on the legacy path.
    """
    (tmp_path / VISIBILITY_FILENAME).write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH_VIS,
        cmd_result=_entity_search_result(),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    # No component search ran; the legacy inventory served the request.
    assert not _search_calls(ws)
    assert client.get_states_calls == 1
