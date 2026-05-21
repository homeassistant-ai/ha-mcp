"""Unit tests for project_fields helper in util_helpers (issue #1199)."""

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
    """Tool-level tests for fields= projection in ha_search_entities."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
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
        return MagicMock()

    @pytest.fixture
    def search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_search_entities"]

    @pytest.mark.asyncio
    async def test_fields_none_returns_full_response(self, search_tool):
        result = await search_tool(query="kitchen")
        data = result["data"]
        assert "success" in data
        assert "results" in data

    @pytest.mark.asyncio
    async def test_fields_single_key_projects_correctly(self, search_tool):
        result = await search_tool(query="kitchen", fields=["results"])
        data = result["data"]
        assert "results" in data
        assert "success" in data
        assert "total_matches" not in data

    @pytest.mark.asyncio
    async def test_fields_success_always_present(self, search_tool):
        result = await search_tool(query="kitchen", fields=["results"])
        assert result["data"]["success"] is True

    @pytest.mark.asyncio
    async def test_malformed_fields_raises_tool_error(self, search_tool):
        with pytest.raises(ToolError):
            await search_tool(query="kitchen", fields=123)

    @pytest.mark.asyncio
    async def test_bad_json_fields_raises_tool_error(self, search_tool):
        with pytest.raises(ToolError):
            await search_tool(query="kitchen", fields='["')


class TestHaSearchEntitiesFieldsProjectionAreaBranches:
    """Tool-level tests for fields= projection across the four area-related return paths.

    The regular-search return at ``tools_search.py:795`` is covered by
    ``TestHaSearchEntitiesFieldsProjection`` above; this class pins the other
    four projection call sites so a regression removing ``project_fields(...)``
    at any of them still produces a test failure:

    - area+query branch        (``tools_search.py:479``)
    - area-only populated      (``tools_search.py:581``)
    - area-only empty          (``tools_search.py:600``)
    - domain-listing branch    (``tools_search.py:694``)
    """

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
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
        return self.registered_tools["ha_search_entities"]

    @pytest.fixture
    def search_tool_empty(self, mock_mcp, mock_client, mock_smart_tools_empty):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools_empty)
        return self.registered_tools["ha_search_entities"]

    @pytest.mark.asyncio
    async def test_area_plus_query_branch_projects(self, search_tool_populated):
        """area+query branch (line 479) honours fields= projection."""
        result = await search_tool_populated(
            query="kitchen", area_filter="kitchen", fields=["results"]
        )
        data = result["data"]
        # Only ``results`` (+ always-retained ``success``) should remain.
        assert "results" in data
        assert "success" in data
        assert "total_matches" not in data
        assert "search_type" not in data
        assert "area_filter" not in data

    @pytest.mark.asyncio
    async def test_area_plus_query_branch_unprojected_baseline(
        self, search_tool_populated
    ):
        """fields=None on area+query returns the full response (sanity check)."""
        result = await search_tool_populated(query="kitchen", area_filter="kitchen")
        data = result["data"]
        assert "results" in data
        assert "search_type" in data
        assert data["search_type"] == "area_filtered_query"

    @pytest.mark.asyncio
    async def test_area_only_populated_branch_projects(self, search_tool_populated):
        """area-only populated branch (line 581) honours fields= projection."""
        result = await search_tool_populated(area_filter="kitchen", fields=["results"])
        data = result["data"]
        assert "results" in data
        assert "success" in data
        assert "area_names" not in data
        assert "search_type" not in data

    @pytest.mark.asyncio
    async def test_area_only_populated_branch_unprojected_baseline(
        self, search_tool_populated
    ):
        """fields=None on area-only returns the full response (sanity check)."""
        result = await search_tool_populated(area_filter="kitchen")
        data = result["data"]
        assert "results" in data
        assert "search_type" in data
        assert data["search_type"] == "area_only"
        assert "area_names" in data

    @pytest.mark.asyncio
    async def test_area_only_empty_branch_projects(self, search_tool_empty):
        """area-only empty branch (line 600) honours fields= projection.

        Selecting ``message`` (set on the zero-match branch) confirms the
        projection is applied to the empty-area response shape too.
        """
        result = await search_tool_empty(area_filter="nonexistent", fields=["message"])
        data = result["data"]
        assert "success" in data
        assert "message" in data
        assert "results" not in data
        assert "search_type" not in data

    @pytest.mark.asyncio
    async def test_area_only_empty_branch_unprojected_baseline(self, search_tool_empty):
        """fields=None on the empty-area branch returns the full response."""
        result = await search_tool_empty(area_filter="nonexistent")
        data = result["data"]
        assert "results" in data
        assert data["results"] == []
        assert "message" in data

    @pytest.mark.asyncio
    async def test_domain_listing_branch_projects(self, search_tool_populated):
        """domain-listing branch (line 694) honours fields= projection.

        Triggered by empty query + domain_filter. The smart_tools mock is
        unused on this branch; client.get_states drives the result.
        """
        result = await search_tool_populated(domain_filter="light", fields=["results"])
        data = result["data"]
        assert "results" in data
        assert "success" in data
        assert "note" not in data
        assert "search_type" not in data

    @pytest.mark.asyncio
    async def test_domain_listing_branch_unprojected_baseline(
        self, search_tool_populated
    ):
        """fields=None on the domain-listing branch returns the full response."""
        result = await search_tool_populated(domain_filter="light")
        data = result["data"]
        assert "results" in data
        assert "search_type" in data
        assert data["search_type"] == "domain_listing"
        assert "note" in data


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
    """Shared fixture mixin for ha_search_entities tests."""

    @pytest.fixture
    def mock_mcp(self):
        mcp = MagicMock()
        self.registered_tools: dict = {}

        def tool_decorator(*args, **kwargs):
            def wrapper(func):
                self.registered_tools[func.__name__] = func
                return func

            return wrapper

        mcp.tool = tool_decorator
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
        return MagicMock()

    @pytest.fixture
    def search_tool(self, mock_mcp, mock_client, mock_smart_tools):
        register_search_tools(mock_mcp, mock_client, smart_tools=mock_smart_tools)
        return self.registered_tools["ha_search_entities"]


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
        by_domain = result["data"].get("by_domain", {})
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
        data = result["data"]
        # Results still present; no by_domain grouping
        assert "results" in data
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
        by_domain = result["data"].get("by_domain", {})
        assert "light" in by_domain
        assert len(by_domain["light"]) <= 1


class TestHaSearchEntitiesStateFilter(_SearchToolFixture):
    """Tests for state_filter= normalization and per-branch behavior (issue #1199)."""

    @pytest.mark.asyncio
    async def test_state_filter_exact_match_keeps_matching_entities(self, search_tool):
        """state_filter='on' in exact_match mode keeps only 'on' entities."""
        result = await search_tool(query="light", state_filter="on")
        data = result["data"]
        for entity in data["results"]:
            assert entity["state"] == "on", (
                "state_filter='on' should remove non-'on' entities from results"
            )

    @pytest.mark.asyncio
    async def test_state_filter_strips_surrounding_whitespace(self, search_tool):
        """state_filter with whitespace padding is normalized before matching."""
        result_padded = await search_tool(query="light", state_filter="  on  ")
        result_plain = await search_tool(query="light", state_filter="on")
        # Both should return the same set of entities
        padded_ids = {e["entity_id"] for e in result_padded["data"]["results"]}
        plain_ids = {e["entity_id"] for e in result_plain["data"]["results"]}
        assert padded_ids == plain_ids

    @pytest.mark.asyncio
    async def test_state_filter_echoed_in_response(self, search_tool):
        """state_filter value (after strip) is echoed back in the data dict."""
        result = await search_tool(query="light", state_filter="on")
        assert result["data"]["state_filter"] == "on"

    @pytest.mark.asyncio
    async def test_state_filter_domain_listing_branch(self, search_tool):
        """state_filter works in the domain_listing branch (empty query + domain_filter)."""
        result = await search_tool(domain_filter="light", state_filter="on")
        data = result["data"]
        for entity in data["results"]:
            assert entity["state"] == "on"
        assert data.get("state_filter") == "on"

    @pytest.mark.asyncio
    async def test_state_filter_whitespace_only_treated_as_no_filter(self, search_tool):
        """state_filter='   ' (whitespace only) is treated as no filter (None)."""
        result_no_filter = await search_tool(query="light")
        result_ws_filter = await search_tool(query="light", state_filter="   ")
        no_filter_count = result_no_filter["data"]["count"]
        ws_filter_count = result_ws_filter["data"]["count"]
        # Both should return the same number of results (no filtering applied)
        assert ws_filter_count == no_filter_count


class TestHaSearchEntitiesResultFields(_SearchToolFixture):
    """Tests for result_fields= per-record projection (issue #1199)."""

    @pytest.mark.asyncio
    async def test_result_fields_projects_entity_records(self, search_tool):
        """result_fields=['entity_id','state'] limits each record to those keys."""
        result = await search_tool(query="light", result_fields=["entity_id", "state"])
        data = result["data"]
        for entity in data["results"]:
            assert set(entity.keys()) == {"entity_id", "state"}

    @pytest.mark.asyncio
    async def test_result_fields_outer_response_keys_preserved(self, search_tool):
        """result_fields only projects inside results[]; top-level keys are unchanged."""
        result = await search_tool(query="light", result_fields=["entity_id"])
        data = result["data"]
        assert "success" in data
        assert "total_matches" in data
        assert "count" in data

    @pytest.mark.asyncio
    async def test_result_fields_unknown_key_emits_warning(self, search_tool):
        """result_fields with only unknown keys emits a diagnostic warning."""
        result = await search_tool(query="light", result_fields=["nonexistent_key"])
        data = result["data"]
        # Each entity record is projected to {} since the key doesn't exist
        for entity in data["results"]:
            assert entity == {}
        # A diagnostic warning should be present
        assert "warnings" in data
        assert any("nonexistent_key" in w for w in data["warnings"])

    @pytest.mark.asyncio
    async def test_result_fields_domain_listing_branch(self, search_tool):
        """result_fields works in the domain_listing branch."""
        result = await search_tool(domain_filter="light", result_fields=["entity_id"])
        data = result["data"]
        for entity in data["results"]:
            assert set(entity.keys()) == {"entity_id"}


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
        return smart

    @pytest.mark.asyncio
    async def test_fuzzy_state_filter_total_matches_is_unfiltered(self, search_tool):
        """total_matches stays at the unfiltered fuzzy count; count is post-filter.

        This pins the dual-count contract: the fuzzy engine already paginated
        internally so total_matches cannot be recomputed after state filtering.
        """
        result = await search_tool(query="light", exact_match=False, state_filter="on")
        data = result["data"]
        # count reflects only the filtered page
        assert data["count"] == 1, "count should reflect only the on-state entity"
        assert data["results"][0]["entity_id"] == "light.kitchen"
        # total_matches is the raw fuzzy-engine number, not re-counted
        assert data["total_matches"] == 5, (
            "total_matches must remain the unfiltered fuzzy count"
        )

    @pytest.mark.asyncio
    async def test_fuzzy_state_filter_note_present(self, search_tool):
        """state_filter_note appears in the response to explain the dual-count."""
        result = await search_tool(query="light", exact_match=False, state_filter="on")
        data = result["data"]
        assert "state_filter_note" in data
        assert "has_more" in data["state_filter_note"]

    @pytest.mark.asyncio
    async def test_state_filter_note_survives_fields_projection(self, search_tool):
        """state_filter_note is force-retained even when not in fields=.

        A caller with fields=["results", "total_matches"] still needs the note
        to understand that total_matches is the unfiltered count.
        """
        result = await search_tool(
            query="light",
            exact_match=False,
            state_filter="on",
            fields=["results", "total_matches"],
        )
        data = result["data"]
        # state_filter_note must be force-retained even when not listed in fields=
        assert "state_filter_note" in data, (
            "state_filter_note must survive fields= projection"
        )
        # Projected keys present
        assert "total_matches" in data
        assert "results" in data
        # Non-requested keys absent (count was not in fields=)
        assert "count" not in data
