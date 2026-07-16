"""Unit tests for project_fields helper in util_helpers (issue #1199)."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.exceptions import ToolError

from ha_mcp.tools.tools_search import register_search_tools
from ha_mcp.tools.util_helpers import project_fields


class TestProjectFields:
    """Test the project_fields shared helper."""

    def test_none_fields_returns_data_unchanged(self):
        data = {"success": True, "results": [1, 2], "count": 2}
        result = project_fields(data, None)
        assert result is data

    def test_single_field_plus_success_retained(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = project_fields(data, ["results"])
        assert set(result.keys()) == {"success", "results"}
        assert result["results"] == [1, 2]

    def test_multiple_fields_retained(self):
        data = {
            "success": True,
            "results": [],
            "count": 0,
            "query": "x",
            "has_more": False,
        }
        result = project_fields(data, ["results", "count"])
        assert set(result.keys()) == {"success", "results", "count"}

    def test_success_always_included_even_if_not_in_fields(self):
        data = {"success": True, "results": [], "count": 0}
        result = project_fields(data, ["count"])
        assert "success" in result
        assert result["success"] is True

    def test_unknown_field_emits_warning(self):
        """Unknown fields key now emits a warning instead of being silently dropped."""
        data = {"success": True, "results": []}
        result = project_fields(data, ["nonexistent"])
        assert result["success"] is True
        assert "warnings" in result
        assert any("nonexistent" in w for w in result["warnings"])

    def test_empty_fields_list_returns_only_success(self):
        data = {"success": True, "results": [], "count": 0}
        result = project_fields(data, [])
        assert set(result.keys()) == {"success"}

    def test_success_in_fields_not_duplicated(self):
        data = {"success": True, "results": []}
        result = project_fields(data, ["success", "results"])
        assert list(result.keys()).count("success") == 1

    def test_empty_data_with_none_fields(self):
        data: dict = {}
        result = project_fields(data, None)
        assert result == {}

    def test_projection_does_not_mutate_original(self):
        data = {"success": True, "results": [1], "count": 1}
        project_fields(data, ["results"])
        assert "count" in data

    def test_warnings_always_retained_alongside_success(self):
        """warnings list survives projection so diagnostic messages are not lost."""
        data = {"success": True, "results": [], "count": 0, "warnings": ["bad field"]}
        result = project_fields(data, ["results"])
        assert "warnings" in result
        assert result["warnings"] == ["bad field"]

    def test_csv_string_input_parsed_correctly(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = project_fields(data, "results,count")
        assert set(result.keys()) == {"success", "results", "count"}

    def test_json_array_string_input_parsed_correctly(self):
        data = {"success": True, "results": [1, 2], "count": 2, "query": "light"}
        result = project_fields(data, '["results"]')
        assert set(result.keys()) == {"success", "results"}


class TestHaSearchEntitiesFieldsProjection:
    """Tool-level coverage of the top-level ``fields=`` projection on the
    consolidated ``ha_search`` tool.

    Re-enabled (was skipped as "ha_search does not expose ``fields=``"): the
    consolidated tool exposes ``fields=`` and applies ``_project_response_fields``
    as the final step of the orchestrator. These exercise the real tool call —
    param parsing, the always-keep contract, the typo guard, and the malformed-
    input error path — not just the ``project_fields`` helper. The response is
    the flat orchestrator envelope (no ``["data"]`` wrapper).
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            name = (
                method.__fastmcp__.name
                if hasattr(method, "__fastmcp__")
                else method.__name__
            )
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # exact_match=True (default) calls client.get_states() + send_websocket_message()
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"friendly_name": "Kitchen Light", "brightness": 200},
                }
            ]
        )
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart_tools = MagicMock()
        # ha_search orchestrator fans out to deep_search whenever ``query`` is
        # set; mock it as an empty-config response so the merge logic doesn't
        # see a non-awaitable MagicMock. The config buckets are present (empty)
        # so a projection can be observed dropping them.
        smart_tools.deep_search = AsyncMock(
            return_value={
                "success": True,
                "automations": [],
                "scripts": [],
                "scenes": [],
                "helpers": [],
                "warnings": [],
            }
        )
        return smart_tools

    @pytest.fixture
    def search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_search"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, search_tool):
        """No projection → the full flat envelope, including the config buckets."""
        data = await search_tool(query="kitchen")
        assert "success" in data
        assert "entities" in data
        assert "automations" in data

    @pytest.mark.asyncio
    async def test_fields_projection_drops_unrequested_buckets(self, search_tool):
        """``fields=["entities"]`` keeps the requested bucket and drops the
        other (non-always-keep) surface buckets — the core projection behavior,
        exercised through the registered tool's own ``fields=`` param."""
        data = await search_tool(query="kitchen", fields=["entities"])
        assert "entities" in data
        # The other surface buckets are neither requested nor always-keep.
        assert "automations" not in data
        assert "scripts" not in data
        assert "scenes" not in data
        assert "helpers" not in data

    @pytest.mark.asyncio
    async def test_fields_projection_retains_always_keep_diagnostics(self, search_tool):
        """Projection narrows the response but never hides the diagnostic /
        pagination contract — ``success`` and the always-keep keys survive even
        when not named in ``fields=``."""
        data = await search_tool(query="kitchen", fields=["entities"])
        assert data["success"] is True
        # Always-keep keys are retained so a narrowing caller can't lose
        # incompleteness / pagination signal.
        assert "search_types" in data
        assert "count" in data

    @pytest.mark.asyncio
    async def test_fields_unknown_key_emits_typo_warning(self, search_tool):
        """A requested key absent from the response surfaces a diagnostic
        warning (with the available keys) rather than a mysteriously empty
        payload."""
        data = await search_tool(query="kitchen", fields=["frobnicate"])
        assert "warnings" in data
        assert any("frobnicate" in w for w in data["warnings"])

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, search_tool):
        with pytest.raises(ToolError):
            await search_tool(query="kitchen", fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, search_tool):
        with pytest.raises(ToolError):
            await search_tool(query="kitchen", fields='["')


