"""Routing tests for ``ha_search`` over the ``ha_mcp_tools`` component gate.

When the component advertises the ``search`` capability, ``ha_search`` serves
the whole query from one ``ha_mcp_tools/search`` WebSocket call and skips the
legacy REST/WS fetch pipeline entirely. These tests pin that fast path and the
error-taxonomy fallbacks (silent on ``unknown_command``; legacy + ``warnings[]``
on any other command error), plus response-shape parity between the two paths.

The WS client is an ``AsyncMock`` whose ``send_command`` dispatches on the
command type. The HA client is a spy that tallies the legacy fetches so a test
can assert they never ran on the component path.
"""

from __future__ import annotations

from collections import Counter
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools import tools_search
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._component_routing_helpers import (
    make_ws,
    patch_ws,
    patch_ws_establish_failure,
)

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
]
_ENTITY_REGISTRY = {
    "success": True,
    "result": [
        {"entity_id": "light.kitchen", "entity_category": None},
        {"entity_id": "sensor.kitchen_temp", "entity_category": None},
    ],
}

_CAPS_SEARCH = {
    "schema_version": 1,
    "component_version": "1.1.0",
    "capabilities": ["search"],
    "limits": {},
}


class RoutingClient:
    """Credentialed HA client spy: tallies every legacy fetch ha_search makes."""

    def __init__(self) -> None:
        self.base_url = "http://ha.local:8123"
        self.token = "tok"
        self.get_states_calls = 0
        self.ws_types: Counter[str] = Counter()

    async def get_states(self) -> list[dict[str, Any]]:
        self.get_states_calls += 1
        return [dict(s) for s in _STATES]

    async def get_config(self) -> dict[str, Any]:
        return {"time_zone": "UTC"}

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        self.ws_types[msg_type] += 1
        if msg_type == "config/entity_registry/list":
            return _ENTITY_REGISTRY
        if msg_type == "config/device_registry/list":
            return {"success": True, "result": []}
        if msg_type == "config/entity_registry/get_entries":
            return {"success": True, "result": {}}
        return {"success": False}

    async def _request(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("bulk config REST must not be hit on the component path")

    async def get_scene_config(self, scene_id: str) -> dict[str, Any]:
        return {"config": {}}

    async def get_script_config(self, script_id: str) -> dict[str, Any]:
        return {"config": {}}


def _build_ha_search(client: Any) -> Any:
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
    register_search_tools(mcp, client, smart_tools=SmartSearchTools(client=client))
    return registered["ha_search"]


def _setup_visibility_disabled(tmp_path: Any, monkeypatch: Any) -> None:
    save_visibility_config(tmp_path, VisibilityConfig(enabled=False))
    monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)


def _entity_search_result() -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": "light.kitchen",
                "friendly_name": "Kitchen",
                "domain": "light",
                "state": "on",
                "score": 100,
                "match_type": "exact",
            }
        ],
        "entity_total_matches": 1,
        "entity_has_more": False,
        "config_total_matches": 0,
        "config_has_more": False,
        "partial": False,
    }


@pytest.mark.asyncio
async def test_component_fast_path_skips_legacy_fetches(tmp_path, monkeypatch) -> None:
    """When the component serves search, none of the legacy fetches are awaited."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_result=_entity_search_result(),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert resp["entities"][0]["entity_id"] == "light.kitchen"
    assert resp["entity_total_matches"] == 1
    # The legacy inventory is untouched: no /api/states, no registry list.
    assert client.get_states_calls == 0
    assert client.ws_types == Counter()
    # Exactly one component search command was issued.
    search_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/search"
    ]
    assert len(search_calls) == 1


@pytest.mark.asyncio
async def test_unknown_command_falls_back_silently(tmp_path, monkeypatch) -> None:
    """unknown_command on the search call → legacy path, no fallback warning."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_exc=HomeAssistantCommandError("Command failed: nope", "unknown_command"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    # Legacy inventory served the request.
    assert client.get_states_calls == 1
    assert client.ws_types["config/entity_registry/list"] == 1
    # Silent fallback: no component-failure warning.
    assert not any("served via legacy path" in w for w in resp.get("warnings", []))


