"""``ha_search`` split route: component ``search`` + dashboards leg, merged.

Naming ``dashboard`` in ``search_types`` used to send the WHOLE call to the
legacy path, so the automation/script/scene/helper surfaces lost the
component's in-process scan and went back to one REST fetch per config —
~94 seconds and ``partial`` on a 136-automation instance versus 62ms complete
for the same call without ``dashboard`` (issue #2289).

These tests pin the split: the component's ``search`` command serves the
surfaces it has, the component's separate ``ha_mcp_tools/dashboards``
doc-search frame serves the dashboard bucket, and the server merges the two
into one score-sorted page. The corner cases (a request the component search
could not serve at all, a page deeper than the component's ``limit`` ceiling,
either leg failing) are pinned here too, because each of them decides between
a merged response and the legacy path.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ha_mcp.client.rest_client import HomeAssistantCommandError
from ha_mcp.tools import tools_config_dashboards, tools_search
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import _merge_dashboard_window, _ResolvedSearch

from ._component_routing_helpers import patch_ws
from .test_ha_search_component_routing import (
    DashboardRoutingClient,
    RoutingClient,
    _build_ha_search,
    _setup_visibility_disabled,
)

# A component that advertises the search command AND the dashboards doc-search
# frame — the install the split targets.
_CAPS_SPLIT = {
    "schema_version": 1,
    "component_version": "2.0.0",
    "capabilities": ["search", "dashboards", "dashboards_doc_search"],
    "limits": {"max_results": 500},
}


class MatchingDashboardClient(RoutingClient):
    """A legacy dashboard walk whose one (default) dashboard matches."""

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Serve the lovelace list/config reads the legacy walk makes."""
        msg_type = msg.get("type", "")
        if msg_type == "lovelace/dashboards/list":
            self.ws_types[msg_type] += 1
            return {"success": True, "result": []}
        if msg_type == "lovelace/config":
            self.ws_types[msg_type] += 1
            return {"success": True, "result": {"views": [{"title": "Kitchen"}]}}
        return await super().send_websocket_message(msg)


class FailingDashboardClient(RoutingClient):
    """A legacy dashboard walk whose per-dashboard config read fails."""

    async def send_websocket_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """List cleanly, then fail the config read of the one dashboard."""
        msg_type = msg.get("type", "")
        if msg_type == "lovelace/dashboards/list":
            self.ws_types[msg_type] += 1
            return {"success": True, "result": []}
        if msg_type == "lovelace/config":
            self.ws_types[msg_type] += 1
            return {"success": False, "error": "Command failed: boom"}
        return await super().send_websocket_message(msg)


def _automation_record(name: str, score: int) -> dict[str, Any]:
    """One component ``automations`` bucket record."""
    return {
        "entity_id": f"automation.{name}",
        "alias": name.replace("_", " ").title(),
        "score": score,
        "match_in_name": False,
        "match_in_config": True,
    }


def _search_result(
    automations: list[dict[str, Any]] | None = None,
    *,
    config_total_matches: int | None = None,
    config_has_more: bool = False,
) -> dict[str, Any]:
    """A component ``search`` result carrying only config buckets.

    Mirrors the component, which emits all four config bucket keys plus the
    corpus-wide ``config_total_matches`` regardless of which surfaces the
    request named.
    """
    records = automations if automations is not None else []
    return {
        "entities": [],
        "entity_total_matches": 0,
        "entity_has_more": False,
        "automations": records,
        "scripts": [],
        "scenes": [],
        "helpers": [],
        "config_total_matches": (
            len(records) if config_total_matches is None else config_total_matches
        ),
        "config_has_more": config_has_more,
        "partial": False,
    }


def _dashboards_result(
    document_matches: list[dict[str, Any]],
    *,
    load_failed: int = 0,
    yaml_skipped: int = 0,
) -> dict[str, Any]:
    """A component ``dashboards`` search-mode result."""
    return {
        "mode": "search",
        "available": True,
        "matches": [],
        "truncated": False,
        "document_matches": document_matches,
        "yaml_skipped": yaml_skipped,
        "load_failed": load_failed,
    }


