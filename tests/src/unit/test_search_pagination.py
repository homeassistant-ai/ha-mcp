"""Unit tests for offset pagination in search tools (issue #605).

Tests that search functions correctly support offset-based pagination
with standardized metadata: total_matches, offset, limit, count, has_more, next_offset.
Also includes regression test for suggestions bug fix.
"""

import pytest

from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import _exact_match_search, _partial_results_search
from ha_mcp.utils.fuzzy_search import (
    FuzzyEntitySearcher,
    calculate_partial_ratio,
    calculate_ratio,
    calculate_token_sort_ratio,
)


class MockClient:
    """Mock Home Assistant client for testing."""

    def __init__(self, entities: list[dict]):
        self.entities = entities

    async def get_states(self) -> list[dict]:
        return self.entities


def _make_entities(count: int) -> list[dict]:
    """Generate a list of light entities for testing."""
    return [
        {
            "entity_id": f"light.room_{i}",
            "attributes": {"friendly_name": f"Room {i} Light"},
            "state": "on" if i % 2 == 0 else "off",
        }
        for i in range(count)
    ]


PAGINATION_FIELDS = {"total_matches", "offset", "limit", "count", "has_more", "next_offset"}


class TestFuzzySearchPagination:
    """Test FuzzyEntitySearcher.search_entities offset pagination."""

    def setup_method(self):
        self.searcher = FuzzyEntitySearcher(threshold=0)  # threshold=0 to match everything

    def test_offset_zero_returns_first_page(self):
        entities = _make_entities(10)
        results, total = self.searcher.search_entities(entities, "light", limit=3, offset=0)
        assert len(results) == 3
        assert total == 10

    def test_offset_skips_results(self):
        entities = _make_entities(10)
        page1, total1 = self.searcher.search_entities(entities, "light", limit=3, offset=0)
        page2, total2 = self.searcher.search_entities(entities, "light", limit=3, offset=3)

        assert total1 == total2 == 10
        assert len(page1) == 3
        assert len(page2) == 3
        # Pages should not overlap
        ids1 = {r["entity_id"] for r in page1}
        ids2 = {r["entity_id"] for r in page2}
        assert ids1.isdisjoint(ids2)

    def test_offset_beyond_results_returns_empty(self):
        entities = _make_entities(5)
        results, total = self.searcher.search_entities(entities, "light", limit=3, offset=100)
        assert len(results) == 0
        assert total == 5

    def test_offset_near_end_returns_partial_page(self):
        entities = _make_entities(10)
        results, total = self.searcher.search_entities(entities, "light", limit=5, offset=8)
        assert len(results) == 2
        assert total == 10

    def test_default_offset_is_zero(self):
        entities = _make_entities(5)
        results_default, total_default = self.searcher.search_entities(entities, "light", limit=3)
        results_explicit, total_explicit = self.searcher.search_entities(entities, "light", limit=3, offset=0)
        assert [r["entity_id"] for r in results_default] == [r["entity_id"] for r in results_explicit]
        assert total_default == total_explicit

    def test_full_pagination_covers_all_results(self):
        """Paginating through all results yields every match exactly once."""
        entities = _make_entities(7)
        all_ids: list[str] = []
        offset = 0
        limit = 3
        while True:
            results, total = self.searcher.search_entities(entities, "light", limit=limit, offset=offset)
            if not results:
                break
            all_ids.extend(r["entity_id"] for r in results)
            offset += limit

        assert len(all_ids) == 7
        assert len(set(all_ids)) == 7  # no duplicates


class TestExactMatchSearchPagination:
    """Test _exact_match_search offset pagination."""

    @pytest.fixture
    def many_lights(self):
        return _make_entities(10)

    @pytest.mark.asyncio
    async def test_offset_skips_results(self, many_lights):
        client = MockClient(many_lights)
        page1 = await _exact_match_search(client, "light", None, 3, offset=0)
        page2 = await _exact_match_search(client, "light", None, 3, offset=3)

        assert page1["count"] == 3
        assert page2["count"] == 3
        ids1 = {r["entity_id"] for r in page1["results"]}
        ids2 = {r["entity_id"] for r in page2["results"]}
        assert ids1.isdisjoint(ids2)

    @pytest.mark.asyncio
    async def test_pagination_metadata(self, many_lights):
        client = MockClient(many_lights)
        result = await _exact_match_search(client, "light", None, 3, offset=0)

        assert PAGINATION_FIELDS <= result.keys()
        assert result["total_matches"] == 10
        assert result["offset"] == 0
        assert result["limit"] == 3
        assert result["count"] == 3
        assert result["has_more"] is True
        assert result["next_offset"] == 3

    @pytest.mark.asyncio
    async def test_last_page_metadata(self, many_lights):
        client = MockClient(many_lights)
        result = await _exact_match_search(client, "light", None, 3, offset=9)

        assert result["count"] == 1
        assert result["has_more"] is False
        assert result["next_offset"] is None

    @pytest.mark.asyncio
    async def test_offset_beyond_total(self, many_lights):
        client = MockClient(many_lights)
        result = await _exact_match_search(client, "light", None, 3, offset=100)

        assert result["count"] == 0
        assert result["has_more"] is False
        assert result["next_offset"] is None
        assert result["total_matches"] == 10