@pytest.mark.asyncio
async def test_raised_command_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A non-unknown command error → legacy path AND a warnings[] entry."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_exc=HomeAssistantCommandError("Command failed: boom", "internal_error"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_caps_probed_once_across_searches(tmp_path, monkeypatch) -> None:
    """The info probe is cached: two searches, one ha_mcp_tools/info call."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_result=_entity_search_result(),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        await ha_search(query="kitchen")
        await ha_search(query="kitchen")

    info_calls = [
        c for c in ws.send_command.call_args_list if c.args[0] == "ha_mcp_tools/info"
    ]
    assert len(info_calls) == 1


@pytest.mark.asyncio
async def test_command_timeout_falls_back_with_warning(tmp_path, monkeypatch) -> None:
    """A component WS timeout → legacy path AND a warnings[] entry (not aborted)."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    ws = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_exc=HomeAssistantCommandTimeout("Command timeout"),
    )
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_ws_establish_failure_falls_back_with_warning(
    tmp_path, monkeypatch
) -> None:
    """A plain establish ``Exception`` from ``get_websocket_client()`` (after caps
    are cached) → legacy path AND a ``warnings[]`` entry, not a propagated error.

    The legacy search reads ``/api/states`` over REST + the swallowing registry
    bridge, so it does not die identically on a pooled-WS drop."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    caps_ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH)
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws_establish_failure(
        caps_ws,
        tools_search,
        Exception("Failed to connect to Home Assistant WebSocket"),
    ):
        resp = await ha_search(query="kitchen")

    assert resp["success"] is True
    assert client.get_states_calls == 1
    assert any("served via legacy path" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_component_diagnostics_mark_partial(tmp_path, monkeypatch) -> None:
    """Non-empty component ``diagnostics`` → partial True + reason names the surface."""
    _setup_visibility_disabled(tmp_path, monkeypatch)
    result = {
        **_entity_search_result(),
        "diagnostics": {"config_components_inaccessible": ["automation", "script"]},
    }
    ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH, cmd_result=result)
    client = RoutingClient()
    ha_search = _build_ha_search(client)

    with patch_ws(ws, tools_search):
        resp = await ha_search(query="kitchen")

    assert resp["partial"] is True
    reason = resp["partial_reason"]
    assert "config components inaccessible" in reason
    assert "automation" in reason and "script" in reason
    # The partial reason is mirrored onto the warnings channel agents read.
    assert any("config components inaccessible" in w for w in resp["warnings"])


@pytest.mark.asyncio
async def test_component_and_legacy_response_shape_parity(
    tmp_path, monkeypatch
) -> None:
    """Same query resolves to the same envelope shape on both serving paths.

    A body-skipped entity query (query + domain_filter) is served (a) by the
    component and (b) by the legacy pipeline over equivalent fixture data; the
    two responses must agree on their key set, pagination axis, and partial
    semantics.
    """
    _setup_visibility_disabled(tmp_path, monkeypatch)

    ws_component = make_ws(
        "ha_mcp_tools/search",
        info_result=_CAPS_SEARCH,
        cmd_result=_entity_search_result(),
    )
    client_component = RoutingClient()
    with patch_ws(ws_component, tools_search):
        component = await _build_ha_search(client_component)(
            query="kitchen", domain_filter="light"
        )

    # info → unknown_command yields no caps, so this run takes the legacy path.
    ws_legacy = make_ws(
        "ha_mcp_tools/search",
        info_exc=HomeAssistantCommandError(
            "Command failed: no info", "unknown_command"
        ),
    )
    client_legacy = RoutingClient()
    with patch_ws(ws_legacy, tools_search):
        legacy = await _build_ha_search(client_legacy)(
            query="kitchen", domain_filter="light"
        )

    assert set(component.keys()) == set(legacy.keys())
    assert component["partial"] == legacy["partial"] is False
    assert component["entity_total_matches"] == legacy["entity_total_matches"] == 1
    assert component["count"] == legacy["count"] == 1
    assert component["has_more"] == legacy["has_more"] is False
    assert component["next_offset"] == legacy["next_offset"]
    # Both paths surface the entity-intent body-skip warning.
    skip = "config-body search skipped"
    assert any(skip in w for w in component["warnings"])
    assert any(skip in w for w in legacy["warnings"])


class ListingModeClient(RoutingClient):
    """RoutingClient + area/floor registries so the area listing path works."""

    def __init__(self) -> None:
        super().__init__()
        self.registry_with_area = {
            "success": True,
            "result": [
                {
                    "entity_id": "light.kitchen",
                    "entity_category": None,
                    "area_id": "kitchen",
                },
                {
                    "entity_id": "sensor.kitchen_temp",
                    "entity_category": None,
                    "area_id": "kitchen",
                },
            ],
        }

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        if msg_type == "config/area_registry/list":
            self.ws_types[msg_type] += 1
            return {
                "success": True,
                "result": [{"area_id": "kitchen", "name": "Kitchen"}],
            }
        if msg_type == "config/floor_registry/list":
            self.ws_types[msg_type] += 1
            return {"success": True, "result": []}
        if msg_type == "config/entity_registry/list":
            self.ws_types[msg_type] += 1
            return self.registry_with_area
        return await super().send_websocket_message(msg)


def _enriched_entity_search_result() -> dict[str, Any]:
    """A component search result whose entity record carries the enrichment join."""
    result = _entity_search_result()
    result["entities"][0].update(
        {
            "area": "Kitchen",
            "floor": "Main",
            "labels": ["Favorites"],
            "aliases": ["lamp"],
        }
    )
    return result


# The registry replies the legacy enrichment join reads (one get_entries + the
# area/floor/label/device lists), served by EnrichmentClient below.
_GET_ENTRIES_RESULT = {
    "success": True,
    "result": {
        "light.kitchen": {
            "entity_id": "light.kitchen",
            "aliases": ["lamp"],
            "area_id": "ar1",
            "labels": ["lb1"],
            "device_id": None,
        },
        "sensor.kitchen_temp": {
            "entity_id": "sensor.kitchen_temp",
            "aliases": [],
            "area_id": "ar1",
            "labels": [],
            "device_id": None,
        },
    },
}
_ENRICH_REGISTRIES = {
    "config/area_registry/list": {
        "success": True,
        "result": [{"area_id": "ar1", "name": "Kitchen", "floor_id": "f1"}],
    },
    "config/floor_registry/list": {
        "success": True,
        "result": [{"floor_id": "f1", "name": "Main"}],
    },
    "config/label_registry/list": {
        "success": True,
        "result": [{"label_id": "lb1", "name": "Favorites"}],
    },
    "config/device_registry/list": {"success": True, "result": []},
}


class EnrichmentClient(RoutingClient):
    """RoutingClient + the registry reads the legacy enrichment join needs."""

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        msg_type = msg.get("type", "")
        if msg_type == "config/entity_registry/get_entries":
            self.ws_types[msg_type] += 1
            return _GET_ENTRIES_RESULT
        if msg_type in _ENRICH_REGISTRIES:
            self.ws_types[msg_type] += 1
            return _ENRICH_REGISTRIES[msg_type]
        return await super().send_websocket_message(msg)


class TestResultFieldsEnrichment:
    """``result_fields`` opt-in area/floor/labels/aliases on both serving paths."""

    @pytest.mark.asyncio
    async def test_component_path_retains_requested_enrichment(
        self, tmp_path, monkeypatch
    ) -> None:
        """Component path: requested enrichment keys survive the projection."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = make_ws(
            "ha_mcp_tools/search",
            info_result=_CAPS_SEARCH,
            cmd_result=_enriched_entity_search_result(),
        )
        client = RoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search):
            resp = await ha_search(
                query="kitchen", result_fields=["entity_id", "area", "floor"]
            )

        rec = resp["entities"][0]
        assert rec == {"entity_id": "light.kitchen", "area": "Kitchen", "floor": "Main"}
        # No legacy enrichment fetch on the component path.
        assert client.ws_types == Counter()

    @pytest.mark.asyncio
    async def test_component_default_shape_unchanged(
        self, tmp_path, monkeypatch
    ) -> None:
        """No result_fields → the default six-key record (enrichment absent)."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = make_ws(
            "ha_mcp_tools/search",
            info_result=_CAPS_SEARCH,
            cmd_result=_enriched_entity_search_result(),
        )
        client = RoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search):
            resp = await ha_search(query="kitchen")

        rec = resp["entities"][0]
        assert set(rec) == {
            "entity_id",
            "friendly_name",
            "domain",
            "state",
            "score",
            "match_type",
        }
        assert "area" not in rec

    @pytest.mark.asyncio
    async def test_legacy_path_joins_enrichment(self, tmp_path, monkeypatch) -> None:
        """Legacy path: the registry join fills the requested enrichment fields."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        # info unknown_command → no caps → the legacy pipeline serves the search.
        ws = make_ws(
            "ha_mcp_tools/search",
            info_exc=HomeAssistantCommandError("no info", "unknown_command"),
        )
        client = EnrichmentClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search):
            resp = await ha_search(
                query="kitchen",
                result_fields=["entity_id", "area", "floor", "labels", "aliases"],
            )

        by_id = {r["entity_id"]: r for r in resp["entities"]}
        assert by_id["light.kitchen"]["area"] == "Kitchen"
        assert by_id["light.kitchen"]["floor"] == "Main"
        assert by_id["light.kitchen"]["labels"] == ["Favorites"]
        assert by_id["light.kitchen"]["aliases"] == ["lamp"]
        # The generalized area-mode join fetched get_entries + the name registries.
        assert client.ws_types["config/entity_registry/get_entries"] == 1
        assert client.ws_types["config/area_registry/list"] == 1

    @pytest.mark.asyncio
    async def test_legacy_default_skips_enrichment_fetch(
        self, tmp_path, monkeypatch
    ) -> None:
        """No result_fields → the legacy path issues no enrichment registry reads."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = make_ws(
            "ha_mcp_tools/search",
            info_exc=HomeAssistantCommandError("no info", "unknown_command"),
        )
        client = EnrichmentClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search):
            await ha_search(query="kitchen")

        assert client.ws_types["config/entity_registry/get_entries"] == 0
        assert client.ws_types["config/area_registry/list"] == 0

    @pytest.mark.asyncio
    async def test_unknown_result_field_rejected(self, tmp_path, monkeypatch) -> None:
        """An unknown result_fields name is a hard validation error, both paths."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = make_ws(
            "ha_mcp_tools/search",
            info_result=_CAPS_SEARCH,
            cmd_result=_enriched_entity_search_result(),
        )
        client = RoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), pytest.raises(ToolError) as excinfo:
            await ha_search(query="kitchen", result_fields=["frobnicate"])

        assert "Unknown result_fields" in str(excinfo.value)
        # Rejected before any backend was consulted.
        assert not ws.send_command.await_count