def _split_ws(
    *,
    caps: dict[str, Any] | None = None,
    search_result: dict[str, Any] | None = None,
    search_exc: Exception | None = None,
    dashboards_result: dict[str, Any] | None = None,
) -> AsyncMock:
    """A WS mock serving the caps probe, the search frame and the dashboards frame.

    ``make_ws`` dispatches one read command; the split issues two, so this
    suite carries its own dispatcher. The dashboards ``list`` mode (which the
    legacy walk's registry read funnels through) answers with an empty registry
    so the walk covers the default dashboard alone. Any other command type is
    an ``AssertionError`` so a stray frame fails loudly.
    """

    async def _send(command_type: str, **kwargs: Any) -> dict[str, Any]:
        if command_type == "ha_mcp_tools/info":
            return {"success": True, "result": caps or _CAPS_SPLIT}
        if command_type == "ha_mcp_tools/search":
            if search_exc is not None:
                raise search_exc
            return {"success": True, "result": search_result}
        if command_type == "ha_mcp_tools/dashboards":
            if kwargs.get("mode") == "list":
                return {
                    "success": True,
                    "result": {"mode": "list", "available": True, "dashboards": []},
                }
            return {"success": True, "result": dashboards_result}
        raise AssertionError(f"unexpected command {command_type!r}")

    ws = AsyncMock()
    ws.send_command = AsyncMock(side_effect=_send)
    return ws


def _patch_dashboards_ws(ws: AsyncMock) -> Any:
    """Resolve the dashboards module's own WS binding to ``ws`` as well."""
    return patch.object(
        tools_config_dashboards, "get_websocket_client", AsyncMock(return_value=ws)
    )


def _sent(ws: AsyncMock, command: str) -> list[Any]:
    """Every call the mock recorded for ``command``."""
    return [c for c in ws.send_command.call_args_list if c.args[0] == command]


def _resolved_split(
    *,
    offset: int = 0,
    limit: int = 10,
    registry_eligible: bool = False,
    include_config: bool = False,
) -> _ResolvedSearch:
    """A resolved mixed-surface search, for driving the merge helper directly."""
    return _ResolvedSearch(
        query="kitchen",
        query_text="kitchen",
        domain_filter=None,
        area_filter=None,
        state_filter=None,
        parsed_search_types=["automation", "dashboard"],
        parsed_fields=None,
        result_fields=None,
        limit=limit,
        offset=offset,
        exact_match=True,
        include_hidden=True,
        include_config=include_config,
        group_by_domain=False,
        per_domain_limit=None,
        config_time_budget=None,
        registry_eligible=registry_eligible,
        body_eligible=True,
        body_skipped_by_intent_gate=False,
    )


class TestMergeWindowHelper:
    """The window merge, driven directly — including a case the tool cannot reach."""

    def test_entity_records_are_resliced_onto_the_page(self) -> None:
        """The window fetch asks for ``[0, offset + limit)`` on EVERY surface
        the request names, so an entity surface would arrive over-fetched.

        No live call reaches this: the split needs an explicit ``search_types``,
        which pins the call config-only, so the window carries no entities. The
        re-slice is the guard for an eligibility change that admits one —
        without it the entity page would be the whole prefix.
        """
        req = _resolved_split(offset=2, limit=2, registry_eligible=True)
        component_result = {
            "entities": [{"entity_id": f"light.e{i}"} for i in range(4)],
            "entity_total_matches": 9,
            "entity_has_more": True,
            "config_total_matches": 0,
        }

        windowed, dashboards = _merge_dashboard_window(req, component_result, [])

        assert [e["entity_id"] for e in windowed["entities"]] == [
            "light.e2",
            "light.e3",
        ]
        assert windowed["entity_total_matches"] == 9
        assert windowed["entity_has_more"] is True
        assert dashboards == []

    def test_entity_has_more_false_on_the_last_page(self) -> None:
        """``entity_has_more`` is recomputed from the corpus total, so the tail
        page does not inherit the window fetch's own ``has_more``."""
        req = _resolved_split(offset=2, limit=5, registry_eligible=True)
        component_result = {
            "entities": [{"entity_id": f"light.e{i}"} for i in range(3)],
            "entity_total_matches": 3,
            "entity_has_more": True,
            "config_total_matches": 0,
        }

        windowed, _ = _merge_dashboard_window(req, component_result, [])

        assert [e["entity_id"] for e in windowed["entities"]] == ["light.e2"]
        assert windowed["entity_has_more"] is False

    def test_buckets_absent_from_the_component_result_stay_absent(self) -> None:
        """Only the buckets the component actually returned are rewritten — an
        untouched key must not appear as an empty list it never had."""
        req = _resolved_split()
        component_result = {
            "automations": [{"entity_id": "automation.one", "score": 100}],
            "config_total_matches": 1,
        }

        windowed, dashboards = _merge_dashboard_window(
            req, component_result, [{"dashboard_url": "energy", "score": 90}]
        )

        assert "scripts" not in windowed
        assert "scenes" not in windowed
        assert "helpers" not in windowed
        # The dashboards bucket is the leg's, never the component's.
        assert "dashboards" not in windowed
        assert [d["dashboard_url"] for d in dashboards] == ["energy"]
        assert windowed["config_total_matches"] == 2


