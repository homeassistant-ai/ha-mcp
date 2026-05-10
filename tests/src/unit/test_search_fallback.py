"""Unit tests for search fallback functionality (issue #214).

Tests the graceful degradation search method:
- _exact_match_search: Fallback exact substring matching

The legacy ``_partial_results_search`` was removed in #1170 (finding 6).
Its behavior was a useless score-0 entity dump that masked errors;
exceptions now propagate to callers instead.
"""


import pytest

from ha_mcp.tools.tools_search import _exact_match_search


class MockClient:
    """Mock Home Assistant client for testing.

    ``_exact_match_search`` now also calls ``send_websocket_message``
    to fetch the entity registry for the hidden_by filter (#1170 finding
    9). The mock returns an empty success response, which is treated as
    "no entities are hidden" by the fallback.
    """

    def __init__(self, entities: list[dict]):
        self.entities = entities

    async def get_states(self) -> list[dict]:
        return self.entities

    async def send_websocket_message(self, payload: dict) -> dict:
        return {"success": True, "result": []}


class TestExactMatchSearch:
    """Test _exact_match_search fallback function."""

    @pytest.fixture
    def sample_entities(self):
        """Sample entities for testing."""
        return [
            {
                "entity_id": "light.living_room",
                "attributes": {"friendly_name": "Living Room Light"},
                "state": "on",
            },
            {
                "entity_id": "light.bedroom",
                "attributes": {"friendly_name": "Bedroom Light"},
                "state": "off",
            },
            {
                "entity_id": "switch.kitchen",
                "attributes": {"friendly_name": "Kitchen Switch"},
                "state": "on",
            },
            {
                "entity_id": "sensor.temperature",
                "attributes": {"friendly_name": "Temperature Sensor"},
                "state": "22.5",
            },
        ]

    @pytest.mark.asyncio
    async def test_exact_match_finds_entity_id_substring(self, sample_entities):
        """Exact match finds entities by entity_id substring."""
        client = MockClient(sample_entities)
        result = await _exact_match_search(client, "living", None, 10)

        assert result["success"] is True
        assert result["search_type"] == "exact_match"
        assert len(result["results"]) == 1
        assert result["results"][0]["entity_id"] == "light.living_room"
        assert result["results"][0]["match_type"] == "exact_match"

    @pytest.mark.asyncio
    async def test_exact_match_finds_friendly_name_substring(self, sample_entities):
        """Exact match finds entities by friendly_name substring."""
        client = MockClient(sample_entities)
        result = await _exact_match_search(client, "bedroom", None, 10)

        assert result["success"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["entity_id"] == "light.bedroom"

    @pytest.mark.asyncio
    async def test_exact_match_case_insensitive(self, sample_entities):
        """Exact match is case insensitive."""
        client = MockClient(sample_entities)
        result = await _exact_match_search(client, "LIVING", None, 10)

        assert result["success"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["entity_id"] == "light.living_room"

    @pytest.mark.asyncio
    async def test_exact_match_with_domain_filter(self, sample_entities):
        """Exact match respects domain_filter."""
        client = MockClient(sample_entities)
        # "light" appears in multiple entity types, but filter to switches
        result = await _exact_match_search(client, "kitchen", "switch", 10)

        assert result["success"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["entity_id"] == "switch.kitchen"
        assert result["results"][0]["domain"] == "switch"

    @pytest.mark.asyncio
    async def test_exact_match_no_results(self, sample_entities):
        """Exact match returns empty results for non-matching query."""
        client = MockClient(sample_entities)
        result = await _exact_match_search(client, "nonexistent", None, 10)

        assert result["success"] is True
        assert len(result["results"]) == 0
        assert result["total_matches"] == 0

    @pytest.mark.asyncio
    async def test_exact_match_respects_limit(self, sample_entities):
        """Exact match respects the limit parameter."""
        client = MockClient(sample_entities)
        # "light" appears in multiple entities
        result = await _exact_match_search(client, "light", None, 1)

        assert result["success"] is True
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_exact_match_perfect_match_higher_score(self, sample_entities):
        """Perfect matches have higher score than partial matches."""
        client = MockClient(sample_entities)
        result = await _exact_match_search(client, "light", None, 10)

        assert result["success"] is True
        # Results should be sorted by score
        scores = [r["score"] for r in result["results"]]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_exact_match_includes_hidden_with_penalty_by_default(
        self, sample_entities
    ):
        """Hidden entities surface in default results with a score
        penalty (option c from issue #1170 finding 9). The penalty
        ensures visible matches sort above hidden ones without
        excluding the hidden entries entirely.
        """

        class HidingClient(MockClient):
            async def send_websocket_message(self, payload):
                return {
                    "success": True,
                    "result": [
                        {"entity_id": "light.bedroom", "hidden_by": "user"},
                    ],
                }

        client = HidingClient(sample_entities)
        result = await _exact_match_search(client, "bedroom", None, 10)
        by_id = {r["entity_id"]: r for r in result["results"]}
        assert "light.bedroom" in by_id, (
            "hidden entity should be in default results (option c)"
        )
        assert by_id["light.bedroom"]["score"] < 100, (
            f"hidden entity should carry penalty: {by_id['light.bedroom']}"
        )
        # If a visible "bedroom" match exists at score 100, it must outrank
        # the penalised hidden entry.
        visible_bedroom = next(
            (
                r for eid, r in by_id.items()
                if eid != "light.bedroom" and "bedroom" in eid.lower()
            ),
            None,
        )
        if visible_bedroom is not None:
            assert visible_bedroom["score"] >= by_id["light.bedroom"]["score"], (
                f"visible match must rank ≥ hidden: {by_id}"
            )

    @pytest.mark.asyncio
    async def test_exact_match_include_hidden_false_filters(self, sample_entities):
        """``include_hidden=False`` filters hidden entities out entirely."""

        class HidingClient(MockClient):
            async def send_websocket_message(self, payload):
                return {
                    "success": True,
                    "result": [
                        {"entity_id": "light.bedroom", "hidden_by": "user"},
                    ],
                }

        client = HidingClient(sample_entities)
        result = await _exact_match_search(
            client, "bedroom", None, 10, include_hidden=False
        )
        entity_ids = [r["entity_id"] for r in result["results"]]
        assert "light.bedroom" not in entity_ids


class TestSearchFallbackResponse:
    """Test the response format matches issue #214 requirements."""

    @pytest.mark.asyncio
    async def test_exact_match_response_format(self):
        """Verify exact match response format."""
        entities = [
            {
                "entity_id": "light.test",
                "attributes": {"friendly_name": "Test Light"},
                "state": "on",
            }
        ]
        client = MockClient(entities)
        result = await _exact_match_search(client, "test", None, 10)

        # Verify expected fields from issue #214
        assert "success" in result
        assert "results" in result
        assert result["success"] is True