class TestListingModesBypassComponent:
    """The three legacy listing modes must NEVER route through the component.

    Regression for the first live e2e run of the component path
    (test_search_entities.py::test_search_entities_{empty,whitespace}_query_
    with_domain_filter and ::test_search_entities_area_filter_only): the
    component path stamped ``search_type: exact_match`` onto responses the
    legacy path labels ``domain_listing`` / ``area_only`` — and those modes
    carry mode-specific response shapes the component does not replicate.
    Only query-driven, non-area searches may route through the component.
    """

    @pytest.mark.asyncio
    async def test_empty_query_domain_listing_bypasses_component(
        self, tmp_path, monkeypatch
    ) -> None:
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = ListingModeClient()
        ha_search = _build_ha_search(client)
        ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH, cmd_result={})
        with patch_ws(ws, tools_search):
            data = await ha_search(domain_filter="light")
        assert data.get("search_type") == "domain_listing", data
        assert not ws.send_command.await_count, (
            "component must not be consulted for domain listings"
        )

    @pytest.mark.asyncio
    async def test_whitespace_query_domain_listing_bypasses_component(
        self, tmp_path, monkeypatch
    ) -> None:
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = ListingModeClient()
        ha_search = _build_ha_search(client)
        ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH, cmd_result={})
        with patch_ws(ws, tools_search):
            data = await ha_search(query="   ", domain_filter="light")
        assert data.get("search_type") == "domain_listing", data
        assert not ws.send_command.await_count

    @pytest.mark.asyncio
    async def test_area_filter_only_bypasses_component(
        self, tmp_path, monkeypatch
    ) -> None:
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = ListingModeClient()
        ha_search = _build_ha_search(client)
        ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH, cmd_result={})
        with patch_ws(ws, tools_search):
            data = await ha_search(area_filter="Kitchen")
        assert data.get("search_type") == "area_only", data
        assert not ws.send_command.await_count, (
            "component must not be consulted for area listings"
        )

    @pytest.mark.asyncio
    async def test_query_with_area_filter_bypasses_component(
        self, tmp_path, monkeypatch
    ) -> None:
        _setup_visibility_disabled(tmp_path, monkeypatch)
        client = ListingModeClient()
        ha_search = _build_ha_search(client)
        ws = make_ws("ha_mcp_tools/search", info_result=_CAPS_SEARCH, cmd_result={})
        with patch_ws(ws, tools_search):
            data = await ha_search(query="kitchen", area_filter="Kitchen")
        assert data.get("search_type") == "area_filtered_query", data
        assert not ws.send_command.await_count, (
            "component must not be consulted for area-scoped queries"
        )


