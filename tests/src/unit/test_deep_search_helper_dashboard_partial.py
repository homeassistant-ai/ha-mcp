"""Unit tests for the helper- and dashboard-surface ``partial`` wiring in
``deep_search``.

The PR's headline — honest ``partial`` flagging when a config-body backend
fails — initially covered only automation / script / scene. The helper
(``input_*`` + flow-helper) and dashboard surfaces still swallowed backend
failures to an empty list, so a failed backend returned ``partial: False``
with no warning (the exact "clean-looking incomplete" pattern the PR set out
to eliminate). These tests pin the closed gap at two levels:

- **Component**: ``_search_helper_type`` / ``_search_one_dashboard`` /
  ``_deep_search_dashboards`` signal failure distinctly from a clean
  zero-match.
- **Seam**: a failed helper / dashboard backend driven through the public
  ``deep_search`` entrypoint reaches ``result["partial"]`` and names the gap
  in ``result["partial_reason"]`` — the wiring the component tests can't see.
"""

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools


def _make_tools(client) -> SmartSearchTools:
    """Construct SmartSearchTools without loading global settings."""
    with patch("ha_mcp.tools.smart_search.get_global_settings") as mock_settings:
        mock_settings.return_value.fuzzy_threshold = 60
        return SmartSearchTools(client=client)


