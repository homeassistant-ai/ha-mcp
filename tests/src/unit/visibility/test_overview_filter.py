"""Runnable in-process coverage for the visibility filter in get_system_overview.

Drives the real ``get_system_overview`` with a fake client so the overview
wiring (M4) has non-e2e coverage: the container-only e2e is the outer proof,
this is the fast inner one. Asserts both the filter effect and count coherence
(total_entities / domain_stats reflect the post-filter universe)."""

import asyncio

from ha_mcp.tools.smart_search._overview import SystemOverviewMixin
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

_STATES = [
    {"entity_id": "light.keep", "state": "on", "attributes": {}},
    {"entity_id": "sensor.drop_batt", "state": "50", "attributes": {}},
]
_ENTITY_REGISTRY = {
    "success": True,
    "result": [
        {"entity_id": "light.keep", "entity_category": None},
        {"entity_id": "sensor.drop_batt", "entity_category": "diagnostic"},
    ],
}


class _OverviewClient:
    """Serves the 5-way gather in get_system_overview; only entity registry
    carries data, the other registries are empty."""

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


def _run_overview(tmp_path, monkeypatch, config: VisibilityConfig):
    save_visibility_config(tmp_path, config)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    mixin = SystemOverviewMixin()
    mixin.client = _OverviewClient(_STATES, _ENTITY_REGISTRY)
    return asyncio.run(mixin.get_system_overview(detail_level="full"))


def test_overview_enabled_drops_diagnostic_and_counts_stay_coherent(
    tmp_path, monkeypatch
):
    res = _run_overview(
        tmp_path,
        monkeypatch,
        VisibilityConfig(enabled=True, exclude_categories=["diagnostic"]),
    )
    assert res["success"] is True
    # Diagnostic entity removed from the universe before any stats are computed,
    # so both the total and the per-domain rollup omit it.
    assert res["system_summary"]["total_entities"] == 1
    assert res["system_summary"]["total_domains"] == 1
    assert "light" in res["domain_stats"]
    assert "sensor" not in res["domain_stats"]


def test_overview_disabled_keeps_all(tmp_path, monkeypatch):
    res = _run_overview(tmp_path, monkeypatch, VisibilityConfig(enabled=False))
    assert res["system_summary"]["total_entities"] == 2
    assert {"light", "sensor"} <= set(res["domain_stats"])
