"""Routing tests for ``ha_get_entity`` enrichment over the ``ha_mcp_tools`` gate.

``ha_get_entity`` serves its base registry record from the native
``config/entity_registry/get`` (single) / ``config/entity_registry/get_entries``
(bulk) reads Phase 0 already landed. When the component advertises
``entity_enrich``, one ``ha_mcp_tools/entity_enrich`` frame additively decorates
each record with the resolved area/floor NAMES and label NAMES the raw registry
entry lacks (it carries ``area_id`` / label ids). These tests pin the additive
merge, that the enrichment keys are ABSENT on a capability miss (legacy shape
unchanged), and the error-taxonomy fallbacks (``unknown_command`` → invalidate the
cached caps + fields absent; a command error/timeout → fields absent silently).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import component_api, tools_entities
from ha_mcp.tools.tools_entities import register_entity_tools

from ._component_routing_helpers import make_ws, patch_ws

_CAPS_ENRICH = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["entity_enrich"],
    "limits": {},
}
_CAPS_NONE = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": [],
    "limits": {},
}


def _raw_entry(
    entity_id: str, *, area_id: str | None, labels: list[str]
) -> dict[str, Any]:
    """A raw HA extended registry entry (``config/entity_registry/get`` result)."""
    return {
        "entity_id": entity_id,
        "name": None,
        "original_name": entity_id,
        "icon": None,
        "area_id": area_id,
        "disabled_by": None,
        "hidden_by": None,
        "aliases": [],
        "labels": labels,
        "categories": {},
        "device_class": None,
        "original_device_class": None,
        "options": {},
        "platform": "hue",
        "device_id": "dev-1",
        "config_entry_id": "cfg-1",
        "unique_id": f"uid-{entity_id}",
    }


def _enrichment(entity_id: str) -> dict[str, Any]:
    return {"area": "Kitchen", "floor": "Main", "labels": ["Favorites"], "aliases": []}


class RoutingClient:
    """Credentialed HA client spy serving the native registry reads."""

    def __init__(self, entries: dict[str, dict[str, Any]]) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self._entries = dict(entries)
        self.ws_calls: list[str] = []

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type")
        self.ws_calls.append(msg_type)
        if msg_type == "config/entity_registry/get":
            eid = msg.get("entity_id")
            if eid in self._entries:
                return {"success": True, "result": self._entries[eid]}
            return {"success": False, "error": {"message": "not found"}}
        if msg_type == "config/entity_registry/get_entries":
            ids = msg.get("entity_ids") or []
            return {
                "success": True,
                "result": {e: self._entries[e] for e in ids if e in self._entries},
            }
        raise AssertionError(f"unexpected ws message {msg_type!r}")


def _build_get_entity(client: Any) -> Any:
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
    register_entity_tools(mcp, client)
    return registered["ha_get_entity"]


@pytest.fixture(autouse=True)
def _clear_caps_cache() -> Any:
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()
    yield
    component_api._CAPS_CACHE.clear()
    component_api._CAPS_LOCKS.clear()


def _enrich_calls(ws: Any) -> list[Any]:
    return [
        c
        for c in ws.send_command.call_args_list
        if c.args[0] == "ha_mcp_tools/entity_enrich"
    ]


@pytest.mark.asyncio
async def test_single_entity_enriched_via_component() -> None:
    """A single-entity get gains area/floor/label_names from one enrich frame."""
    ws = make_ws(
        "ha_mcp_tools/entity_enrich",
        info_result=_CAPS_ENRICH,
        cmd_result={"entities": {"light.a": _enrichment("light.a")}},
    )
    client = RoutingClient(
        {"light.a": _raw_entry("light.a", area_id="ar1", labels=["lb1"])}
    )
    get_entity = _build_get_entity(client)

    with patch_ws(ws, tools_entities):
        resp = await get_entity("light.a")

    entry = resp["entity_entry"]
    # Base fields unchanged.
    assert entry["entity_id"] == "light.a"
    assert entry["area_id"] == "ar1"
    assert entry["labels"] == ["lb1"]
    # Additive resolved-name enrichment, under non-clobbering keys.
    assert entry["area"] == "Kitchen"
    assert entry["floor"] == "Main"
    assert entry["label_names"] == ["Favorites"]
    assert len(_enrich_calls(ws)) == 1
    assert _enrich_calls(ws)[0].kwargs["entity_ids"] == ["light.a"]


@pytest.mark.asyncio
async def test_bulk_entities_enriched_via_component() -> None:
    """Each found record in a bulk get is enriched from the same enrich frame."""
    ws = make_ws(
        "ha_mcp_tools/entity_enrich",
        info_result=_CAPS_ENRICH,
        cmd_result={
            "entities": {
                "light.a": _enrichment("light.a"),
                "light.b": {"area": "Den", "floor": None, "labels": [], "aliases": []},
            }
        },
    )
    client = RoutingClient(
        {
            "light.a": _raw_entry("light.a", area_id="ar1", labels=["lb1"]),
            "light.b": _raw_entry("light.b", area_id=None, labels=[]),
        }
    )
    get_entity = _build_get_entity(client)

    with patch_ws(ws, tools_entities):
        resp = await get_entity(["light.a", "light.b"])

    by_id = {e["entity_id"]: e for e in resp["entity_entries"]}
    assert by_id["light.a"]["area"] == "Kitchen"
    assert by_id["light.a"]["label_names"] == ["Favorites"]
    assert by_id["light.b"]["area"] == "Den"
    assert by_id["light.b"]["label_names"] == []
    assert len(_enrich_calls(ws)) == 1
    assert set(_enrich_calls(ws)[0].kwargs["entity_ids"]) == {"light.a", "light.b"}


@pytest.mark.asyncio
async def test_no_capability_leaves_base_shape() -> None:
    """Component without entity_enrich → no enrich frame, no enrichment keys."""
    ws = make_ws("ha_mcp_tools/entity_enrich", info_result=_CAPS_NONE)
    client = RoutingClient(
        {"light.a": _raw_entry("light.a", area_id="ar1", labels=["lb1"])}
    )
    get_entity = _build_get_entity(client)

    with patch_ws(ws, tools_entities):
        resp = await get_entity("light.a")

    entry = resp["entity_entry"]
    assert "area" not in entry
    assert "floor" not in entry
    assert "label_names" not in entry
    assert not _enrich_calls(ws)


@pytest.mark.asyncio
async def test_unknown_command_invalidates_and_omits_fields() -> None:
    """unknown_command on the enrich frame → invalidate caps + fields absent."""
    ws = make_ws(
        "ha_mcp_tools/entity_enrich",
        info_result=_CAPS_ENRICH,
        cmd_exc=HomeAssistantCommandError("gone", "unknown_command"),
    )
    client = RoutingClient(
        {"light.a": _raw_entry("light.a", area_id="ar1", labels=["lb1"])}
    )
    get_entity = _build_get_entity(client)

    with patch_ws(ws, tools_entities):
        resp = await get_entity("light.a")

    entry = resp["entity_entry"]
    assert entry["entity_id"] == "light.a"
    assert "area" not in entry
    # The stale positive caps entry was dropped so the next call re-probes.
    assert client not in component_api._CAPS_CACHE


@pytest.mark.asyncio
async def test_command_error_omits_fields_silently() -> None:
    """A non-unknown command error/timeout → fields absent, base record intact."""
    ws = make_ws(
        "ha_mcp_tools/entity_enrich",
        info_result=_CAPS_ENRICH,
        cmd_exc=HomeAssistantCommandTimeout("timeout"),
    )
    client = RoutingClient(
        {"light.a": _raw_entry("light.a", area_id="ar1", labels=["lb1"])}
    )
    get_entity = _build_get_entity(client)

    with patch_ws(ws, tools_entities):
        resp = await get_entity("light.a")

    entry = resp["entity_entry"]
    assert entry["entity_id"] == "light.a"
    assert "area" not in entry
    assert "label_names" not in entry
