"""Routing tests for ``ha_search`` over the ``ha_mcp_tools`` component gate.

When the component advertises the ``search`` capability, ``ha_search`` serves
the whole query from one ``ha_mcp_tools/search`` WebSocket call and skips the
legacy REST/WS fetch pipeline entirely. These tests pin that fast path, the
error-taxonomy fallbacks (silent on ``unknown_command``; legacy + ``warnings[]``
on a timeout or a dropped connection), the capability gates an older component
falls under, and response parity between the two paths.

The search runs on the server's own client against a real ``hass``; spies on
each route show which one served it. The dashboard routes are in
``test_ha_search_dashboard_split.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools import websocket_api as component_ws_api
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.visibility import resolver
from ha_mcp.visibility.model import VisibilityConfig
from ha_mcp.visibility.persistence import save_visibility_config

from ._component_frames import drop_connection_on, frames, held, older_component, pooled
from ._search_routing import SEARCH, bridge_types, fell_back, routes
from ._search_tool import _build_ha_search, _setup_visibility_disabled


@pytest.fixture(autouse=True)
def _visibility_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)


def _kitchen(hass: HomeAssistant) -> None:
    """A Kitchen area on a Main floor holding a lamp and a temperature sensor."""
    floor = fr.async_get(hass).async_create("Main")
    kitchen = ar.async_get(hass).async_create("Kitchen", floor_id=floor.floor_id)
    favorites = lr.async_get(hass).async_create("Favorites")
    registry = er.async_get(hass)
    light = registry.async_get_or_create(
        "light", "test", "kitchen", suggested_object_id="kitchen"
    )
    registry.async_update_entity(
        light.entity_id,
        area_id=kitchen.id,
        labels={favorites.label_id},
        aliases=["lamp"],
    )
    sensor = registry.async_get_or_create(
        "sensor", "test", "kitchen_temp", suggested_object_id="kitchen_temp"
    )
    registry.async_update_entity(sensor.entity_id, area_id=kitchen.id)
    hass.states.async_set("light.kitchen", "on", {"friendly_name": "Kitchen"})
    hass.states.async_set(
        "sensor.kitchen_temp", "21", {"friendly_name": "Kitchen Temp"}
    )


@pytest.fixture
async def client(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """``core_client`` on a ``hass`` with the kitchen and every config surface."""
    for domain in ("config", "automation", "script", "scene"):
        assert await async_setup_component(hass, domain, {})
    _kitchen(hass)
    return core_client


@pytest.fixture
def component(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The same client with the component's commands registered."""
    return component_core_client


@pytest.fixture
async def socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled(client)


async def _search(client: HomeAssistantClient, **arguments: Any) -> dict[str, Any]:
    response: dict[str, Any] = await _build_ha_search(client)(**arguments)
    return response


def _ids(response: dict[str, Any]) -> list[str]:
    return [e["entity_id"] for e in response["entities"]]


