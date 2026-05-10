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

    def test_production_threshold_passes_full_match(self, entities):
        """A match containing all query tokens must pass the production threshold (60)."""
        searcher = FuzzyEntitySearcher(threshold=60)
        results, total = searcher.search_entities(
            entities, "kitchen ceiling", limit=5
        )
        assert total > 0, (
            "Full token match ('kitchen' + 'ceiling') must survive threshold=60 "
            "under absolute IDF-based normalization"
        )
        assert results[0]["entity_id"] == "light.kitchen_ceiling"

    def test_production_threshold_filters_partial_match(self, entities):
        """A query sharing only a common token should not dominate at threshold=60."""
        searcher = FuzzyEntitySearcher(threshold=60)
        # "light nonexistent" only matches on the very common 'light' token —
        # with absolute normalization, a half-match of a common term should
        # score well below 60.
        results, _ = searcher.search_entities(
            entities, "light nonexistent", limit=5
        )
        # Either zero results or only those where 'light' carries enough IDF
        # weight — no noise floor of 100 from empirical normalization.
        assert all(r["score"] < 100 for r in results), (
            "Partial match on common token should not be normalized to 100"
        )

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


# ---------------------------------------------------------------------------
# Issue #1170 — fuzzy_search.py algorithmic regression tests
# ---------------------------------------------------------------------------