class TestPartialResultsSearchPagination:
    """Test _partial_results_search offset pagination."""

    @pytest.fixture
    def many_entities(self):
        return _make_entities(10)

    @pytest.mark.asyncio
    async def test_offset_skips_results(self, many_entities):
        client = MockClient(many_entities)
        page1 = await _partial_results_search(client, "anything", None, 3, offset=0)
        page2 = await _partial_results_search(client, "anything", None, 3, offset=3)

        assert page1["count"] == 3
        assert page2["count"] == 3
        ids1 = {r["entity_id"] for r in page1["results"]}
        ids2 = {r["entity_id"] for r in page2["results"]}
        assert ids1.isdisjoint(ids2)

    @pytest.mark.asyncio
    async def test_pagination_metadata(self, many_entities):
        client = MockClient(many_entities)
        result = await _partial_results_search(client, "anything", None, 3, offset=0)

        assert PAGINATION_FIELDS <= result.keys()
        assert result["total_matches"] == 10
        assert result["offset"] == 0
        assert result["limit"] == 3
        assert result["count"] == 3
        assert result["has_more"] is True
        assert result["next_offset"] == 3

    @pytest.mark.asyncio
    async def test_last_page_has_more_false(self, many_entities):
        client = MockClient(many_entities)
        result = await _partial_results_search(client, "anything", None, 5, offset=5)

        assert result["count"] == 5
        assert result["has_more"] is False
        assert result["next_offset"] is None


class TestFuzzyScoreAccumulation:
    """Regression test: weighted scores must accumulate as floats before flooring.

    int(a*0.7) + int(b*0.8) can lose points vs int(a*0.7 + b*0.8).
    E.g. int(10.6) + int(5.6) = 15, but int(10.6 + 5.6) = 16.
    """

    def test_weighted_scores_floor_once(self):
        """Verify scoring floors the weighted sum once, not per-component."""
        searcher = FuzzyEntitySearcher(threshold=0)

        entity_id = "light.test"
        friendly_name = "Test Light"
        domain = "light"
        query = "test"

        score = searcher._calculate_entity_score(entity_id, friendly_name, domain, query)

        # Recompute expected score with single-floor accumulation
        base = 0
        if query in entity_id.lower():
            base += 85
        if query in friendly_name.lower():
            base += 80

        er = calculate_ratio(query, entity_id.lower())
        fr = calculate_ratio(query, friendly_name.lower())
        dr = calculate_ratio(query, domain.lower())
        ep = calculate_partial_ratio(query, entity_id.lower())
        fp = calculate_partial_ratio(query, friendly_name.lower())
        et = calculate_token_sort_ratio(query, entity_id.lower())
        ft = calculate_token_sort_ratio(query, friendly_name.lower())

        weighted = (
            max(er, ep, et) * 0.7
            + max(fr, fp, ft) * 0.8
            + dr * 0.6
        )
        expected = base + int(weighted)

        assert score == expected, (
            f"Score {score} != expected {expected}; "
            f"weighted components should be floored once, not per-step"
        )


class TestSmartEntitySearchSuggestions:
    """Regression test: suggestions should be returned when search quality is poor."""

    @pytest.mark.asyncio
    async def test_suggestions_returned_for_no_matches(self):
        """When query matches nothing, response should include suggestions."""
        entities = _make_entities(5)
        client = MockClient(entities)
        smart = SmartSearchTools(client=client, fuzzy_threshold=60)

        result = await smart.smart_entity_search("xyznonexistent999")

        assert result["success"] is True
        assert result["total_matches"] == 0
        assert "suggestions" in result, "suggestions should be present when no matches found"
        assert isinstance(result["suggestions"], list)
        assert len(result["suggestions"]) > 0

