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


class _DomainClient:
    """Client for the domain-listing path: states + registry + get_config
    (the domain path wraps its result via add_timezone_metadata)."""

    def __init__(self, states, registry):
        self._states = states
        self._registry = registry

    async def get_states(self):
        return self._states

    async def send_websocket_message(self, msg):
        assert msg == {"type": "config/entity_registry/list"}
        return self._registry

    async def get_config(self):
        return {"time_zone": "UTC"}


def test_domain_only_search_excludes_denied_and_counts_stay_coherent(
    tmp_path, monkeypatch
):
    """_search_domain_only applies the filter before pagination, so a hidden
    entity is gone AND total_matches reflects the post-filter set."""
    states = [
        {"entity_id": "sensor.keep", "state": "1", "attributes": {}},
        {"entity_id": "sensor.drop", "state": "2", "attributes": {}},
    ]
    registry = {
        "success": True,
        "result": [
            {"entity_id": "sensor.keep", "entity_category": None},
            {"entity_id": "sensor.drop", "entity_category": "diagnostic"},
        ],
    }
    save_visibility_config(
        tmp_path,
        VisibilityConfig(enabled=True, exclude_categories=["diagnostic"]),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
    tools = tools_search.SearchTools(_DomainClient(states, registry), smart_tools=None)
    res = asyncio.run(
        tools._search_domain_only(
            query=None,
            domain_filter="sensor",
            state_filter=None,
            limit=10,
            offset=0,
            include_hidden_bool=True,
            group_by_domain_bool=False,
            per_domain_limit_int=None,
            parsed_result_fields=None,
        )
    )
    data = res["data"]
    ids = [r["entity_id"] for r in data["results"]]
    assert ids == ["sensor.keep"]  # diagnostic sensor.drop excluded
    assert data["total_matches"] == 1  # count reflects post-filter set, not raw 2


class _GetStateClient:
    """Minimal client for the targeted single-entity read path."""

    async def get_entity_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    async def get_config(self):
        return {"time_zone": "UTC"}


def test_targeted_get_state_ignores_visibility_filter(tmp_path, monkeypatch):
    """Tier-B contract: a targeted read returns the entity even when an enabled
    denylist would hide it from collection reads. This is the runnable, in-process
    counterpart to the container-only e2e Tier-B assertion."""
    denied = "input_boolean.denied_probe"
    # Filter enabled and denying the entity — a collection read would drop it.
    save_visibility_config(
        tmp_path,
        VisibilityConfig(enabled=True, exclude_categories=[], deny_entity_ids=[denied]),
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    tools = tools_search.SearchTools(_GetStateClient(), smart_tools=None)
    res = asyncio.run(tools._get_single_entity_state(denied, None, None, False))

    # Targeted path never consults the filter — the entity is still returned.
    assert res["data"]["entity_id"] == denied