class TestHaSearchEntitiesFieldsProjectionAreaBranches:
    """Tool-level ``fields=`` projection across the entity-branch return paths.

    Re-enabled (was skipped on the false "does not expose ``fields=``" reason).
    The consolidated orchestrator no longer projects per-branch — it applies a
    single ``_project_response_fields`` end-pass after every branch assembles
    its response, so one projection covers all exits. These pin that the
    end-pass reaches each entity-branch exit (area+query, area-only populated,
    area-only empty, domain-listing) by projecting onto a non-``entities`` key
    and asserting the ``entities`` bucket is dropped on each path. The response
    is the flat envelope (no ``["data"]`` wrapper); ``search_type`` /
    ``area_names`` / ``message`` are now in ``_ALWAYS_KEEP_PROJECTION``.
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def capture_add_tool(method):
            name = (
                method.__fastmcp__.name
                if hasattr(method, "__fastmcp__")
                else method.__name__
            )
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        # Used by the domain-listing branch (parallel states + registry fetch)
        client.get_states = AsyncMock(
            return_value=[
                {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "attributes": {"friendly_name": "Kitchen Light"},
                },
                {
                    "entity_id": "light.living_room",
                    "state": "off",
                    "attributes": {"friendly_name": "Living Room Light"},
                },
            ]
        )
        # Default WS response (registry list / alias enrichment): empty success.
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        return client

    @pytest.fixture
    def mock_smart_tools_populated(self):
        """smart_tools mock returning one area with one entity (kitchen.light)."""
        smart = MagicMock()
        smart.get_entities_by_area = AsyncMock(
            return_value={
                "areas": {
                    "kitchen": {
                        "area_name": "Kitchen",
                        "entities": {
                            "light": [
                                {
                                    "entity_id": "light.kitchen",
                                    "friendly_name": "Kitchen Light",
                                    "state": "on",
                                    "_hidden_by": None,
                                }
                            ],
                        },
                    }
                }
            }
        )
        return smart

    @pytest.fixture
    def mock_smart_tools_empty(self):
        """smart_tools mock returning no matched areas."""
        smart = MagicMock()
        smart.get_entities_by_area = AsyncMock(return_value={"areas": {}})
        return smart

    @pytest.fixture
    def search_tool_populated(self, mock_mcp, mock_client, mock_smart_tools_populated):
        register_search_tools(
            mock_mcp, mock_client, smart_tools=mock_smart_tools_populated
        )
        return self.registered_tools["ha_search"]

    @pytest.fixture
    def search_tool_empty(self, mock_mcp, mock_client, mock_smart_tools_empty):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools_empty)
        return self.registered_tools["ha_search"]

    @pytest.mark.asyncio
    async def test_area_plus_query_branch_projects(self, search_tool_populated):
        """The end-pass honors ``fields=`` on the area+query branch for a
        *non-always-keep* key: ``entities`` is retained when requested and
        dropped when not. ``search_type`` is in ``_ALWAYS_KEEP_PROJECTION``, so
        asserting it survives can't witness projection — only a non-always-keep
        key can (PR #1529 R8)."""
        retained = await search_tool_populated(
            query="kitchen", area_filter="kitchen", fields=["entities"]
        )
        assert "entities" in retained
        dropped = await search_tool_populated(
            query="kitchen", area_filter="kitchen", fields=["search_type"]
        )
        assert "success" in dropped
        assert "entities" not in dropped

    @pytest.mark.asyncio
    async def test_area_plus_query_branch_unprojected_baseline(
        self, search_tool_populated
    ):
        """fields=None on area+query returns the full response (sanity check)."""
        data = await search_tool_populated(query="kitchen", area_filter="kitchen")
        assert "entities" in data
        assert "search_type" in data
        assert data["search_type"] == "area_filtered_query"

    @pytest.mark.asyncio
    async def test_area_only_populated_branch_projects(self, search_tool_populated):
        """The end-pass projects the area-only populated branch: the
        non-always-keep ``entities`` bucket is retained when requested and
        dropped when not."""
        retained = await search_tool_populated(
            area_filter="kitchen", fields=["entities"]
        )
        assert "entities" in retained
        dropped = await search_tool_populated(
            area_filter="kitchen", fields=["search_type"]
        )
        assert "success" in dropped
        assert "entities" not in dropped

    @pytest.mark.asyncio
    async def test_area_only_populated_branch_unprojected_baseline(
        self, search_tool_populated
    ):
        """fields=None on area-only returns the full response (sanity check)."""
        data = await search_tool_populated(area_filter="kitchen")
        assert "entities" in data
        assert "search_type" in data
        assert data["search_type"] == "area_only"
        assert "area_names" in data

    @pytest.mark.asyncio
    async def test_area_only_empty_branch_projects(self, search_tool_empty):
        """The end-pass projects the area-only empty branch: the non-always-keep
        ``entities`` bucket (``[]`` on this branch) is retained when requested
        and dropped when not. ``message`` is always-keep, so requesting it can't
        witness projection — it survives either way."""
        retained = await search_tool_empty(
            area_filter="nonexistent", fields=["entities"]
        )
        assert "entities" in retained
        dropped = await search_tool_empty(area_filter="nonexistent", fields=["message"])
        assert "success" in dropped
        assert "message" in dropped
        assert "entities" not in dropped

    @pytest.mark.asyncio
    async def test_area_only_empty_branch_unprojected_baseline(self, search_tool_empty):
        """fields=None on the empty-area branch returns the full response."""
        data = await search_tool_empty(area_filter="nonexistent")
        assert "entities" in data
        assert data["entities"] == []
        assert "message" in data

    @pytest.mark.asyncio
    async def test_domain_listing_branch_projects(self, search_tool_populated):
        """The end-pass projects the domain-listing branch (empty query +
        domain_filter; client.get_states drives the result): the non-always-keep
        ``entities`` bucket is retained when requested and dropped when not."""
        retained = await search_tool_populated(
            domain_filter="light", fields=["entities"]
        )
        assert "entities" in retained
        dropped = await search_tool_populated(
            domain_filter="light", fields=["search_type"]
        )
        assert "success" in dropped
        assert "entities" not in dropped

    @pytest.mark.asyncio
    async def test_domain_listing_branch_unprojected_baseline(
        self, search_tool_populated
    ):
        """fields=None on the domain-listing branch returns the full response."""
        data = await search_tool_populated(domain_filter="light")
        assert "entities" in data
        assert "search_type" in data
        assert data["search_type"] == "domain_listing"


# ---------------------------------------------------------------------------
# New test classes for feature #1199 review items
# ---------------------------------------------------------------------------

_MULTI_ENTITY_STATES = [
    {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"friendly_name": "Kitchen Light"},
    },
    {
        "entity_id": "light.bedroom",
        "state": "off",
        "attributes": {"friendly_name": "Bedroom Light"},
    },
    {
        "entity_id": "switch.fan",
        "state": "on",
        "attributes": {"friendly_name": "Fan Switch"},
    },
    {
        "entity_id": "switch.pump",
        "state": "off",
        "attributes": {"friendly_name": "Pump Switch"},
    },
]