class TestDashboardSplitRoute:
    """A mixed ``search_types`` keeps the component fast path for its surfaces."""

    @pytest.mark.asyncio
    async def test_mixed_search_types_merge_both_legs(
        self, tmp_path, monkeypatch
    ) -> None:
        """The #2289 regression: one component search frame, one dashboards
        frame, both buckets filled, and none of the legacy fetches the
        all-or-nothing route used to force."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result([_automation_record("kitchen_lights", 100)]),
            dashboards_result=_dashboards_result(
                [{"url_path": "energy", "title": "Energy"}]
            ),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert [a["entity_id"] for a in resp["automations"]] == [
            "automation.kitchen_lights"
        ]
        assert [d["dashboard_url"] for d in resp["dashboards"]] == ["energy"]
        assert resp["config_total_matches"] == 2
        assert resp["count"] == 2
        assert resp["partial"] is False
        assert resp["warnings"] == []
        # Exactly one frame per leg, and the legacy inventory is untouched.
        assert len(_sent(ws, "ha_mcp_tools/search")) == 1
        assert len(_sent(ws, "ha_mcp_tools/dashboards")) == 1
        assert client.get_states_calls == 0
        assert client.ws_types.get("lovelace/dashboards/list", 0) == 0
        assert client.ws_types.get("lovelace/config", 0) == 0

    @pytest.mark.asyncio
    async def test_dashboard_stripped_from_the_component_request(
        self, tmp_path, monkeypatch
    ) -> None:
        """The component's voluptuous allowlist has no ``dashboard`` value, so
        the surface must never reach it (issue #2008's original failure)."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            await ha_search(
                query="kitchen", search_types=["automation", "helper", "dashboard"]
            )

        request = _sent(ws, "ha_mcp_tools/search")[0].kwargs
        assert request["search_types"] == ["automation", "helper"]

    @pytest.mark.asyncio
    async def test_dashboard_only_request_stays_legacy(
        self, tmp_path, monkeypatch
    ) -> None:
        """``search_types=["dashboard"]`` leaves the component search request
        with no surface of its own (an explicit pin drops the entity surface),
        so the whole call goes to the legacy path — silently, as before."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(dashboards_result=_dashboards_result([]))
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(query="kitchen", search_types=["dashboard"])

        assert resp["success"] is True
        assert _sent(ws, "ha_mcp_tools/search") == []
        assert not any("served via legacy path" in w for w in resp.get("warnings", []))

    @pytest.mark.asyncio
    async def test_window_beyond_component_limit_ceiling_stays_legacy(
        self, tmp_path, monkeypatch
    ) -> None:
        """The window fetch asks for ``offset + limit`` records; past the
        component's 500-record ``limit`` ceiling that frame would be rejected
        outright, so the call takes the legacy path whole instead."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen",
                search_types=["automation", "dashboard"],
                offset=460,
                limit=50,
            )

        assert resp["success"] is True
        assert _sent(ws, "ha_mcp_tools/search") == []
        assert not any("served via legacy path" in w for w in resp.get("warnings", []))
        # The legacy pipeline served it.
        assert client.get_states_calls == 1

    @pytest.mark.asyncio
    async def test_component_advertised_smaller_ceiling_gates_the_split(
        self, tmp_path, monkeypatch
    ) -> None:
        """The gate reads the component's own ``limits.max_results``: a
        component advertising a smaller ceiling routes legacy instead of
        accepting the split and having its schema reject the window frame."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            caps={**_CAPS_SPLIT, "limits": {"max_results": 3}},
            search_result=_search_result(),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen",
                search_types=["automation", "dashboard"],
                limit=4,
            )

        assert resp["success"] is True
        assert _sent(ws, "ha_mcp_tools/search") == []
        assert not any("served via legacy path" in w for w in resp.get("warnings", []))
        # The legacy pipeline served it.
        assert client.get_states_calls == 1

    @pytest.mark.asyncio
    async def test_window_at_the_ceiling_still_merges(
        self, tmp_path, monkeypatch
    ) -> None:
        """500 is the inclusive maximum the component's schema accepts, so the
        boundary page still splits."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            await ha_search(
                query="kitchen",
                search_types=["automation", "dashboard"],
                offset=450,
                limit=50,
            )

        assert _sent(ws, "ha_mcp_tools/search")[0].kwargs["limit"] == 500


