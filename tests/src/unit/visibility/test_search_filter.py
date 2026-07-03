"""Integration of the visibility filter into tools_search._exact_match_search."""

import asyncio

from ha_mcp.tools import tools_search
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


class _FakeClient:
    """Minimal client: get_states() + registry send_websocket_message()."""

    def __init__(self, states, registry):
        self._states = states
        self._registry = registry

    async def get_states(self):
        return self._states

    async def send_websocket_message(self, msg):
        assert msg == {"type": "config/entity_registry/list"}
        return self._registry


_STATES = [
    {"entity_id": "sensor.foo_batt", "state": "50", "attributes": {}},
    {"entity_id": "light.foo_lamp", "state": "on", "attributes": {}},
]
_REGISTRY = {
    "success": True,
    "result": [
        {"entity_id": "sensor.foo_batt", "entity_category": "diagnostic"},
        {"entity_id": "light.foo_lamp", "entity_category": None},
    ],
}


def _run_search(tmp_path, monkeypatch, config: VisibilityConfig):
    save_visibility_config(tmp_path, config)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    client = _FakeClient(_STATES, _REGISTRY)
    return asyncio.run(
        tools_search._exact_match_search(
            client, query="foo", domain_filter=None, limit=10
        )
    )


def test_enabled_category_exclude_drops_diagnostic_and_counts_stay_coherent(
    tmp_path, monkeypatch
):
    res = _run_search(
        tmp_path,
        monkeypatch,
        VisibilityConfig(enabled=True, exclude_categories=["diagnostic"]),
    )
    ids = [r["entity_id"] for r in res["results"]]
    assert ids == ["light.foo_lamp"]  # diagnostic sensor excluded
    # Coherence: the total reflects the post-filter set, not the raw 2.
    assert res["total_matches"] == 1


def test_disabled_config_returns_both(tmp_path, monkeypatch):
    res = _run_search(tmp_path, monkeypatch, VisibilityConfig(enabled=False))
    ids = sorted(r["entity_id"] for r in res["results"])
    assert ids == ["light.foo_lamp", "sensor.foo_batt"]


def test_corrupt_config_fails_open_returns_both(tmp_path, monkeypatch):
    (tmp_path / "entity_visibility.json").write_text("{ corrupt")
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    client = _FakeClient(_STATES, _REGISTRY)
    res = asyncio.run(
        tools_search._exact_match_search(
            client, query="foo", domain_filter=None, limit=10
        )
    )
    ids = sorted(r["entity_id"] for r in res["results"])
    assert ids == ["light.foo_lamp", "sensor.foo_batt"]