class _SearchToolFixture:
    """Shared fixture mixin for ha_search tests."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def capture_add_tool(method):
            name = (
                method.__fastmcp__.name
                if hasattr(method, "__fastmcp__")
                else method.__name__
            )
            self.registered_tools[name] = method

        mcp.add_tool = capture_add_tool
        return mcp

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(return_value=_MULTI_ENTITY_STATES)
        client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": []}
        )
        return client

    @pytest.fixture
    def mock_smart_tools(self):
        smart_tools = MagicMock()
        # ha_search orchestrator fans out to deep_search whenever ``query`` is
        # set; mock it as an empty-config response so the merge logic doesn't
        # see a non-awaitable MagicMock.
        smart_tools.deep_search = AsyncMock(
            return_value={
                "success": True,
                "automations": [],
                "scripts": [],
                "scenes": [],
                "helpers": [],
                "warnings": [],
            }
        )
        return smart_tools

    @pytest.fixture
    def search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_search"]


class TestHaSearchEntitiesPerDomainLimit(_SearchToolFixture):
    """Tests for per_domain_limit= cap on group_by_domain results (issue #1199)."""

    @pytest.mark.asyncio
    async def test_per_domain_limit_caps_by_domain_entries(self, search_tool):
        """per_domain_limit=1 with group_by_domain=True caps each domain to 1 entity."""
        result = await search_tool(
            query="light",
            group_by_domain=True,
            per_domain_limit=1,
            limit=20,
        )
        by_domain = result.get("by_domain", {})
        assert "light" in by_domain
        assert len(by_domain["light"]) <= 1, (
            "per_domain_limit=1 should cap each domain bucket to at most 1 entity"
        )

    @pytest.mark.asyncio
    async def test_per_domain_limit_no_cap_without_group_by_domain(self, search_tool):
        """per_domain_limit is ignored when group_by_domain=False (no by_domain key)."""
        result = await search_tool(
            query="light",
            group_by_domain=False,
            per_domain_limit=1,
        )
        data = result
        # Results still present; no by_domain grouping
        assert "entities" in data
        assert "by_domain" not in data

    @pytest.mark.asyncio
    async def test_per_domain_limit_domain_listing_branch(self, search_tool):
        """per_domain_limit=1 with group_by_domain=True works in domain_listing branch."""
        result = await search_tool(
            domain_filter="light",
            group_by_domain=True,
            per_domain_limit=1,
            limit=20,
        )
        by_domain = result.get("by_domain", {})
        assert "light" in by_domain
        assert len(by_domain["light"]) <= 1


class TestHaSearchEntitiesStateFilter(_SearchToolFixture):
    """Tests for state_filter= normalization and per-branch behavior (issue #1199)."""

    @pytest.mark.asyncio
    async def test_state_filter_exact_match_keeps_matching_entities(self, search_tool):
        """state_filter='on' in exact_match mode keeps only 'on' entities."""
        result = await search_tool(query="light", state_filter="on")
        data = result
        for entity in data["entities"]:
            assert entity["state"] == "on", (
                "state_filter='on' should remove non-'on' entities from results"
            )

    @pytest.mark.asyncio
    async def test_state_filter_strips_surrounding_whitespace(self, search_tool):
        """state_filter with whitespace padding is normalized before matching."""
        result_padded = await search_tool(query="light", state_filter="  on  ")
        result_plain = await search_tool(query="light", state_filter="on")
        # Both should return the same set of entities
        padded_ids = {e["entity_id"] for e in result_padded["entities"]}
        plain_ids = {e["entity_id"] for e in result_plain["entities"]}
        assert padded_ids == plain_ids

    @pytest.mark.asyncio
    async def test_state_filter_not_echoed_at_top_level(self, search_tool):
        """state_filter is a caller-input echo and must not bleed into the
        ha_search top-level envelope (entities-branch strip — see
        `_ENTITIES_BRANCH_SKIP_KEYS`). The filtering still applies to
        `entities`; the caller already has the input they passed.
        """
        result = await search_tool(query="light", state_filter="on")
        assert "state_filter" not in result, (
            f"state_filter must not echo at top level of ha_search; "
            f"got keys {sorted(result)}"
        )

    @pytest.mark.asyncio
    async def test_state_filter_domain_listing_branch(self, search_tool):
        """state_filter works in the domain_listing branch (empty query + domain_filter).

        The filter still applies to each entity in ``entities``; the input
        echo at top level is stripped at the orchestrator (see
        ``_ENTITIES_BRANCH_SKIP_KEYS``), so callers must not expect the
        echo even when they read it through the domain_listing branch.
        """
        result = await search_tool(domain_filter="light", state_filter="on")
        data = result
        for entity in data["entities"]:
            assert entity["state"] == "on"
        assert "state_filter" not in data, (
            f"state_filter must not echo at top level even in domain_listing; "
            f"got keys {sorted(data)}"
        )

    @pytest.mark.asyncio
    async def test_state_filter_whitespace_only_treated_as_no_filter(self, search_tool):
        """state_filter='   ' (whitespace only) is treated as no filter (None)."""
        result_no_filter = await search_tool(query="light")
        result_ws_filter = await search_tool(query="light", state_filter="   ")
        no_filter_count = result_no_filter["count"]
        ws_filter_count = result_ws_filter["count"]
        # Both should return the same number of results (no filtering applied)
        assert ws_filter_count == no_filter_count


class TestHaSearchEntitiesResultFields(_SearchToolFixture):
    """Tests for result_fields= per-record projection (issue #1199)."""

    @pytest.mark.asyncio
    async def test_result_fields_projects_entity_records(self, search_tool):
        """result_fields=['entity_id','state'] limits each record to those keys."""
        result = await search_tool(query="light", result_fields=["entity_id", "state"])
        data = result
        for entity in data["entities"]:
            assert set(entity.keys()) == {"entity_id", "state"}

    @pytest.mark.asyncio
    async def test_result_fields_outer_response_keys_preserved(self, search_tool):
        """result_fields only projects inside results[]; top-level keys are unchanged."""
        result = await search_tool(query="light", result_fields=["entity_id"])
        data = result
        assert "success" in data
        assert "entity_total_matches" in data
        assert "count" in data

    @pytest.mark.asyncio
    async def test_result_fields_unknown_key_rejected(self, search_tool):
        """An unknown result_fields key is now a hard validation error.

        result_fields drives area/floor/labels/aliases enrichment (issue #1813
        C1), so the server must recognise every requested key — an unknown one is
        rejected up front rather than silently projecting each record to ``{}``.
        """
        with pytest.raises(ToolError) as excinfo:
            await search_tool(query="light", result_fields=["nonexistent_key"])
        assert "Unknown result_fields" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_result_fields_domain_listing_branch(self, search_tool):
        """result_fields works in the domain_listing branch."""
        result = await search_tool(domain_filter="light", result_fields=["entity_id"])
        data = result
        for entity in data["entities"]:
            assert set(entity.keys()) == {"entity_id"}

    @pytest.mark.asyncio
    async def test_enrichment_healthy_reads_emit_no_warning(self, search_tool):
        """Byte-parity guard: with every registry read succeeding, an enrichment
        request adds no degraded-enrichment warning."""
        data = await search_tool(query="light", result_fields=["entity_id", "area"])
        assert not any(
            "enrichment incomplete" in w for w in data.get("warnings", [])
        ), f"healthy enrichment must not warn; got {data.get('warnings')}"


class TestHaSearchEnrichmentDegraded(_SearchToolFixture):
    """A failed registry read during result_fields enrichment surfaces a top-level
    warning and logs, instead of silently emitting null area/floor/labels
    indistinguishable from a genuinely-unassigned entity (silent-failure fix,
    issue #1813 F1)."""

    @pytest.fixture
    def mock_client(self):
        client = MagicMock()
        client.base_url = "http://localhost:8123"
        client.get_config = AsyncMock(return_value={"time_zone": "UTC"})
        client.get_states = AsyncMock(return_value=_MULTI_ENTITY_STATES)

        async def _ws(msg):
            # One registry read fails; every other read succeeds (empty).
            if msg.get("type") == "config/area_registry/list":
                return {"success": False, "error": "boom"}
            return {"success": True, "result": []}

        client.send_websocket_message = AsyncMock(side_effect=_ws)
        return client

    @pytest.mark.asyncio
    async def test_degraded_enrichment_read_surfaces_warning(self, search_tool, caplog):
        with caplog.at_level(logging.WARNING):
            data = await search_tool(
                query="light", result_fields=["entity_id", "area"]
            )
        # Records are still returned — enrichment never withholds results.
        assert data["entities"], "entities must still be returned on a degraded join"
        # The degradation is surfaced as a top-level warning...
        assert any("enrichment incomplete" in w for w in data.get("warnings", [])), (
            f"expected a degraded-enrichment warning; got {data.get('warnings')}"
        )
        # ...and logged, not silent.
        assert any(
            "result_fields_enrichment_failed" in r.getMessage() for r in caplog.records
        )


# Fuzzy results returned by smart_tools.smart_entity_search mock.
# 5 total matches in the index; this page contains 3, only 1 matches "on".
_FUZZY_RESULT = {
    "results": [
        {"entity_id": "light.kitchen", "state": "on", "domain": "light"},
        {"entity_id": "light.bedroom", "state": "off", "domain": "light"},
        {"entity_id": "light.hall", "state": "unavailable", "domain": "light"},
    ],
    "total_matches": 5,
    "has_more": True,
    "offset": 0,
    "limit": 3,
    "count": 3,
}


class TestHaSearchEntitiesFuzzyStateFilter(_SearchToolFixture):
    """Tests for state_filter= semantics on the fuzzy (exact_match=False) branch.

    Pins the dual-count contract: total_matches is the unfiltered fuzzy count,
    count reflects only the entities on this page that matched the state filter.
    """

    @pytest.fixture
    def mock_smart_tools(self):
        smart = MagicMock()
        smart.smart_entity_search = AsyncMock(return_value=dict(_FUZZY_RESULT))
        smart.deep_search = AsyncMock(
            return_value={
                "success": True,
                "automations": [],
                "scripts": [],
                "scenes": [],
                "helpers": [],
                "warnings": [],
            }
        )
        return smart

    @pytest.mark.asyncio
    async def test_fuzzy_state_filter_total_matches_is_unfiltered(self, search_tool):
        """total_matches stays at the unfiltered fuzzy count; count is post-filter.

        This pins the dual-count contract: the fuzzy engine already paginated
        internally so total_matches cannot be recomputed after state filtering.
        """
        result = await search_tool(query="light", exact_match=False, state_filter="on")
        data = result
        # count reflects only the filtered page
        assert data["count"] == 1, "count should reflect only the on-state entity"
        assert data["entities"][0]["entity_id"] == "light.kitchen"
        # total_matches is the raw fuzzy-engine number, not re-counted
        assert data["entity_total_matches"] == 5, (
            "entity_total_matches must remain the unfiltered fuzzy count"
        )

    @pytest.mark.asyncio
    async def test_fuzzy_state_filter_note_present(self, search_tool):
        """state_filter_note appears in the response to explain the dual-count."""
        result = await search_tool(query="light", exact_match=False, state_filter="on")
        data = result
        assert "state_filter_note" in data
        assert "has_more" in data["state_filter_note"]

    @pytest.mark.asyncio
    async def test_state_filter_note_survives_fields_projection(self, search_tool):
        """state_filter_note is force-retained even when not in fields=.

        A caller projecting to only ``entities`` still needs the note to
        understand that ``entity_total_matches`` is the unfiltered fuzzy
        count, not the post-filter result count. Pinned via
        ``_ALWAYS_KEEP_PROJECTION``.
        """
        result = await search_tool(
            query="light",
            exact_match=False,
            state_filter="on",
            fields=["entities"],
        )
        data = result
        # state_filter_note must be force-retained even when not listed in fields=
        assert "state_filter_note" in data, (
            "state_filter_note must survive fields= projection "
            f"(in _ALWAYS_KEEP_PROJECTION); got keys {sorted(data)}"
        )
        # Requested key present.
        assert "entities" in data
        # entity_total_matches is also force-retained (in _ALWAYS_KEEP_PROJECTION).
        assert "entity_total_matches" in data
