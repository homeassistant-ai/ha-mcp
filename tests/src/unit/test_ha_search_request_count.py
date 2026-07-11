"""Request-cost regression tests for ``ha_search``.

Pins the fetch economy of the default query-only path so a future refactor that
re-introduces a duplicate or discarded fetch fails loudly:

- ITEM 1 — no ``/api/config`` timezone fetch on the entity branch (entity records
  carry no timestamp fields, so the enrichment was discarded).
- ITEM 2 — a single ``/api/states`` shared by the entity + config branches.
- ITEM 3 — a single ``config/entity_registry/list`` shared the same way.
- ITEM 5 — ``config/device_registry/list`` is fetched only when the visibility
  config has an area/label dimension that consumes it; ``get_entities_by_area``
  still fetches it regardless (entity->device->area fallback).
- ITEM 4 — the fuzzy path fetches aliases only for entities that survive the
  domain filter.

The default path is exact-match, so it exercises ``_exact_match_search`` +
``deep_search``; the config-body sub-fetches fail against these minimal mocks
(``partial: True``), which does not affect the shared/gated counts under test.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest.mock import MagicMock

import pytest

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
    {
        "entity_id": "scene.movie",
        "state": "scening",
        "attributes": {"friendly_name": "Movie"},
    },
]
_ENTITY_REGISTRY = {
    "success": True,
    "result": [
        {"entity_id": "light.kitchen", "entity_category": None},
        {"entity_id": "sensor.kitchen_temp", "entity_category": None},
        {"entity_id": "scene.movie", "unique_id": "movie", "platform": "homeassistant"},
    ],
}


class CountingClient:
    """Mock HA client that tallies the fetches ha_search makes.

    Only the shared/gated fetches under test return usable payloads; every other
    config-body list fetch returns a soft failure so ``deep_search`` degrades to
    ``partial: True`` instead of hanging on an un-mocked method.
    """

    def __init__(self) -> None:
        self.get_states_calls = 0
        self.get_config_calls = 0
        self.ws_types: Counter[str] = Counter()
        self.get_entries_entity_ids: list[list[str]] = []

    async def get_states(self) -> list[dict[str, Any]]:
        self.get_states_calls += 1
        return [dict(s) for s in _STATES]

    async def get_config(self) -> dict[str, Any]:
        self.get_config_calls += 1
        return {"time_zone": "UTC"}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/entity_registry/list":
            return _ENTITY_REGISTRY
        if msg_type == "config/device_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/entity_registry/get_entries":
            self.get_entries_entity_ids.append(list(msg.get("entity_ids", [])))
            return {"success": True, "result": {}}
        # Config-body list fetches (automation/script/scene/helper) — soft-fail.
        return {"success": False}

    async def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise Exception("bulk config REST unavailable")

    async def get_scene_config(self, scene_id: str) -> dict[str, Any]:
        return {"config": {}}

    async def get_script_config(self, script_id: str) -> dict[str, Any]:
        return {"config": {}}


def _build_ha_search(client: CountingClient):
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
    smart_tools = SmartSearchTools(client=client)
    register_search_tools(mcp, client, smart_tools=smart_tools)
    return registered["ha_search"]


@pytest.mark.asyncio
async def test_default_query_path_shares_snapshots_and_skips_gated_fetches(
    tmp_path, monkeypatch
):
    """Default disabled install: one /api/states, one entity-registry list, no
    /api/config on the entity branch, and no device registry."""
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    client = CountingClient()
    ha_search = _build_ha_search(client)
    resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    # ITEM 2: both branches share one state-machine fetch (was 2).
    assert client.get_states_calls == 1
    # ITEM 1: the entity branch no longer fetches /api/config for timezone.
    assert client.get_config_calls == 0
    # ITEM 3: both branches share one entity-registry list (was 2).
    assert client.ws_types["config/entity_registry/list"] == 1
    # ITEM 5: device registry is pure waste when visibility is disabled.
    assert client.ws_types["config/device_registry/list"] == 0


@pytest.mark.asyncio
async def test_visibility_area_rule_fetches_device_registry(tmp_path, monkeypatch):
    """An enabled config with an area exclude consumes the device registry, so the
    fetch returns — while the shared /api/states + registry economy is unchanged."""
    save_visibility_config(
        tmp_path, VisibilityConfig(enabled=True, exclude_areas=["garage"])
    )
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    client = CountingClient()
    ha_search = _build_ha_search(client)
    resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert client.get_config_calls == 0
    assert client.ws_types["config/entity_registry/list"] == 1
    # ITEM 5: the area dimension needs device-inherited areas, so it is fetched.
    assert client.ws_types["config/device_registry/list"] == 1


@pytest.mark.asyncio
async def test_get_entities_by_area_fetches_device_registry_when_visibility_disabled(
    tmp_path, monkeypatch
):
    """CRITICAL EXCLUSION: get_entities_by_area resolves entity->device->area via
    the device registry, so it must fetch it even with visibility disabled."""
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    client = CountingClient()
    smart_tools = SmartSearchTools(client=client)
    await smart_tools.get_entities_by_area("Kitchen", group_by_domain=False)

    assert client.ws_types["config/device_registry/list"] == 1


@pytest.mark.asyncio
async def test_fuzzy_domain_filter_fetches_aliases_only_for_survivors(
    tmp_path, monkeypatch
):
    """ITEM 4: the domain filter runs before the alias fetch, so get_entries only
    covers the domain survivors — not every entity in the state machine."""
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

    client = CountingClient()
    smart_tools = SmartSearchTools(client=client)
    await smart_tools.smart_entity_search("kitchen", domain_filter="light")

    # Aliases fetched exactly once, and only for the surviving light entity —
    # the sensor and scene are filtered out before the alias fan-out.
    assert client.get_entries_entity_ids == [["light.kitchen"]]
