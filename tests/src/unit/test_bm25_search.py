"""Unit tests for BM25-based fuzzy search (issue #851).

Tests the BM25Scorer class, tokenizer, FuzzyEntitySearcher BM25 integration,
and the BM25 path in SmartSearchTools._search_in_dict.
"""

import pytest

from ha_mcp.utils.fuzzy_search import BM25Scorer, FuzzyEntitySearcher, tokenize

# ---------------------------------------------------------------------------
# tokenize()
# ---------------------------------------------------------------------------


class TestTokenize:
    def test_entity_id_splits_on_dot_and_underscore(self):
        assert tokenize("light.kitchen_ceiling") == ["light", "kitchen", "ceiling"]

    def test_friendly_name_splits_on_spaces(self):
        assert tokenize("Kitchen Ceiling Light") == ["kitchen", "ceiling", "light"]

    def test_mixed_delimiters(self):
        assert tokenize("sensor.living_room-temp 2") == [
            "sensor",
            "living",
            "room",
            "temp",
            "2",
        ]

    def test_empty_string(self):
        assert tokenize("") == []

    def test_single_token(self):
        assert tokenize("light") == ["light"]


# ---------------------------------------------------------------------------
# BM25Scorer
# ---------------------------------------------------------------------------


class TestBM25Scorer:
    @pytest.fixture
    def simple_corpus(self):
        return [
            ["kitchen", "ceiling", "light"],
            ["living", "room", "light"],
            ["kitchen", "temperature", "sensor"],
            ["bedroom", "light"],
            ["garage", "door"],
        ]

    def test_fit_builds_idf(self, simple_corpus):
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        # "light" appears in 3/5 docs, "kitchen" in 2/5, "garage" in 1/5
        assert scorer._idf["kitchen"] > scorer._idf["light"]
        assert scorer._idf["garage"] > scorer._idf["kitchen"]

    def test_score_prefers_rare_term(self, simple_corpus):
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        # "kitchen light" should rank the kitchen ceiling light higher than
        # living room light because "kitchen" is rarer than "light"
        scores = scorer.score_all(["kitchen", "light"])
        kitchen_ceiling_score = scores[0]
        living_room_score = scores[1]
        assert kitchen_ceiling_score > living_room_score

    def test_score_zero_for_no_match(self, simple_corpus):
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        scores = scorer.score_all(["nonexistent"])
        assert all(s == 0.0 for s in scores)

    def test_multi_word_non_adjacent(self, simple_corpus):
        """BM25 finds documents where query terms exist but are not adjacent.
        This is the 'dryer override' case from issue #851."""
        scorer = BM25Scorer()
        scorer.fit(simple_corpus)
        # "kitchen sensor" — terms exist in doc 2 but not adjacent
        score = scorer.score(["kitchen", "sensor"], 2)
        assert score > 0

    def test_empty_corpus(self):
        scorer = BM25Scorer()
        scorer.fit([])
        assert scorer.score_all(["test"]) == []

    def test_single_doc(self):
        scorer = BM25Scorer()
        scorer.fit([["hello", "world"]])
        scores = scorer.score_all(["hello"])
        assert scores[0] > 0


# ---------------------------------------------------------------------------
# FuzzyEntitySearcher with BM25
# ---------------------------------------------------------------------------