class TestVisibilityFilterBypassesComponent:
    """An ACTIVE entity-visibility filter must force the legacy path.

    The component applies no visibility filtering, so a query search on a
    visibility-enabled install has to stay on the legacy pipeline that excludes
    hidden entities before the counts/pagination — otherwise a denied entity
    reappears in ha_search. Regression for the first live component e2e run
    (test_entity_visibility.py::test_visibility_denylist_hides_entity_from_
    search_but_get_state_returns_it and ::test_visibility_label_dimension_hides_
    entity_from_search).
    """

    @pytest.mark.asyncio
    async def test_active_deny_filter_bypasses_component(
        self, tmp_path, monkeypatch
    ) -> None:
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True,
                exclude_categories=[],
                deny_entity_ids=["light.kitchen"],
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
        client = RoutingClient()
        ha_search = _build_ha_search(client)
        ws = make_ws(
            "ha_mcp_tools/search",
            info_result=_CAPS_SEARCH,
            cmd_result=_entity_search_result(),
        )

        with patch_ws(ws, tools_search):
            data = await ha_search(query="kitchen")

        # The component search command must never run while the filter is active.
        assert not any(
            c.args[0] == "ha_mcp_tools/search" for c in ws.send_command.call_args_list
        ), "component search must not run while the visibility filter is active"
        # Legacy inventory served the request and the denied entity is gone.
        assert client.get_states_calls == 1
        entity_ids = {e["entity_id"] for e in data["entities"]}
        assert "light.kitchen" not in entity_ids
        assert "sensor.kitchen_temp" in entity_ids

    @pytest.mark.asyncio
    async def test_enabled_but_no_active_dimension_still_uses_component(
        self, tmp_path, monkeypatch
    ) -> None:
        # enabled=True with every dimension cleared hides nothing → the fast
        # component path is still eligible (no needless legacy fallback).
        save_visibility_config(
            tmp_path, VisibilityConfig(enabled=True, exclude_categories=[])
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)
        client = RoutingClient()
        ha_search = _build_ha_search(client)
        ws = make_ws(
            "ha_mcp_tools/search",
            info_result=_CAPS_SEARCH,
            cmd_result=_entity_search_result(),
        )

        with patch_ws(ws, tools_search):
            await ha_search(query="kitchen")

        assert any(
            c.args[0] == "ha_mcp_tools/search" for c in ws.send_command.call_args_list
        ), "no active hide dimension → component should still serve"
        assert client.get_states_calls == 0


