"""Visibility hard-exclude wired into the smart_search entity helpers."""

import asyncio
import json

from ha_mcp.tools.smart_search._entities import EntitySearchMixin
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


def test_filter_hidden_entities_hard_excludes_visibility_set():
    entities = [{"entity_id": "sensor.a"}, {"entity_id": "light.b"}]
    survivor_ids, survivor_states = EntitySearchMixin._filter_hidden_entities(
        entities,
        registry_slim={},
        include_hidden=True,
        visibility_hidden={"sensor.a"},
    )
    assert survivor_ids == ["light.b"]
    assert [s["entity_id"] for s in survivor_states] == ["light.b"]


def test_filter_hidden_entities_empty_set_keeps_all():
    entities = [{"entity_id": "sensor.a"}, {"entity_id": "light.b"}]
    survivor_ids, _ = EntitySearchMixin._filter_hidden_entities(
        entities, registry_slim={}, include_hidden=True, visibility_hidden=set()
    )
    assert survivor_ids == ["sensor.a", "light.b"]


def test_resolve_entity_areas_hard_excludes_visibility_set():
    entity_reg_map = {
        "sensor.a": {"area_id": "kitchen", "device_id": None, "hidden_by": None},
        "light.b": {"area_id": "kitchen", "device_id": None, "hidden_by": None},
    }
    resolved, _hidden = EntitySearchMixin._resolve_entity_areas(
        entity_reg_map,
        device_area_map={},
        include_hidden=True,
        visibility_hidden={"sensor.a"},
    )
    assert resolved == {"light.b": "kitchen"}


def test_resolve_entity_areas_empty_set_keeps_all():
    entity_reg_map = {
        "sensor.a": {"area_id": "kitchen", "device_id": None, "hidden_by": None},
    }
    resolved, _hidden = EntitySearchMixin._resolve_entity_areas(
        entity_reg_map,
        device_area_map={},
        include_hidden=True,
        visibility_hidden=set(),
    )
    assert resolved == {"sensor.a": "kitchen"}


# --- End-to-end (full-method) coverage for the two smart_search filter sites ---

_STATES = [
    {"entity_id": "light.keep", "state": "on", "attributes": {"friendly_name": "Keep"}},
    {"entity_id": "sensor.drop", "state": "1", "attributes": {"friendly_name": "Drop"}},
]
_ENTITY_REGISTRY = {
    "success": True,
    "result": [
        {
            "entity_id": "light.keep",
            "entity_category": None,
            "hidden_by": None,
            "area_id": "kitchen",
            "device_id": None,
        },
        {
            "entity_id": "sensor.drop",
            "entity_category": "diagnostic",
            "hidden_by": None,
            "area_id": "kitchen",
            "device_id": None,
        },
    ],
}


class _FetchClient:
    """Serves _fetch_search_entities: states + entity registry + get_entries."""

    async def get_states(self):
        return _STATES

    async def send_websocket_message(self, msg):
        if msg["type"] == "config/entity_registry/list":
            return _ENTITY_REGISTRY
        # get_entries (aliases) — benign empty response
        return {"success": True, "result": []}


class _AreaClient:
    """Serves get_entities_by_area: states + area/entity/device registries."""

    async def get_states(self):
        return _STATES

    async def send_websocket_message(self, msg):
        t = msg["type"]
        if t == "config/area_registry/list":
            return {
                "success": True,
                "result": [{"area_id": "kitchen", "name": "Kitchen"}],
            }
        if t == "config/entity_registry/list":
            return _ENTITY_REGISTRY
        return {"success": True, "result": []}  # device registry


def _enable_diagnostic_filter(tmp_path, monkeypatch):
    save_visibility_config(
        tmp_path,
        VisibilityConfig(enabled=True, exclude_categories=["diagnostic"]),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


def test_fetch_search_entities_excludes_denied_end_to_end(tmp_path, monkeypatch):
    """Full _fetch_search_entities path: the diagnostic entity is filtered out."""
    _enable_diagnostic_filter(tmp_path, monkeypatch)
    mixin = EntitySearchMixin()
    mixin.client = _FetchClient()
    out = asyncio.run(
        mixin._fetch_search_entities(domain_filter=None, include_hidden=True)
    )
    ids = [e["entity_id"] for e in out]
    assert ids == ["light.keep"]  # sensor.drop (diagnostic) excluded


def test_get_entities_by_area_excludes_denied_end_to_end(tmp_path, monkeypatch):
    """Full get_entities_by_area path: the diagnostic entity is gone from the
    area result and the total stays coherent."""
    _enable_diagnostic_filter(tmp_path, monkeypatch)
    mixin = EntitySearchMixin()
    mixin.client = _AreaClient()
    res = asyncio.run(
        mixin.get_entities_by_area(
            area_query="Kitchen", group_by_domain=False, include_hidden=True
        )
    )
    blob = json.dumps(res)
    assert "sensor.drop" not in blob
    assert "light.keep" in blob
    assert res["total_entities"] == 1  # only the surviving entity counted