class TestDashboardSplitPagination:
    """The merged page is one global score sort across both legs."""

    @pytest.mark.asyncio
    async def test_window_fetch_and_merged_slice(self, tmp_path, monkeypatch) -> None:
        """With ``offset>0`` the component is asked for the whole
        ``[0, offset+limit)`` prefix, and the server pages the merged list."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(
                [
                    _automation_record("first", 100),
                    _automation_record("second", 80),
                    _automation_record("third", 60),
                ]
            ),
            dashboards_result=_dashboards_result(
                [{"url_path": "energy", "title": "Energy"}]
            ),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen",
                search_types=["automation", "dashboard"],
                offset=2,
                limit=2,
            )

        request = _sent(ws, "ha_mcp_tools/search")[0].kwargs
        assert request["offset"] == 0
        assert request["limit"] == 4
        # Merged order: automation(100), dashboard(100), automation(80),
        # automation(60) — on the score tie the mirrored component tiebreak
        # sorts "automation.first" before the "energy" dashboard_url, so
        # page [2:4] is the 80 then the 60 automation.
        assert [a["entity_id"] for a in resp["automations"]] == [
            "automation.second",
            "automation.third",
        ]
        assert resp["dashboards"] == []
        assert resp["config_total_matches"] == 4
        assert resp["config_has_more"] is False
        assert resp["config_next_offset"] is None
        assert resp["count"] == 2

    @pytest.mark.asyncio
    async def test_score_tie_keeps_component_record_first(
        self, tmp_path, monkeypatch
    ) -> None:
        """On a score tie the mirrored component tiebreak decides the page:
        "automation.kitchen_lights" sorts before the "energy" dashboard_url,
        so the dashboard does not displace the component record."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result([_automation_record("kitchen_lights", 100)]),
            dashboards_result=_dashboards_result(
                [{"url_path": "energy", "title": "Energy"}]
            ),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"], limit=1
            )

        assert [a["entity_id"] for a in resp["automations"]] == [
            "automation.kitchen_lights"
        ]
        assert resp["dashboards"] == []
        assert resp["config_total_matches"] == 2
        assert resp["config_has_more"] is True
        assert resp["config_next_offset"] == 1
        assert resp["has_more"] is True

    def test_equal_score_pages_stay_disjoint_across_offsets(self) -> None:
        """Pages must be cut with the same total order that picked the window.

        The component orders its corpus by ``(-score, _sort_key)``; on an
        all-tied score, "scene.aaa" sorts before "script.aaa" even though the
        scenes bucket is concatenated after scripts. A bucket-order (or
        score-only stable) cut would show "scene.aaa" on BOTH pages and never
        show "script.aaa" (the Codex review's boundary-tie finding)."""
        scene = {"entity_id": "scene.aaa", "score": 100, "match_in_config": True}
        script = {"entity_id": "script.aaa", "score": 100, "match_in_config": True}

        def window(
            records_by_bucket: dict[str, list[dict[str, Any]]],
        ) -> dict[str, Any]:
            return {
                "automations": [],
                "scripts": records_by_bucket.get("scripts", []),
                "scenes": records_by_bucket.get("scenes", []),
                "helpers": [],
                "config_total_matches": 2,
                "config_has_more": False,
            }

        page1, _ = _merge_dashboard_window(
            _resolved_split(offset=0, limit=1),
            window({"scenes": [scene]}),
            [],
        )
        page2, _ = _merge_dashboard_window(
            _resolved_split(offset=1, limit=1),
            window({"scenes": [scene], "scripts": [script]}),
            [],
        )

        assert [r["entity_id"] for r in page1["scenes"]] == ["scene.aaa"]
        assert page1["scripts"] == []
        assert [r["entity_id"] for r in page2["scripts"]] == ["script.aaa"]
        assert page2["scenes"] == []

    @pytest.mark.asyncio
    async def test_component_corpus_past_the_window_reports_has_more(
        self, tmp_path, monkeypatch
    ) -> None:
        """The component's own ``config_has_more`` for the window is OR-ed in:
        its records beyond the window are not in the merged list at all."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(
                [_automation_record("first", 100)],
                config_total_matches=1,
                config_has_more=True,
            ),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"], limit=5
            )

        assert resp["config_total_matches"] == 1
        assert resp["config_has_more"] is True
        assert resp["config_next_offset"] == 5


class TestDashboardSplitIncludeConfig:
    """``include_config`` governs the dashboard records' bodies too."""

    @pytest.mark.asyncio
    async def test_include_config_false_strips_the_body(
        self, tmp_path, monkeypatch
    ) -> None:
        """Mirrors the legacy pipeline's per-record ``config`` pop. A non-zero
        ``yaml_skipped`` sends the leg to the legacy walk, which is the route
        that carries bodies at all."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(),
            dashboards_result=_dashboards_result([], yaml_skipped=1),
        )
        client = MatchingDashboardClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen",
                search_types=["automation", "dashboard"],
                include_config=False,
            )

        assert [d["dashboard_url"] for d in resp["dashboards"]] == ["default"]
        assert "config" not in resp["dashboards"][0]

    @pytest.mark.asyncio
    async def test_include_config_true_keeps_the_body(
        self, tmp_path, monkeypatch
    ) -> None:
        """``include_config=True`` also forces the legacy dashboard walk (the
        component's doc-search carries no bodies), which is where the body
        comes from."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(search_result=_search_result())
        client = MatchingDashboardClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen",
                search_types=["automation", "dashboard"],
                include_config=True,
            )

        assert resp["dashboards"][0]["config"] == {"views": [{"title": "Kitchen"}]}


