"""``ha_search`` with ``dashboard`` in ``search_types``: the split route and its gates.

Naming ``dashboard`` in ``search_types`` used to send the WHOLE call to the
legacy path, so the automation/script/scene/helper surfaces lost the
component's in-process scan and went back to one REST fetch per config —
~94 seconds and ``partial`` on a 136-automation instance versus 62ms complete
for the same call without ``dashboard`` (issue #2289).

These tests pin the split: the component's ``search`` command serves the
surfaces it has (``dashboard`` never reaches it: its schema has no such value,
issue #2008), the dashboards leg serves that bucket (the component's
``ha_mcp_tools/dashboards`` doc search, else the legacy per-dashboard walk),
and the server merges the two into one score-sorted page. The corner cases
that send the call to the legacy path whole, and the leg's partial semantics,
are pinned too.

Everything runs on a real ``hass``: automations, scripts and scenes are
Core's, dashboards are Lovelace's storage and YAML dashboards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from custom_components.ha_mcp_tools.websocket_api import system as component_system
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.smart_search import SmartSearchTools

from ._component_frames import frames, older_component, pooled
from ._search_routing import DASHBOARDS, SEARCH, bridge_types, fell_back, routes
from ._search_tool import _build_ha_search, _setup_visibility_disabled

_KITCHEN_VIEW = {"views": [{"title": "Kitchen"}]}
_MIXED = ["automation", "dashboard"]


@pytest.fixture(autouse=True)
def _visibility_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_visibility_disabled(tmp_path, monkeypatch)


@pytest.fixture
def client(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> HomeAssistantClient:
    """The server's client on a ``hass`` that answers ``ha_mcp_tools/*``."""
    return component_core_client


@pytest.fixture
async def socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled(client)


async def _lovelace(hass: HomeAssistant, yaml: dict[str, str] | None = None) -> None:
    """Set Lovelace up; ``yaml`` maps a YAML dashboard's url_path to its file text."""
    dashboards: dict[str, Any] = {}
    for url_path, text in (yaml or {}).items():
        filename = f"{url_path}.yaml"
        await hass.async_add_executor_job(
            Path(hass.config.path(filename)).write_text, text
        )
        dashboards[url_path] = {"mode": "yaml", "filename": filename, "title": "Yaml"}
    config = {"lovelace": {"dashboards": dashboards}} if dashboards else {}
    assert await async_setup_component(hass, "lovelace", config)


async def _storage_dashboard(
    client: HomeAssistantClient, url_path: str, config: dict[str, Any] | None
) -> str:
    """Create a storage dashboard (with ``config`` saved, if given); returns its id."""
    created = await client.send_websocket_message(
        {"type": "lovelace/dashboards/create", "url_path": url_path, "title": "Mine"}
    )
    assert created["success"], created
    if config is not None:
        saved = await client.send_websocket_message(
            {"type": "lovelace/config/save", "url_path": url_path, "config": config}
        )
        assert saved["success"], saved
    return str(created["result"]["id"])


async def _automations(hass: HomeAssistant, *aliases: str) -> None:
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": f"a{i}",
                    "alias": alias,
                    "triggers": [{"trigger": "event", "event_type": "test"}],
                    "actions": [],
                }
                for i, alias in enumerate(aliases)
            ]
        },
    )


async def _search(client: HomeAssistantClient, **arguments: Any) -> dict[str, Any]:
    response: dict[str, Any] = await _build_ha_search(client)(
        query="kitchen", **arguments
    )
    assert response["success"] is True
    return response


def _page(response: dict[str, Any]) -> list[str]:
    """Every record on the page: config entity_ids and dashboard url_paths."""
    ids = [
        r["entity_id"]
        for bucket in ("automations", "scripts", "scenes", "helpers")
        for r in response.get(bucket, [])
    ]
    return ids + [d["url_path"] for d in response.get("dashboards", [])]


def _lovelace_reads(spies: Any) -> list[str]:
    return [t for t in bridge_types(spies.bridge) if t.startswith("lovelace/")]


