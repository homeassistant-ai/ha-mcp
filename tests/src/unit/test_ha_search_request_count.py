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

The search runs on the server's own client against a real ``hass`` without
the component, so it takes the legacy path; call-through spies count what the
client sends while Core answers every request.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config


@dataclass
class _Sent:
    """What the client sent while the spies were on."""

    states: AsyncMock
    config: AsyncMock
    websocket: AsyncMock

    def ws_types(self) -> Counter[str]:
        return Counter(
            call.args[0].get("type") for call in self.websocket.call_args_list
        )

    def ws_messages(self, msg_type: str) -> list[dict[str, Any]]:
        return [
            call.args[0]
            for call in self.websocket.call_args_list
            if call.args[0].get("type") == msg_type
        ]


@contextmanager
def _spies(client: HomeAssistantClient) -> Iterator[_Sent]:
    """Call-through spies on the reads under test; Core still answers each."""
    with (
        patch.object(client, "get_states", wraps=client.get_states) as states,
        patch.object(client, "get_config", wraps=client.get_config) as config,
        patch.object(
            client, "send_websocket_message", wraps=client.send_websocket_message
        ) as websocket,
    ):
        yield _Sent(states, config, websocket)


def _visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: VisibilityConfig
) -> None:
    save_visibility_config(tmp_path, config)
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


@pytest.fixture
async def smart_tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> SmartSearchTools:
    """Core with a kitchen light, a kitchen sensor and a scene, all registered."""
    # ``config`` serves the registry reads.
    assert await async_setup_component(hass, "config", {})
    for domain, object_id, name in (
        ("light", "kitchen", "Kitchen"),
        ("sensor", "kitchen_temp", "Kitchen Temp"),
        ("scene", "movie", "Movie"),
    ):
        entry = entity_registry.async_get_or_create(
            domain, "test", object_id, suggested_object_id=object_id
        )
        hass.states.async_set(entry.entity_id, "on", {"friendly_name": name})
    return SmartSearchTools(client=core_client)


@pytest.fixture
def search_tools(
    core_client: HomeAssistantClient, smart_tools: SmartSearchTools
) -> SearchTools:
    return SearchTools(core_client, smart_tools)


async def test_default_query_path_shares_snapshots_and_skips_gated_fetches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    search_tools: SearchTools,
) -> None:
    """Default disabled install: one /api/states, one entity-registry list, no
    /api/config on the entity branch, and no device registry."""
    _visibility(tmp_path, monkeypatch, VisibilityConfig(enabled=False))

    with _spies(core_client) as sent:
        resp = await search_tools.ha_search(query="kitchen")

    assert resp["success"] is True
    assert [row["entity_id"] for row in resp["entities"]] == [
        "light.kitchen",
        "sensor.kitchen_temp",
    ]
    # ITEM 2: both branches share one state-machine fetch (was 2).
    assert sent.states.await_count == 1
    # ITEM 1: the entity branch no longer fetches /api/config for timezone.
    sent.config.assert_not_awaited()
    # ITEM 3: both branches share one entity-registry list (was 2).
    assert sent.ws_types()["config/entity_registry/list"] == 1
    # ITEM 5: device registry is pure waste when visibility is disabled.
    assert sent.ws_types()["config/device_registry/list"] == 0


async def test_visibility_area_rule_fetches_device_registry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    search_tools: SearchTools,
) -> None:
    """An enabled config with an area exclude consumes the device registry, so the
    fetch returns — while the shared /api/states + registry economy is unchanged."""
    _visibility(
        tmp_path,
        monkeypatch,
        VisibilityConfig(enabled=True, exclude_areas=["garage"]),
    )

    with _spies(core_client) as sent:
        resp = await search_tools.ha_search(query="kitchen")

    assert resp["success"] is True
    assert sent.states.await_count == 1
    sent.config.assert_not_awaited()
    assert sent.ws_types()["config/entity_registry/list"] == 1
    # ITEM 5: the area dimension needs device-inherited areas, so it is fetched.
    assert sent.ws_types()["config/device_registry/list"] == 1


async def test_get_entities_by_area_fetches_device_registry_when_visibility_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    smart_tools: SmartSearchTools,
    area_registry: ar.AreaRegistry,
) -> None:
    """CRITICAL EXCLUSION: get_entities_by_area resolves entity->device->area via
    the device registry, so it must fetch it even with visibility disabled."""
    _visibility(tmp_path, monkeypatch, VisibilityConfig(enabled=False))
    area_registry.async_create("Kitchen")

    with _spies(core_client) as sent:
        await smart_tools.get_entities_by_area("Kitchen")

    assert sent.ws_types()["config/device_registry/list"] == 1


async def test_fuzzy_domain_filter_fetches_aliases_only_for_survivors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    smart_tools: SmartSearchTools,
) -> None:
    """ITEM 4: the domain filter runs before the alias fetch, so get_entries only
    covers the domain survivors — not every entity in the state machine."""
    _visibility(tmp_path, monkeypatch, VisibilityConfig(enabled=False))

    with _spies(core_client) as sent:
        await smart_tools.smart_entity_search("kitchen", domain_filter="light")

    # Aliases fetched exactly once, and only for the surviving light entity —
    # the sensor and scene are filtered out before the alias fan-out.
    assert [
        msg["entity_ids"]
        for msg in sent.ws_messages("config/entity_registry/get_entries")
    ] == [["light.kitchen"]]


# The WS "bulk config list" command types the legacy fetch used to try when
# the REST bulk endpoint failed. None of them has ever existed in HA core, so
# every attempt came back ``unknown_command`` and was ERROR-logged by
# ``send_websocket_message`` — the paired "WebSocket message failed: Command
# failed: Unknown command." spam of issue #1889.
_PHANTOM_BULK_WS_TYPES = (
    "config/automation/config/list",
    "automation/config/list",
    "config/script/config/list",
    "script/config/list",
    "config/scene/config/list",
    "scene/config/list",
)


async def test_config_search_never_sends_phantom_bulk_ws_commands(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    search_tools: SearchTools,
) -> None:
    """Issue #1889: real HA has no bulk config REST endpoint, so the config
    branch must fall straight to budgeted individual fetches — never through
    the nonexistent ``config/<domain>/config/list`` /
    ``<domain>/config/list`` WebSocket commands."""
    _visibility(tmp_path, monkeypatch, VisibilityConfig(enabled=False))
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "kitchen_lights",
                "alias": "Kitchen lights",
                "triggers": [{"trigger": "event", "event_type": "kitchen_motion"}],
                "actions": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": "light.kitchen"},
                    }
                ],
            }
        },
    )

    with _spies(core_client) as sent:
        resp = await search_tools.ha_search(
            query="kitchen", search_types=["automation", "script", "scene"]
        )

    assert resp["success"] is True
    # The individual fetch was made: Core's per-id endpoint answered 404 for
    # the YAML automation, which the search names, and the name still matched.
    assert [row["entity_id"] for row in resp["automations"]] == [
        "automation.kitchen_lights"
    ]
    assert "per-id config endpoint returned 404" in resp["partial_reason"]
    for ws_type in _PHANTOM_BULK_WS_TYPES:
        assert sent.ws_types()[ws_type] == 0, ws_type