@pytest.mark.usefixtures("component")
class TestComponentPath:
    async def test_component_fast_path_skips_legacy_fetches(
        self, client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """When the component serves search, none of the legacy fetches run."""
        with routes(client, socket) as spies:
            resp = await _search(client, query="kitchen")

        assert "light.kitchen" in _ids(resp)
        spies.states.assert_not_called()
        spies.bridge.assert_not_called()
        (request,) = frames(spies.component, SEARCH)
        assert "result_fields" not in request

    async def test_caps_probed_once_across_searches(
        self, client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """The info probe is cached: two searches, one ha_mcp_tools/info call."""
        with routes(client, socket) as spies:
            await _search(client, query="kitchen")
            await _search(client, query="kitchen")

        assert len(frames(spies.component, "ha_mcp_tools/info")) == 1

    async def test_membership_capability_forwards_only_requested_fields(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """A capable component receives the canonical opt-in field list and
        names the group's members."""
        members = ["light.member_one", "light.member_two"]
        hass.states.async_set(
            "light.kitchen", "on", {"friendly_name": "Kitchen", "entity_id": members}
        )

        with routes(client, socket) as spies:
            resp = await _search(
                client,
                query="kitchen",
                domain_filter="light",
                result_fields=["member_entity_ids", "entity_id", "is_group"],
            )

        assert resp["entities"][0] == {
            "entity_id": "light.kitchen",
            "is_group": True,
            "member_entity_ids": members,
        }
        (request,) = frames(spies.component, SEARCH)
        assert request["result_fields"] == ["is_group", "member_entity_ids"]
        spies.states.assert_not_called()

    async def test_component_path_retains_requested_enrichment(
        self, client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """Requested enrichment keys survive the projection, with no legacy
        enrichment fetch."""
        with routes(client, socket) as spies:
            resp = await _search(
                client,
                query="kitchen",
                domain_filter="light",
                result_fields=["entity_id", "area", "floor"],
            )

        assert resp["entities"][0] == {
            "entity_id": "light.kitchen",
            "area": "Kitchen",
            "floor": "Main",
        }
        spies.bridge.assert_not_called()

    async def test_unknown_result_field_rejected(
        self, client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """An unknown result_fields name is a hard validation error, rejected
        before any backend was consulted."""
        with routes(client, socket) as spies, pytest.raises(ToolError) as excinfo:
            await _search(client, query="kitchen", result_fields=["frobnicate"])

        assert "Unknown result_fields" in str(excinfo.value)
        spies.component.assert_not_called()
        spies.states.assert_not_called()

    async def test_component_supported_search_types_still_route_component(
        self, client: HomeAssistantClient, socket: HomeAssistantWebSocketClient
    ) -> None:
        """A search_types pin without 'dashboard' keeps the fast path, and the
        pinned list reaches the component verbatim (config-only: the explicit
        pin drops the entity surface)."""
        with routes(client, socket) as spies:
            await _search(client, query="kitchen", search_types=["automation"])

        (request,) = frames(spies.component, SEARCH)
        assert request["search_types"] == ["automation"]
        spies.states.assert_not_called()


@pytest.mark.usefixtures("component")
class TestFallbacks:
    async def test_unknown_command_falls_back_silently(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A component downgraded since the probe no longer registers the
        command; Core answers ``unknown_command`` → legacy, no warning."""
        monkeypatch.delitem(hass.data["websocket_api"], SEARCH)

        with routes(client, socket) as spies:
            resp = await _search(client, query="kitchen")

        spies.states.assert_awaited_once()
        assert bridge_types(spies.bridge).count("config/entity_registry/list") == 1
        assert not fell_back(resp)

    async def test_command_timeout_falls_back_with_warning(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The component search answering too late → legacy + a warning, not
        an aborted call. A command error other than ``unknown_command`` takes
        the same branch."""
        async with held(hass, SEARCH, monkeypatch, client=client):
            with routes(client, socket) as spies:
                resp = await _search(client, query="kitchen")

        spies.states.assert_awaited_once()
        assert fell_back(resp)

    async def test_connection_drop_falls_back_with_warning(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """A dropped connection → legacy + a warning, not a propagated error:
        the legacy search reads ``/api/states`` over REST and the registry
        bridge, which reconnects."""
        drop_connection_on(hass, SEARCH, socket)

        with routes(client, socket) as spies:
            resp = await _search(client, query="kitchen")

        spies.states.assert_awaited_once()
        assert "light.kitchen" in _ids(resp)
        assert fell_back(resp)


async def test_component_diagnostics_mark_partial(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The component naming config domains it could not read (here automation,
    script and scene, never set up) → partial, with the reason mirrored onto
    the warnings channel agents read."""
    _kitchen(hass)

    resp = await _search(component_core_client, query="kitchen")

    assert resp["partial"] is True
    assert "config components inaccessible" in resp["partial_reason"]
    assert any("config components inaccessible" in w for w in resp["warnings"])


class TestOlderComponent:
    """A component without a capability keeps the legacy path for what it
    cannot serve."""

    async def test_pre_child_semantics_search_capability_uses_legacy(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A pre-fix component's search cannot carry child-device semantics."""
        older_component(monkeypatch, "device_registry_child_semantics")

        with routes(client, socket) as spies:
            await _search(client, query="kitchen")

        spies.states.assert_awaited_once()
        assert frames(spies.component, SEARCH) == []

    async def test_membership_request_falls_back_from_old_component(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        older_component(monkeypatch, "search_entity_membership")

        with routes(client, socket) as spies:
            resp = await _search(
                client, query="kitchen", result_fields=["entity_id", "is_group"]
            )

        assert sorted(resp["entities"], key=lambda e: e["entity_id"]) == [
            {"entity_id": "light.kitchen", "is_group": False},
            {"entity_id": "sensor.kitchen_temp", "is_group": False},
        ]
        spies.states.assert_awaited_once()
        assert frames(spies.component, SEARCH) == []

    @pytest.mark.parametrize(
        ("arguments", "mode"),
        [
            ({"domain_filter": "light"}, "domain_listing"),
            ({"query": "   ", "domain_filter": "light"}, "domain_listing"),
            ({"area_filter": "Kitchen"}, "area_only"),
            ({"query": "kitchen", "area_filter": "Kitchen"}, "area_filtered_query"),
            ({"state_filter": "on"}, "state_listing"),
        ],
        ids=[
            "empty-query-domain",
            "whitespace-query-domain",
            "area-only",
            "query-and-area",
            "state-only",
        ],
    )
    async def test_listing_modes_bypass_a_component_without_unified_search(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
        arguments: dict[str, Any],
        mode: str,
    ) -> None:
        older_component(monkeypatch, "search_unified")

        with routes(client, socket) as spies:
            data = await _search(client, **arguments)

        assert data.get("search_type") == mode, data
        assert frames(spies.component, SEARCH) == []

    async def test_floor_filter_expands_from_public_ha_search(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The public tool expands an exact floor (by its alias) to all of its
        areas."""
        older_component(monkeypatch, "search_unified")
        basement = fr.async_get(hass).async_create("Sous-sol", aliases={"Basement"})
        cave = ar.async_get(hass).async_create("Cave", floor_id=basement.floor_id)
        er.async_get(hass).async_update_entity("light.kitchen", area_id=cave.id)

        with routes(client, socket) as spies:
            data = await _search(client, area_filter="Sous-sol", domain_filter="light")

        assert data["area_names"] == ["Cave"]
        assert _ids(data) == ["light.kitchen"]
        assert any("expanded to 1 area(s)" in w for w in data["warnings"])
        assert frames(spies.component, SEARCH) == []

    async def test_active_deny_filter_bypasses_a_component_without_visibility(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A component that cannot apply the visibility filter must not serve a
        search while one is active, or a denied entity reappears in ha_search."""
        older_component(
            monkeypatch,
            "search_visibility",
            "search_visibility_allowlist_authorization",
        )
        save_visibility_config(
            tmp_path,
            VisibilityConfig(
                enabled=True, exclude_categories=[], deny_entity_ids=["light.kitchen"]
            ),
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        with routes(client, socket) as spies:
            data = await _search(client, query="kitchen")

        assert frames(spies.component, SEARCH) == []
        spies.states.assert_awaited_once()
        assert "light.kitchen" not in _ids(data)
        assert "sensor.kitchen_temp" in _ids(data)

    async def test_enabled_but_no_active_dimension_still_uses_component(
        self,
        client: HomeAssistantClient,
        component: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """enabled=True with every dimension cleared hides nothing, so even a
        component that cannot filter still serves."""
        older_component(
            monkeypatch,
            "search_visibility",
            "search_visibility_allowlist_authorization",
        )
        save_visibility_config(
            tmp_path, VisibilityConfig(enabled=True, exclude_categories=[])
        )
        monkeypatch.setattr(resolver, "get_data_dir", lambda: tmp_path)

        with routes(client, socket) as spies:
            await _search(client, query="kitchen")

        assert frames(spies.component, SEARCH)
        spies.states.assert_not_called()


class TestLegacyEnrichment:
    """``result_fields`` enrichment on the legacy path (no component)."""

    async def test_legacy_path_joins_enrichment(
        self, client: HomeAssistantClient
    ) -> None:
        """The registry join fills the requested enrichment fields."""
        with routes(client, await pooled(client)) as spies:
            resp = await _search(
                client,
                query="kitchen",
                result_fields=["entity_id", "area", "floor", "labels", "aliases"],
            )

        light = next(r for r in resp["entities"] if r["entity_id"] == "light.kitchen")
        assert light["area"] == "Kitchen"
        assert light["floor"] == "Main"
        assert light["labels"] == ["Favorites"]
        assert "lamp" in light["aliases"]
        sent = bridge_types(spies.bridge)
        assert sent.count("config/entity_registry/get_entries") == 1
        assert sent.count("config/area_registry/list") == 1

    async def test_legacy_default_skips_enrichment_fetch(
        self, client: HomeAssistantClient
    ) -> None:
        """No result_fields → the legacy path issues no enrichment registry reads."""
        with routes(client, await pooled(client)) as spies:
            await _search(client, query="kitchen")

        sent = bridge_types(spies.bridge)
        assert "config/entity_registry/get_entries" not in sent
        assert "config/area_registry/list" not in sent

    async def test_area_query_enrichment_reuses_haystack_entries(
        self, client: HomeAssistantClient
    ) -> None:
        """Area+query mode threads the alias haystack's ``get_entries`` into the
        ``result_fields`` enrichment join, so the whole flow costs exactly one
        ``config/entity_registry/get_entries`` for overlapping ids."""
        with routes(client, await pooled(client)) as spies:
            resp = await _search(
                client,
                query="kitchen",
                area_filter="Kitchen",
                result_fields=["entity_id", "area", "aliases"],
            )

        light = next(r for r in resp["entities"] if r["entity_id"] == "light.kitchen")
        assert light["area"] == "Kitchen"
        assert "lamp" in light["aliases"]
        assert (
            bridge_types(spies.bridge).count("config/entity_registry/get_entries") == 1
        )


async def test_component_and_legacy_response_shape_parity(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    second_client: HomeAssistantClient,
) -> None:
    """A body-skipped entity query (query + domain_filter) resolves to the same
    envelope on both serving paths: key set, pagination axis, partial semantics
    and the body-skip warning."""
    # Probed before the component is registered: this client stays legacy.
    legacy = await _search(client, query="kitchen", domain_filter="light")
    component_ws_api.async_register_commands(hass)
    socket = await pooled(second_client)

    with routes(second_client, socket) as spies:
        component = await _search(second_client, query="kitchen", domain_filter="light")

    assert frames(spies.component, SEARCH)
    assert set(component) == set(legacy)
    assert component["partial"] == legacy["partial"] is False
    assert component["entity_total_matches"] == legacy["entity_total_matches"] == 1
    assert component["count"] == legacy["count"] == 1
    assert component["has_more"] == legacy["has_more"] is False
    assert component["next_offset"] == legacy["next_offset"]
    skip = "config-body search skipped"
    assert any(skip in w for w in component["warnings"])
    assert any(skip in w for w in legacy["warnings"])