class TestDashboardSplitRoute:
    """A mixed ``search_types`` keeps the component fast path for its surfaces."""

    async def test_mixed_search_types_merge_both_legs(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """The #2289 regression: one component search frame, one dashboards
        frame, both buckets filled, and none of the legacy fetches the
        all-or-nothing route used to force."""
        await _automations(hass, "Kitchen lights")
        await _lovelace(hass)
        await _storage_dashboard(client, "kitchen-view", _KITCHEN_VIEW)

        with routes(client, socket) as spies:
            resp = await _search(client, search_types=_MIXED)

        assert [a["entity_id"] for a in resp["automations"]] == [
            "automation.kitchen_lights"
        ]
        assert [d["url_path"] for d in resp["dashboards"]] == ["kitchen-view"]
        assert resp["config_total_matches"] == resp["count"] == 2
        assert resp["partial"] is False
        assert len(resp["warnings"]) == 1
        assert "entity search skipped" in resp["warnings"][0]
        assert len(frames(spies.component, SEARCH)) == 1
        assert len(frames(spies.component, DASHBOARDS)) == 1
        spies.states.assert_not_called()
        assert _lovelace_reads(spies) == []

    async def test_dashboard_stripped_from_the_component_request(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """The component's schema has no ``dashboard`` value, so the surface
        must never reach it (issue #2008's original failure)."""
        await _automations(hass)
        await _lovelace(hass)

        with routes(client, socket) as spies:
            await _search(client, search_types=["automation", "helper", "dashboard"])

        (request,) = frames(spies.component, SEARCH)
        assert request["search_types"] == ["automation", "helper"]

    async def test_dashboard_only_request_is_served_by_the_dashboards_frame(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """``search_types=["dashboard"]`` leaves the search command no surface
        of its own (an explicit pin drops the entity surface), so it never
        runs, silently; the dashboard search is ONE ``ha_mcp_tools/dashboards``
        frame with zero legacy ``lovelace/*`` round-trips."""
        await _lovelace(hass)
        await _storage_dashboard(client, "kitchen-view", _KITCHEN_VIEW)

        with routes(client, socket) as spies:
            resp = await _search(client, search_types=["dashboard"])

        assert [d["url_path"] for d in resp["dashboards"]] == ["kitchen-view"]
        assert not resp.get("partial")
        assert not fell_back(resp)
        assert len(resp["warnings"]) == 1
        assert "entity search skipped" in resp["warnings"][0]
        assert frames(spies.component, SEARCH) == []
        assert [f.get("mode") for f in frames(spies.component, DASHBOARDS)] == [
            "search"
        ]
        assert _lovelace_reads(spies) == []


class TestComponentWithoutDocSearch:
    """A component without ``dashboards_doc_search``: the legacy walk serves
    the dashboard bucket."""

    async def test_dashboard_only_search_walks_cleanly_past_an_auto_generated_dashboard(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The issue #2008 repro through the real tool: an auto-generated
        (never taken control of) dashboard answers ``config_not_found``, which
        must not flag the envelope partial; the route stays silent."""
        older_component(monkeypatch, "dashboards_doc_search")
        await _lovelace(hass)
        await _storage_dashboard(client, "auto-gen", None)

        with routes(client, socket) as spies:
            resp = await _search(client, search_types=["dashboard"])

        assert not resp.get("partial"), resp.get("partial_reason")
        assert not fell_back(resp)
        assert len(resp["warnings"]) == 1
        assert "entity search skipped" in resp["warnings"][0]
        assert frames(spies.component, SEARCH) == []
        # The walk read both dashboards: the auto-generated one and the default.
        assert _lovelace_reads(spies).count("lovelace/config") == 2

    async def test_mixed_search_types_keep_the_component_fast_path(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The component search still serves the surfaces it has, and the
        dashboard bucket comes from the legacy walk. Sending the whole call to
        legacy instead cost every such search the per-config REST fetches."""
        older_component(monkeypatch, "dashboards_doc_search")
        await _automations(hass)
        await _lovelace(hass)

        with routes(client, socket) as spies:
            resp = await _search(client, search_types=_MIXED)

        (request,) = frames(spies.component, SEARCH)
        assert request["search_types"] == ["automation"]
        spies.states.assert_not_called()
        # The legacy walk read the default dashboard's config.
        assert "lovelace/config" in _lovelace_reads(spies)
        assert resp["dashboards"] == []
        assert not resp.get("partial")
        assert not fell_back(resp)


class TestComponentWithoutUnifiedSearch:
    """An older component's ``limits.max_results`` caps the window fetch the
    merge needs; a window past it takes the legacy path whole."""

    @pytest.mark.parametrize(
        ("max_results", "offset", "limit"),
        [(None, 460, 50), (3, 0, 4)],
        ids=["past-the-default-ceiling", "past-an-advertised-smaller-ceiling"],
    )
    async def test_window_beyond_the_ceiling_stays_legacy(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
        max_results: int | None,
        offset: int,
        limit: int,
    ) -> None:
        """Past the ceiling the component's schema would reject the window
        frame outright; the gate reads the component's own limit."""
        older_component(monkeypatch, "search_unified")
        if max_results is not None:
            monkeypatch.setattr(
                component_system,
                "LIMITS",
                {**component_system.LIMITS, "max_results": max_results},
            )
        await _automations(hass)
        await _lovelace(hass)

        with routes(client, socket) as spies:
            resp = await _search(
                client, search_types=_MIXED, offset=offset, limit=limit
            )

        assert frames(spies.component, SEARCH) == []
        assert not fell_back(resp)
        spies.states.assert_awaited_once()

    async def test_window_at_the_ceiling_still_merges(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """500 is the inclusive maximum the component's schema accepts."""
        older_component(monkeypatch, "search_unified")
        await _automations(hass)
        await _lovelace(hass)

        with routes(client, socket) as spies:
            await _search(client, search_types=_MIXED, offset=450, limit=50)

        (request,) = frames(spies.component, SEARCH)
        assert request["limit"] == 500


class TestDashboardSplitPagination:
    """The merged page is one global score sort across both legs."""

    async def test_pages_are_cut_from_one_window_and_stay_disjoint(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """With ``offset>0`` the component is asked for the whole
        ``[0, offset+limit)`` prefix, and the server pages the merged list:
        two pages together are exactly the first four records, once each."""
        await _automations(hass, "Kitchen one", "Kitchen two", "Kitchen three")
        await _lovelace(hass)
        await _storage_dashboard(client, "kitchen-view", _KITCHEN_VIEW)

        full = await _search(client, search_types=_MIXED, limit=4)
        first = await _search(client, search_types=_MIXED, limit=2)
        with routes(client, socket) as spies:
            second = await _search(client, search_types=_MIXED, offset=2, limit=2)

        (request,) = frames(spies.component, SEARCH)
        assert (request["offset"], request["limit"]) == (0, 4)
        assert not set(_page(first)) & set(_page(second))
        assert sorted(_page(first) + _page(second)) == sorted(_page(full))
        assert second["count"] == 2
        assert second["config_total_matches"] == 4
        assert second["config_has_more"] is False
        assert second["config_next_offset"] is None

    async def test_equal_score_pages_follow_the_component_sort_key(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """Pages are cut with the total order that picked the window. The
        component orders its corpus by ``(-score, sort key)``; on a tie
        "scene.kitchen" sorts before "script.kitchen" although the scenes
        bucket comes after scripts, so a bucket-order cut would show the scene
        on both pages and never the script."""
        assert await async_setup_component(
            hass,
            "script",
            {"script": {"kitchen": {"alias": "Kitchen", "sequence": []}}},
        )
        assert await async_setup_component(
            hass, "scene", {"scene": [{"name": "Kitchen", "entities": {}}]}
        )
        await _lovelace(hass)
        types = ["script", "scene", "dashboard"]

        both = await _search(client, search_types=types, limit=2)
        first = await _search(client, search_types=types, limit=1)
        second = await _search(client, search_types=types, offset=1, limit=1)

        (scene,) = both["scenes"]
        (script,) = both["scripts"]
        assert scene["score"] == script["score"]
        assert _page(first) == ["scene.kitchen"]
        assert _page(second) == ["script.kitchen"]
        assert first["config_has_more"] is True
        assert first["config_next_offset"] == 1
        assert first["has_more"] is True

    async def test_component_corpus_past_the_window_reports_has_more(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """The component's own ``config_has_more`` for the window is OR-ed in:
        its records beyond the window are not in the merged list at all."""
        await _automations(hass, *(f"Kitchen {i}" for i in range(6)))
        await _lovelace(hass)

        resp = await _search(client, search_types=_MIXED, limit=5)

        assert resp["config_total_matches"] == 6
        assert resp["config_has_more"] is True
        assert resp["config_next_offset"] == 5


class TestDashboardSplitIncludeConfig:
    """``include_config`` governs the dashboard records' bodies too."""

    async def test_include_config_false_strips_the_body(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """A YAML dashboard sends the leg to the legacy walk (the component's
        doc search never reads YAML), which carries bodies; without
        ``include_config`` the record drops its body."""
        await _automations(hass)
        await _lovelace(hass, {"kitchen-yaml": "views:\n  - title: Kitchen\n"})

        resp = await _search(client, search_types=_MIXED, include_config=False)

        assert [d["url_path"] for d in resp["dashboards"]] == ["kitchen-yaml"]
        assert "config" not in resp["dashboards"][0]

    async def test_include_config_true_keeps_the_body(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """``include_config=True`` forces the legacy dashboard walk (the doc
        search carries no bodies), which is where the body comes from."""
        await _automations(hass)
        await _lovelace(hass)
        await _storage_dashboard(client, "kitchen-view", _KITCHEN_VIEW)

        resp = await _search(client, search_types=_MIXED, include_config=True)

        (record,) = resp["dashboards"]
        assert record["config"] == _KITCHEN_VIEW


class TestDashboardSplitPartialSemantics:
    """The dashboard bucket is never dropped silently."""

    async def test_dashboard_leg_failure_marks_partial(
        self, hass: HomeAssistant, client: HomeAssistantClient
    ) -> None:
        """A raising dashboards leg reports like a failed legacy branch:
        ``partial``, an ``errors[]`` entry naming the surface, the warning
        mirror, and the component's buckets intact."""
        await _automations(hass, "Kitchen lights")
        await _lovelace(hass)

        with patch.object(
            SmartSearchTools,
            "_search_dashboards_surface",
            AsyncMock(side_effect=RuntimeError("dashboards backend exploded")),
        ):
            resp = await _search(client, search_types=_MIXED)

        assert [a["entity_id"] for a in resp["automations"]] == [
            "automation.kitchen_lights"
        ]
        assert resp["dashboards"] == []
        assert resp["partial"] is True
        assert {e["surface"] for e in resp["errors"]} == {"dashboards"}
        assert "dashboards backend exploded" in resp["partial_reason"]
        assert any("incomplete results" in w for w in resp["warnings"])

    async def test_unloadable_storage_dashboards_mark_partial(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        hass_storage: dict[str, Any],
    ) -> None:
        """Storage dashboards whose stored config Core cannot load are counted
        by the component and reported with the deep path's own ``not
        scanned`` wording, not as no matches."""
        await _automations(hass)
        await _lovelace(hass)
        for url_path in ("broken-one", "broken-two"):
            dashboard_id = await _storage_dashboard(client, url_path, None)
            key = f"lovelace.{dashboard_id}"
            hass_storage[key] = {"version": 1, "key": key, "data": {"views": []}}

        resp = await _search(client, search_types=_MIXED)

        assert resp["partial"] is True
        assert "2 dashboard(s) not scanned" in resp["partial_reason"]
        assert any("2 dashboard(s) not scanned" in w for w in resp["warnings"])

    async def test_legacy_walk_failure_marks_partial(
        self,
        hass: HomeAssistant,
        client: HomeAssistantClient,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """The same count reaches the envelope from the legacy walk: a YAML
        dashboard sends the leg there, and its unparsable file is the one
        config read that fails."""
        await _automations(hass)
        await _lovelace(hass, {"broken-yaml": "views: [\n"})

        with routes(client, socket) as spies:
            resp = await _search(client, search_types=_MIXED)

        assert "lovelace/config" in _lovelace_reads(spies)
        assert resp["partial"] is True
        assert "1 dashboard(s) not scanned" in resp["partial_reason"]
