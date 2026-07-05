"""Runnable in-process coverage for the visibility filter in get_system_overview.

Drives the real ``get_system_overview`` with a fake client so the overview
wiring (M4) has non-e2e coverage: the container-only e2e is the outer proof,
this is the fast inner one. Asserts both the filter effect and count coherence
(total_entities / domain_stats reflect the post-filter universe)."""

import asyncio

import pytest

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


class _DeviceRegistryOverviewClient(_OverviewClient):
    """Overview client that also serves a device registry (round-4 wiring proof)."""

    def __init__(self, states, entity_registry, device_registry):
        super().__init__(states, entity_registry)
        self._device_registry = device_registry

    async def send_websocket_message(self, msg):
        if msg["type"] == "config/device_registry/list":
            return self._device_registry
        return await super().send_websocket_message(msg)


def test_overview_excludes_device_inherited_area(tmp_path, monkeypatch):
    # Seam proof that the device registry reaches the resolver through the overview
    # gather (results[4]): a device-bound entity (registry area_id None + device_id)
    # is dropped by exclude_areas via its device's area. Guards the call-site
    # wiring, not just the pure resolver.
    states = [
        {"entity_id": "light.spot", "state": "on", "attributes": {}},
        {"entity_id": "light.keep", "state": "on", "attributes": {}},
    ]
    entity_registry = {
        "success": True,
        "result": [
            {"entity_id": "light.spot", "area_id": None, "device_id": "d1"},
            {"entity_id": "light.keep", "area_id": None, "device_id": None},
        ],
    }
    device_registry = {"success": True, "result": [{"id": "d1", "area_id": "garage"}]}
    save_visibility_config(
        tmp_path,
        VisibilityConfig(enabled=True, exclude_categories=[], exclude_areas=["garage"]),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    mixin = SystemOverviewMixin()
    mixin.client = _DeviceRegistryOverviewClient(
        states, entity_registry, device_registry
    )
    res = asyncio.run(mixin.get_system_overview(detail_level="full"))
    # light.spot dropped via its device's area; light.keep (no device) remains.
    assert res["system_summary"]["total_entities"] == 1


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


def test_overview_visibility_warning_does_not_mark_partial(tmp_path, monkeypatch):
    # A pure visibility warning on otherwise-complete data (an unknown exclude
    # category) surfaces in `warnings` but must NOT set `partial` — partial is for
    # genuinely incomplete data (e.g. a failed services fetch). Aligns overview
    # with ha_search, which reports the same warnings without a partial flag.
    res = _run_overview(
        tmp_path,
        monkeypatch,
        VisibilityConfig(enabled=True, exclude_categories=["typo"]),
    )
    assert any("unknown exclude_categories" in w for w in res.get("warnings", []))
    assert res.get("partial") is not True


class _ServicesFailOverviewClient(_OverviewClient):
    """get_system_overview client whose services fetch fails (partial data)."""

    async def get_services(self):
        raise RuntimeError("services boom")


def test_overview_services_failure_marks_partial_with_coexisting_visibility_warning(
    tmp_path, monkeypatch
):
    # The other half of the partial/warnings split: a genuine services failure sets
    # `partial: true` and surfaces "Services unavailable", and a coexisting pure
    # visibility warning (an unknown category) rides alongside in the same
    # `warnings` list without being conflated — the two channels (partial =
    # genuinely incomplete data, visibility = annotation on complete data) coexist.
    save_visibility_config(
        tmp_path, VisibilityConfig(enabled=True, exclude_categories=["typo"])
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    mixin = SystemOverviewMixin()
    mixin.client = _ServicesFailOverviewClient(_STATES, _ENTITY_REGISTRY)
    res = asyncio.run(mixin.get_system_overview(detail_level="full"))
    assert res.get("partial") is True
    warnings = res.get("warnings", [])
    assert any("Services unavailable" in w for w in warnings)
    assert any("unknown exclude_categories" in w for w in warnings)


class _StatesCancelOverviewClient(_OverviewClient):
    """get_system_overview client whose mandatory states fetch is cancelled."""

    async def get_states(self):
        raise asyncio.CancelledError


def test_overview_propagates_states_cancellation():
    """A cancelled states fetch propagates instead of being assigned to
    ``entities`` and crashing downstream iteration (mirrors the area-search
    siblings)."""
    mixin = SystemOverviewMixin()
    mixin.client = _StatesCancelOverviewClient(_STATES, _ENTITY_REGISTRY)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(mixin.get_system_overview(detail_level="full"))