# --------------------------------------------------------------------------
# Component: _search_helper_type
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSearchHelperTypeFailure:
    async def test_soft_non_success_signals_failed(self) -> None:
        """A ``{"success": False}`` list response is a backend failure, not a
        clean zero-match — it must return ``failed=True`` so the gather can
        route it to ``partial`` instead of swallowing it to ``[]``."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        tools = _make_tools(client)
        matches, failed = await tools._search_helper_type(
            "input_boolean", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is True

    async def test_raise_signals_failed(self) -> None:
        """A raised list fetch returns ``failed=True`` rather than being
        swallowed by the ``except`` to a silent empty list."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(side_effect=RuntimeError("ws down"))
        tools = _make_tools(client)
        matches, failed = await tools._search_helper_type(
            "input_number", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is True

    async def test_clean_empty_is_not_failed(self) -> None:
        """A successful list with no query match is a genuine zero —
        ``failed`` stays False so a clean instance doesn't report partial."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        tools = _make_tools(client)
        matches, failed = await tools._search_helper_type(
            "input_text", "zzznomatch", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is False


# --------------------------------------------------------------------------
# Component: _search_one_dashboard / _deep_search_dashboards
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDashboardFailure:
    async def test_one_dashboard_non_dict_config_signals_failed(self) -> None:
        """A non-dict ``lovelace/config`` response is a backend failure for
        that dashboard — ``failed=True``, distinct from a clean no-match."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(return_value="not-a-dict")
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "default", "Default", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is True

    async def test_one_dashboard_soft_non_success_signals_failed(self) -> None:
        """A soft websocket failure (``{"success": False}`` — does NOT raise,
        e.g. a 403-after-retries) is a backend failure, not a clean no-match.
        Without the guard it would be searched as an error envelope and report
        ``failed=False`` — the same silent-incompleteness class the scene
        registry walk handles."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"success": False, "error": "WebSocket request blocked (403)"}
        )
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "default", "Default", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is True

    async def test_one_dashboard_config_not_found_is_clean_no_match(self) -> None:
        """``config_not_found`` is NOT a backend failure: an auto-generated
        dashboard (strategy-backed, never taken control of) has no stored
        config, so there is nothing to scan and the failure envelope must
        read as a clean no-match. Issue #2008: a stock default dashboard
        made every dashboard search report partial."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: No config found.",
                "error_code": "config_not_found",
            }
        )
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "default", "Default", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is False

    async def test_one_dashboard_unknown_config_still_signals_failed(self) -> None:
        """``config_not_found`` with the "Unknown config specified" message
        is HA's OTHER cause for the same code — the url_path no longer
        resolves (dashboard deleted between the registry-list snapshot and
        this fetch). That is a genuine scan gap and must stay ``failed=True``;
        only the no-stored-config form is a clean skip."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": "Command failed: Unknown config specified: gone-dash",
                "error_code": "config_not_found",
            }
        )
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "gone-dash", "Gone", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is True

    async def test_one_dashboard_nested_config_not_found_is_clean_no_match(
        self,
    ) -> None:
        """The nested ``error.code``/``error.message`` shape is accepted
        defensively alongside the client envelope's flat form (the flat shape
        is the only one ``send_websocket_message`` emits today; the nested
        check mirrors ``dashboard_screenshot/paths.py``'s detection)."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={
                "success": False,
                "error": {"code": "config_not_found", "message": "No config found."},
            }
        )
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "auto-gen", "Auto", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is False

    async def test_one_dashboard_raise_signals_failed(self) -> None:
        """A raised config fetch returns ``failed=True`` rather than swallowing
        to a silent empty list."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(side_effect=RuntimeError("ws down"))
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "default", "Default", "x", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is True

    async def test_one_dashboard_clean_no_match_not_failed(self) -> None:
        """A valid config dict with no query match is a genuine zero —
        ``failed`` stays False."""
        client = MagicMock()
        client.send_websocket_message = AsyncMock(
            return_value={"result": {"views": []}}
        )
        tools = _make_tools(client)
        matches, failed = await tools._search_one_dashboard(
            "default", "Default", "zzznomatch", True, asyncio.Semaphore(4)
        )
        assert matches == []
        assert failed is False

    async def test_deep_search_dashboards_list_failure_counts(self) -> None:
        """``fetch_dashboards_list`` returning None (unexpected shape) is
        counted — previously the ``or []`` swallowed it to a clean empty."""
        client = MagicMock()
        # Unexpected shape → fetch_dashboards_list returns None → list_failed.
        client.send_websocket_message = AsyncMock(return_value={"unexpected": "shape"})
        tools = _make_tools(client)
        results, failed_count = await tools._deep_search_dashboards(
            "zzznomatch", True, asyncio.Semaphore(4)
        )
        assert results == []
        assert failed_count >= 1

    async def test_deep_search_dashboards_per_dashboard_failure_counts(self) -> None:
        """Per-dashboard config-fetch failures (raised) are each counted; the
        registry-list itself succeeded (no list_failed)."""

        async def _ws(msg):
            if msg.get("type") == "lovelace/dashboards/list":
                return {"result": [{"url_path": "lovelace-extra", "title": "Extra"}]}
            raise RuntimeError("config ws down")  # lovelace/config for each dashboard

        client = MagicMock()
        client.send_websocket_message = AsyncMock(side_effect=_ws)
        tools = _make_tools(client)
        results, failed_count = await tools._deep_search_dashboards(
            "x", True, asyncio.Semaphore(4)
        )
        assert results == []
        # default + lovelace-extra both fail their config fetch; list ok.
        assert failed_count == 2

    async def test_deep_search_dashboards_per_dashboard_soft_failure_counts(
        self,
    ) -> None:
        """A per-dashboard *soft* failure (non-dict config, returned as
        ``(..., True)`` rather than raised) is counted via the gather's tuple
        branch — pins ``if dash_failed: failed_count += 1`` distinctly from the
        Exception branch. One dashboard soft-fails, the other is clean → 1."""

        async def _ws(msg):
            if msg.get("type") == "lovelace/dashboards/list":
                return {"result": [{"url_path": "lovelace-extra", "title": "Extra"}]}
            # lovelace/config: the extra dashboard returns a non-dict (soft
            # fail); the default dashboard returns a clean empty config.
            if msg.get("url_path") == "lovelace-extra":
                return "not-a-dict"
            return {"result": {"views": []}}

        client = MagicMock()
        client.send_websocket_message = AsyncMock(side_effect=_ws)
        tools = _make_tools(client)
        results, failed_count = await tools._deep_search_dashboards(
            "zzznomatch", True, asyncio.Semaphore(4)
        )
        assert results == []
        # Only the extra dashboard soft-failed; default clean; list ok.
        assert failed_count == 1


# --------------------------------------------------------------------------
# Seam: failures reach result["partial"] through public deep_search()
# --------------------------------------------------------------------------


@pytest.mark.asyncio
class TestHelperDashboardPartialThroughDeepSearch:
    async def test_helper_soft_failure_surfaces_partial(self) -> None:
        """All six ``input_*`` list fetches soft-failing must drive
        ``deep_search`` to ``partial: True`` with the helper fragment — pins
        the ``helper_failed`` forward through ``_deep_search_helpers`` →
        ``_paginate_and_build_response`` → ``_apply_per_type_partial_flag``."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        # input_*/list → soft failure; flow-helper config-entries list → clean empty.
        client.send_websocket_message = AsyncMock(return_value={"success": False})
        client._request = AsyncMock(return_value=[])
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="anything", search_types=["helper"], limit=10
        )

        assert result["partial"] is True, (
            f"a failed helper backend must flag partial through deep_search; "
            f"got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "helper backend(s) not scanned" in reason, (
            f"partial_reason must name the helper gap; got {reason!r}"
        )
        # Six input_* types each soft-fail → the real count must reach the
        # reason (pins against a hardcoded slot rather than the actual count).
        assert re.search(r"\b6 helper backend\(s\)", reason), (
            f"partial_reason must carry the real helper_failed count (6); "
            f"got {reason!r}"
        )

    async def test_flow_helper_list_failure_surfaces_partial(self) -> None:
        """The flow-helper config-entries list fetch raising adds to
        ``helper_failed`` even when the input_* lists succeed cleanly."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        # input_*/list → clean empty success; flow-helper list → raises.
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        client._request = AsyncMock(side_effect=RuntimeError("config entries down"))
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="anything", search_types=["helper"], limit=10
        )

        assert result["partial"] is True
        assert "helper backend(s) not scanned" in result["partial_reason"]
        # Only the flow-helper surface failed → exactly one.
        assert re.search(r"\b1 helper backend\(s\)", result["partial_reason"])

    async def test_flow_helper_options_probe_failure_surfaces_partial(self) -> None:
        """A flow-helper whose options-flow probe FAILS (vs returns a
        genuinely-empty options form) must drive ``partial`` through
        deep_search — even when the entry does not match the query. Without it
        a user searching for content inside a template/group helper whose
        options endpoint is down gets a false "no match" with no incompleteness
        signal. Pins the per-entry probe failure forward: ``_score_flow_entry``
        → ``_search_flow_helpers`` count → ``helper_failed`` → partial."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        # input_*/list → clean empty success (no helper-type failures).
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        # flow-helper config-entries list → one template helper that won't match
        # the query, forcing the options probe (title score < 100).
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXPROBEFAIL",
                    "domain": "template",
                    "title": "Some Template",
                    "supports_options": True,
                }
            ]
        )
        # The options-flow probe raises → probe failure (not an empty form).
        client.start_options_flow = AsyncMock(
            side_effect=RuntimeError("options flow down")
        )
        client.abort_options_flow = AsyncMock()
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["helper"], limit=10
        )

        assert result["partial"] is True, (
            f"a failed options-flow probe must flag partial through deep_search; "
            f"got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "helper backend(s) not scanned" in reason
        # Exactly one flow-helper probe failed; input_* lists were clean.
        assert re.search(r"\b1 helper backend\(s\)", reason), (
            f"partial_reason must carry the real probe-failure count (1); "
            f"got {reason!r}"
        )

    async def test_flow_helper_empty_options_form_stays_not_partial(self) -> None:
        """A flow-helper whose options probe SUCCEEDS but yields an empty form
        (``{}`` options — a genuinely-empty read) must NOT flag partial. Guards
        the failure/empty distinction ``fetch_entry_options_with_status`` draws:
        an empty form is a clean read, not a backend failure."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        client._request = AsyncMock(
            return_value=[
                {
                    "entry_id": "01HXEMPTYFORM",
                    "domain": "template",
                    "title": "Some Template",
                    "supports_options": True,
                }
            ]
        )
        # A form first-step with no harvestable fields → options {} but ok=True.
        client.start_options_flow = AsyncMock(
            return_value={"flow_id": "f1", "type": "form", "data_schema": []}
        )
        client.abort_options_flow = AsyncMock()
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["helper"], limit=10
        )

        assert not result.get("partial"), (
            f"a genuinely-empty options form is a clean read, not a failure; "
            f"got {result.get('partial')!r} / {result.get('partial_reason')!r}"
        )

    async def test_dashboard_list_failure_surfaces_partial(self) -> None:
        """A failed dashboard registry-list driven through ``deep_search``
        must flag ``partial`` and name the dashboard gap — pins the
        ``dashboard_failed`` forward (opt-in surface, so this only runs when
        ``dashboard`` is in ``search_types``)."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(return_value={"unexpected": "shape"})
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["dashboard"], limit=10
        )

        assert result["partial"] is True, (
            f"a failed dashboard backend must flag partial through deep_search; "
            f"got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "dashboard(s) not scanned" in reason
        # The real count (1: the registry-list failure) must reach the reason,
        # not a hardcoded slot — same guard the helper count tests apply.
        assert re.search(r"\b1 dashboard\(s\)", reason), (
            f"partial_reason must carry the real dashboard_failed count (1); "
            f"got {reason!r}"
        )

    async def test_dashboard_per_config_soft_failure_surfaces_partial(self) -> None:
        """The per-dashboard soft ``{"success": False}`` config failure must
        surface ``partial`` through the public ``deep_search`` seam — not just
        at the component level. The registry list succeeds (one entry); that
        entry's ``lovelace/config`` soft-fails while the default dashboard is
        clean. Pins the per-dashboard config-threading distinctly from the
        list-failure path, so a regression there can't ship green."""

        async def _ws(msg):
            if msg.get("type") == "lovelace/dashboards/list":
                return {"result": [{"url_path": "lovelace-extra", "title": "Extra"}]}
            # lovelace/config: the extra dashboard soft-fails (403-after-retries
            # shape); the default dashboard returns a clean empty config.
            if msg.get("url_path") == "lovelace-extra":
                return {"success": False, "error": "WebSocket request blocked (403)"}
            return {"result": {"views": []}}

        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(side_effect=_ws)
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["dashboard"], limit=10
        )

        assert result["partial"] is True, (
            f"a per-dashboard soft config failure must flag partial through "
            f"deep_search; got {result.get('partial')!r}"
        )
        reason = result["partial_reason"]
        assert "dashboard(s) not scanned" in reason
        # Only the extra dashboard's config soft-failed → exactly one.
        assert re.search(r"\b1 dashboard\(s\)", reason), (
            f"partial_reason must carry the real dashboard_failed count (1); "
            f"got {reason!r}"
        )

    async def test_dashboard_config_not_found_stays_not_partial(self) -> None:
        """An auto-generated dashboard's ``config_not_found`` driven through
        the public ``deep_search`` seam must NOT flag partial — the exact
        false-``partial`` issue #2008 reports (one never-taken-control
        dashboard turned every dashboard search into 'not exhaustive')."""

        async def _ws(msg):
            if msg.get("type") == "lovelace/dashboards/list":
                return {"result": [{"url_path": "auto-gen", "title": "Auto"}]}
            # lovelace/config: the auto-generated dashboard has no stored
            # config; the default dashboard returns a clean empty config.
            if msg.get("url_path") == "auto-gen":
                return {
                    "success": False,
                    "error": "Command failed: No config found.",
                    "error_code": "config_not_found",
                }
            return {"result": {"views": []}}

        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(side_effect=_ws)
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["dashboard"], limit=10
        )

        assert not result.get("partial"), (
            f"a config-less auto-generated dashboard is a clean no-match, "
            f"not a scan failure; got {result.get('partial')!r} / "
            f"{result.get('partial_reason')!r}"
        )

    async def test_clean_helper_instance_stays_not_partial(self) -> None:
        """All helper backends succeeding (empty results) must NOT flag
        partial — guards against a counter that increments unconditionally and
        false-reports a clean instance as incomplete."""
        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        client._request = AsyncMock(return_value=[])
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["helper"], limit=10
        )

        assert not result.get("partial"), (
            f"a clean helper instance must not report partial; "
            f"got {result.get('partial')!r} / {result.get('partial_reason')!r}"
        )

    async def test_clean_dashboard_instance_stays_not_partial(self) -> None:
        """A clean dashboard instance (valid list, clean configs) must NOT
        flag partial."""

        async def _ws(msg):
            if msg.get("type") == "lovelace/dashboards/list":
                return {"result": []}
            return {"result": {"views": []}}

        client = MagicMock()
        client.get_states = AsyncMock(return_value=[])
        client.send_websocket_message = AsyncMock(side_effect=_ws)
        tools = _make_tools(client)

        result = await tools.deep_search(
            query="zzznomatch", search_types=["dashboard"], limit=10
        )

        assert not result.get("partial"), (
            f"a clean dashboard instance must not report partial; "
            f"got {result.get('partial')!r} / {result.get('partial_reason')!r}"
        )