class TestFuzzySearcherIssue1170:
    """Lock down the fuzzy-search behavior changes from #1170 findings 2/5/8."""

    @pytest.fixture
    def lights_corpus(self):
        return [
            {
                "entity_id": "light.bed_light",
                "attributes": {"friendly_name": "Bed Light"},
                "state": "off",
            },
            {
                "entity_id": "light.ceiling_lights",
                "attributes": {"friendly_name": "Ceiling Lights"},
                "state": "off",
            },
            {
                "entity_id": "light.kitchen_lights",
                "attributes": {"friendly_name": "Kitchen Lights"},
                "state": "off",
            },
            {
                "entity_id": "cover.garage_door",
                "attributes": {"friendly_name": "Garage Door"},
                "state": "closed",
            },
        ]

    def test_finding_2_elided_separator_query_finds_target_uniquely(
        self, lights_corpus
    ):
        """Query ``bedlight`` (no separator) matches only ``light.bed_light``,
        not the entire 3-light tie cluster pre-fix saw at score 76.

        Implementation: separator-stripped concat tokens are added to the
        BM25 corpus so a single-token query can match a multi-word entity
        name directly, with high IDF (rare token = strong signal).
        """
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(lights_corpus, "bedlight", limit=10)
        assert total == 1, (
            f"bedlight should uniquely match bed_light, not tie-cluster: {results}"
        )
        assert results[0]["entity_id"] == "light.bed_light"
        assert results[0]["score"] >= 75, (
            f"score should remain useful after fix: {results[0]}"
        )

    def test_finding_5_multi_token_garbage_rejected(self, lights_corpus):
        """A 3-token nonsense query where only one token grazes a doc token
        does NOT surface that doc.

        Pre-fix: ``xyz_irrelevant_garbage`` returned ``cover.garage_door`` at
        score 92 because typo_fallback compared each query token against
        each doc token and a single ``garbage~garage`` ratio of 92 was
        enough.

        Post-fix: typo_fallback requires multi-token coverage ≥50% on
        multi-token queries.
        """
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(
            lights_corpus, "xyz_irrelevant_garbage", limit=10
        )
        assert total == 0, (
            f"low-coverage garbage query must yield no matches: {results}"
        )

    def test_finding_5_single_token_typo_still_recalls(self, lights_corpus):
        """Single-token typos like ``ligth`` still recall (no coverage gate
        on single-token queries — that would be too aggressive)."""
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(lights_corpus, "ligth", limit=10)
        # Should still find a real light.* entity via typo_fallback — not
        # just *something*.
        assert total >= 1, f"single-token typo recall regressed: {results}"
        assert any(r["entity_id"].startswith("light.") for r in results), (
            f"coverage gate falsely rejected legitimate single-token typo: "
            f"{[r['entity_id'] for r in results]}"
        )

    def test_finding_8_alias_search_with_match_type_label(self, lights_corpus):
        """Aliases are searchable when the caller supplies them on the
        entity dict via ``_aliases``. Matches surface as
        ``match_type='alias_match'`` so callers can distinguish."""
        # Add an alias ONLY known via the `_aliases` field — neither
        # entity_id nor friendly_name contain "lullaby".
        enriched = []
        for e in lights_corpus:
            e2 = dict(e)
            if e2["entity_id"] == "light.bed_light":
                e2["_aliases"] = ["lullaby lamp"]
            enriched.append(e2)
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(enriched, "lullaby", limit=10)
        assert total == 1, (
            f"alias should be searchable as a query token: {results}"
        )
        assert results[0]["entity_id"] == "light.bed_light"
        assert results[0]["match_type"] == "alias_match", (
            f"alias-driven match should be labeled alias_match: {results[0]}"
        )

    def test_finding_8_no_alias_no_match(self, lights_corpus):
        """Without ``_aliases``, an alias-only query yields no result."""
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(
            lights_corpus, "lullaby", limit=10
        )
        assert total == 0, f"no alias data → no match: {results}"

    def test_finding_8_alias_does_not_overshadow_name_match(self, lights_corpus):
        """When a query token matches BOTH the friendly_name and an alias,
        the result keeps a name-driven match_type rather than mislabeling
        as ``alias_match``. A future code change that swapped the
        precedence (or used a too-broad set intersection) would surface
        as a confused match_type for queries that obviously matched on
        the entity's primary identity."""
        # alias contains "bed" (which also tokenizes from "Bed Light")
        # plus a unique alias-only token "lullaby".
        enriched = []
        for e in lights_corpus:
            e2 = dict(e)
            if e2["entity_id"] == "light.bed_light":
                e2["_aliases"] = ["bed lullaby"]
            enriched.append(e2)
        searcher = FuzzyEntitySearcher()
        results, total = searcher.search_entities(enriched, "bed", limit=10)
        assert total >= 1
        target = next(r for r in results if r["entity_id"] == "light.bed_light")
        # "bed" is a substring of "light.bed_light", so _get_match_type
        # returns "partial_id". Asserting the specific value (not just
        # !="alias_match") locks in the precedence: id/name path wins
        # over alias_match labeling whenever the query token has *any*
        # primary-identity hit.
        assert target["match_type"] == "partial_id", (
            f"query 'bed' should be labeled partial_id (entity_id contains "
            f"'bed'), got: {target}"
        )


class TestSmartEntitySearchPropagation:
    """Lock down the finding-6 fix at the service layer: a fatal
    ``get_states`` failure must propagate as a ``ToolError`` rather
    than being swallowed into a zero-result dump.

    The pre-fix code masked auth/connection failures by emitting a
    ``partial_listing`` of every entity at score 0 with
    ``partial: True`` — agents that read ``success: True`` would
    silently accept the noise pile. After the fix, the failure surfaces
    as ``isError=true`` so the caller can act on it.
    """

    @pytest.mark.asyncio
    async def test_get_states_failure_propagates_as_tool_error(self):
        from unittest.mock import AsyncMock

        from fastmcp.exceptions import ToolError

        from ha_mcp.tools.smart_search import SmartSearchTools

        mock_client = AsyncMock()
        mock_client.get_states = AsyncMock(
            side_effect=RuntimeError("simulated transport failure")
        )
        mock_client.send_websocket_message = AsyncMock(
            return_value={"success": True, "result": {}}
        )
        smart_tools = SmartSearchTools(client=mock_client, fuzzy_threshold=60)

        with pytest.raises(ToolError):
            await smart_tools.smart_entity_search("anything", limit=5)