class TestAreaModeEnrichmentConsolidation:
    """Area+query mode reuses the haystack ``get_entries`` for enrichment.

    The alias haystack fetch (all area entities) and the opt-in
    ``result_fields`` enrichment previously issued two
    ``config/entity_registry/get_entries`` calls for overlapping ids; the
    haystack's entries map is now threaded into the enrichment join so the
    whole flow costs exactly one.
    """

    @pytest.mark.asyncio
    async def test_area_query_enrichment_reuses_haystack_entries(self) -> None:
        client = EnrichmentClient()
        tools = tools_search.SearchTools(client, MagicMock())
        area_result = {
            "areas": {
                "ar1": {
                    "entities": {
                        "light": [
                            {
                                "entity_id": "light.kitchen",
                                "friendly_name": "Kitchen Light",
                                "state": "on",
                            }
                        ]
                    }
                }
            }
        }

        resp = await tools._search_area_with_query(
            query="kitchen",
            area_filter="Kitchen",
            area_result=area_result,
            domain_filter=None,
            state_filter=None,
            limit=10,
            offset=0,
            group_by_domain_bool=False,
            per_domain_limit_int=None,
            parsed_result_fields=["entity_id", "area", "aliases"],
        )

        rec = resp["results"][0]
        assert rec["area"] == "Kitchen"
        assert rec["aliases"] == ["lamp"]
        assert client.ws_types["config/entity_registry/get_entries"] == 1