class TestDashboardSplitPartialSemantics:
    """The dashboard bucket is never dropped silently."""

    @pytest.mark.asyncio
    async def test_dashboard_leg_failure_marks_partial(
        self, tmp_path, monkeypatch
    ) -> None:
        """A raising dashboards leg reports like a failed legacy branch:
        ``partial``, an ``errors[]`` entry naming the surface, the warning
        mirror — and the component's buckets survive intact."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result([_automation_record("kitchen_lights", 100)]),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with (
            patch_ws(ws, tools_search),
            _patch_dashboards_ws(ws),
            patch.object(
                SmartSearchTools,
                "_search_dashboards_surface",
                AsyncMock(side_effect=RuntimeError("dashboards backend exploded")),
            ),
        ):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert [a["entity_id"] for a in resp["automations"]] == [
            "automation.kitchen_lights"
        ]
        assert resp["dashboards"] == []
        assert resp["partial"] is True
        assert {e["surface"] for e in resp["errors"]} == {"dashboards"}
        assert "dashboards backend exploded" in resp["partial_reason"]
        assert any("incomplete results" in w for w in resp["warnings"])

    @pytest.mark.asyncio
    async def test_dashboard_failed_count_marks_partial(
        self, tmp_path, monkeypatch
    ) -> None:
        """A dashboard whose config read failed without raising gets the deep
        path's own ``not scanned`` wording, not a second phrasing."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(),
            dashboards_result=_dashboards_result([], load_failed=2),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["partial"] is True
        assert "2 dashboard(s) not scanned" in resp["partial_reason"]
        assert any("2 dashboard(s) not scanned" in w for w in resp["warnings"])

    @pytest.mark.asyncio
    async def test_legacy_walk_failure_marks_partial(
        self, tmp_path, monkeypatch
    ) -> None:
        """The same count reaches the envelope from the legacy walk fallback."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_result=_search_result(),
            dashboards_result=_dashboards_result([], yaml_skipped=1),
        )
        client = FailingDashboardClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        # yaml_skipped sends the leg to the legacy walk, whose one dashboard
        # config read fails.
        assert client.ws_types["lovelace/config"] == 1
        assert resp["partial"] is True
        assert "1 dashboard(s) not scanned" in resp["partial_reason"]


class TestDashboardSplitComponentLegFailure:
    """The component leg's failure taxonomy is unchanged by the split."""

    @pytest.mark.asyncio
    async def test_command_error_falls_back_once(self, tmp_path, monkeypatch) -> None:
        """A failing component search serves the WHOLE call from legacy — which
        searches dashboards itself, so the split's own dashboard records must
        not be merged on top of it."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_exc=HomeAssistantCommandError("Command failed: boom", "internal"),
            dashboards_result=_dashboards_result(
                [{"url_path": "energy", "title": "Energy"}]
            ),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert any("served via legacy path" in w for w in resp["warnings"])
        assert client.get_states_calls == 1
        # Exactly one "energy" record: the legacy path produced it, and the
        # split leg's copy was dropped rather than merged on top.
        assert [d["dashboard_url"] for d in resp["dashboards"]] == ["energy"]

    @pytest.mark.asyncio
    async def test_component_error_cancels_slow_dashboard_leg(
        self, tmp_path, monkeypatch
    ) -> None:
        """A failed component leg cancels the dashboards leg instead of
        waiting for it: the legacy fallback searches dashboards itself, so a
        slow leg would only delay the fallback and duplicate its I/O."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        state = {"calls": 0, "cancelled": False}

        async def hang_then_serve(
            self: SmartSearchTools,
            query_lower: str,
            exact_match: bool,
            semaphore: asyncio.Semaphore,
            *,
            include_config: bool,
        ) -> tuple[list[dict[str, Any]], int]:
            state["calls"] += 1
            if state["calls"] == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    state["cancelled"] = True
                    raise
            return [], 0

        monkeypatch.setattr(
            SmartSearchTools, "_search_dashboards_surface", hang_then_serve
        )
        ws = _split_ws(
            search_exc=HomeAssistantCommandError("Command failed: boom", "internal"),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert any("served via legacy path" in w for w in resp["warnings"])
        assert state["cancelled"] is True

    @pytest.mark.asyncio
    async def test_unknown_command_falls_back_silently(
        self, tmp_path, monkeypatch
    ) -> None:
        """A component downgraded mid-session is still a silent legacy route."""
        _setup_visibility_disabled(tmp_path, monkeypatch)
        ws = _split_ws(
            search_exc=HomeAssistantCommandError(
                "Command failed: nope", "unknown_command"
            ),
            dashboards_result=_dashboards_result([]),
        )
        client = DashboardRoutingClient()
        ha_search = _build_ha_search(client)

        with patch_ws(ws, tools_search), _patch_dashboards_ws(ws):
            resp = await ha_search(
                query="kitchen", search_types=["automation", "dashboard"]
            )

        assert resp["success"] is True
        assert client.get_states_calls == 1
        assert not any("served via legacy path" in w for w in resp["warnings"])