class TestFuzzyEntitySearcherBM25:
    @pytest.fixture
    def entities(self):
        return [
            {
                "entity_id": "light.kitchen_ceiling",
                "attributes": {"friendly_name": "Kitchen Ceiling Light"},
                "state": "on",
            },
            {
                "entity_id": "light.living_room",
                "attributes": {"friendly_name": "Living Room Light"},
                "state": "on",
            },
            {
                "entity_id": "sensor.kitchen_temperature",
                "attributes": {"friendly_name": "Kitchen Temperature"},
                "state": "22.5",
            },
            {
                "entity_id": "light.bedroom",
                "attributes": {"friendly_name": "Bedroom Light"},
                "state": "off",
            },
            {
                "entity_id": "binary_sensor.garage_door",
                "attributes": {"friendly_name": "Garage Door"},
                "state": "closed",
            },
        ]

    def test_multi_word_query_ranks_correctly(self, entities):
        """'kitchen light' should rank kitchen ceiling light first."""
        searcher = FuzzyEntitySearcher(threshold=30)
        results, total = searcher.search_entities(entities, "kitchen light", limit=5)
        assert total > 0
        assert results[0]["entity_id"] == "light.kitchen_ceiling"

    def test_single_word_query(self, entities):
        searcher = FuzzyEntitySearcher(threshold=30)
        results, total = searcher.search_entities(entities, "garage", limit=5)
        assert total >= 1
        assert any(r["entity_id"] == "binary_sensor.garage_door" for r in results)

    def test_no_match_returns_empty(self, entities):
        """BM25 should return 0 results for completely unrelated query."""
        searcher = FuzzyEntitySearcher(threshold=30)
        results, total = searcher.search_entities(
            entities, "microcontroller zebra", limit=5
        )
        assert total == 0
        assert results == []

    def test_empty_query_returns_empty(self, entities):
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(entities, "", limit=5)
        assert total == 0

    def test_empty_entities_returns_empty(self):
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities([], "kitchen", limit=5)
        assert total == 0

    def test_pagination(self, entities):
        searcher = FuzzyEntitySearcher(threshold=30)
        results_p1, total = searcher.search_entities(
            entities, "light", limit=2, offset=0
        )
        results_p2, _ = searcher.search_entities(
            entities, "light", limit=2, offset=2
        )
        # Pages should not overlap
        ids_p1 = {r["entity_id"] for r in results_p1}
        ids_p2 = {r["entity_id"] for r in results_p2}
        assert not ids_p1.intersection(ids_p2)

    def test_typo_fallback(self, entities):
        """Slight typo should still find results via SequenceMatcher fallback."""
        searcher = FuzzyEntitySearcher(threshold=30)
        results, total = searcher.search_entities(entities, "kitchn", limit=5)
        # "kitchn" is close to "kitchen" — typo fallback should catch it
        assert total > 0

    def test_underscore_space_equivalence(self, entities):
        """'tesla_ble' and 'tesla ble' should return the same results (unified tokenization)."""
        extra_entities = [
            {
                "entity_id": "number.tesla_ble_charging_amps",
                "attributes": {"friendly_name": "Tesla BLE Charging Amps"},
                "state": "16",
            },
        ]
        searcher = FuzzyEntitySearcher(threshold=30)
        results_underscore, total_u = searcher.search_entities(
            extra_entities, "tesla_ble", limit=5
        )
        results_space, total_s = searcher.search_entities(
            extra_entities, "tesla ble", limit=5
        )
        assert total_u == total_s
        assert results_underscore[0]["entity_id"] == results_space[0]["entity_id"]


# ---------------------------------------------------------------------------
# _search_in_dict BM25 path (via SmartSearchTools)
# ---------------------------------------------------------------------------


class TestSearchInDictBM25:
    """Test the BM25 fuzzy path in SmartSearchTools._search_in_dict."""

    @pytest.fixture
    def smart_tools(self):
        from unittest.mock import AsyncMock

        from ha_mcp.tools.smart_search import SmartSearchTools

        mock_client = AsyncMock()
        mock_client.get_states = AsyncMock(return_value=[])
        return SmartSearchTools(client=mock_client, fuzzy_threshold=60)

    def test_multi_word_finds_non_adjacent_terms(self, smart_tools):
        """The 'dryer override' case: terms exist but not adjacent."""
        config = {
            "alias": "Tesla Mobile Connector Dryer Load Sharing",
            "trigger": [{"entity_id": "sensor.dryer_power"}],
            "action": [
                {"service": "input_boolean.toggle"},
                {"entity_id": "input_boolean.emporia_vehicle_tesla_override"},
            ],
        }
        score = smart_tools._search_in_dict(config, "dryer override", exact_match=False)
        assert score > 0

    def test_exact_match_requires_contiguous_substring(self, smart_tools):
        """Exact match: 'dryer override' is NOT a contiguous substring."""
        config = {
            "alias": "Tesla Mobile Connector Dryer Load Sharing",
            "trigger": [{"entity_id": "sensor.dryer_power"}],
            "action": [
                {"entity_id": "input_boolean.emporia_vehicle_tesla_override"},
            ],
        }
        score = smart_tools._search_in_dict(config, "dryer override", exact_match=True)
        assert score == 0

    def test_exact_match_finds_contiguous_substring(self, smart_tools):
        config = {"alias": "Turn on dryer override mode"}
        score = smart_tools._search_in_dict(config, "dryer override", exact_match=True)
        assert score == 100

    def test_fuzzy_empty_data(self, smart_tools):
        assert smart_tools._search_in_dict({}, "test", exact_match=False) == 0

    def test_fuzzy_nested_structure(self, smart_tools):
        config = {
            "trigger": [
                {
                    "platform": "state",
                    "entity_id": "binary_sensor.kitchen_motion",
                }
            ],
            "action": [
                {"service": "light.turn_on", "target": {"entity_id": "light.kitchen"}}
            ],
        }
        score = smart_tools._search_in_dict(
            config, "kitchen motion", exact_match=False
        )
        assert score > 0
