"""The visibility "warnings last mile".

Covers ``merge_visibility_warnings`` directly and asserts a degradation warning
actually reaches each collection-read tool's *response* (not just the resolver
return) — the coverage gap that let the area path silently drop them. A corrupt
config is the cheapest trigger: ``load_hidden_set`` fails open with a warning.
"""

import asyncio

from ha_mcp.tools import tools_search
from ha_mcp.tools.smart_search._overview import SystemOverviewMixin
from ha_mcp.tools.util_helpers import merge_visibility_warnings
from ha_mcp.visibility import resolver

_CORRUPT = "{ corrupt"
_LOAD_FAIL = "could not be loaded"


# --- merge_visibility_warnings (direct) -------------------------------------


def test_merge_visibility_warnings_creates_list_and_returns_same_response():
    resp = {"success": True}
    out = merge_visibility_warnings(resp, ["w1"])
    assert out is resp  # returned for return-composition
    assert out["warnings"] == ["w1"]


def test_merge_visibility_warnings_extends_existing_list():
    resp = {"success": True, "warnings": ["pre"]}
    merge_visibility_warnings(resp, ["w1", "w2"])
    assert resp["warnings"] == ["pre", "w1", "w2"]


def test_merge_visibility_warnings_empty_is_noop():
    resp = {"success": True}
    merge_visibility_warnings(resp, [])
    assert "warnings" not in resp


# --- response-level: a corrupt config surfaces a warning at each seam --------


class _SearchClient:
    def __init__(self, states, registry):
        self._states = states
        self._registry = registry

    async def get_states(self):
        return self._states

    async def send_websocket_message(self, msg):
        assert msg == {"type": "config/entity_registry/list"}
        return self._registry


class _OverviewClient:
    def __init__(self, states, entity_registry):
        self._states = states
        self._entity_registry = entity_registry

    async def get_states(self):
        return self._states

    async def get_services(self):
        return []

    async def send_websocket_message(self, msg):
        if msg["type"] == "config/entity_registry/list":
            return self._entity_registry
        return {"success": True, "result": []}


_STATES = [{"entity_id": "light.a", "state": "on", "attributes": {}}]
_REGISTRY = {
    "success": True,
    "result": [{"entity_id": "light.a", "entity_category": None}],
}


def test_exact_match_search_surfaces_visibility_load_warning(tmp_path, monkeypatch):
    (tmp_path / "entity_visibility.json").write_text(_CORRUPT)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    res = asyncio.run(
        tools_search._exact_match_search(
            _SearchClient(_STATES, _REGISTRY),
            query="a",
            domain_filter=None,
            limit=10,
        )
    )
    assert any(_LOAD_FAIL in w for w in res.get("warnings", []))


def test_system_overview_surfaces_visibility_load_warning(tmp_path, monkeypatch):
    (tmp_path / "entity_visibility.json").write_text(_CORRUPT)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    mixin = SystemOverviewMixin()
    mixin.client = _OverviewClient(_STATES, _REGISTRY)
    res = asyncio.run(mixin.get_system_overview(detail_level="full"))
    assert any(_LOAD_FAIL in w for w in res.get("warnings", []))
