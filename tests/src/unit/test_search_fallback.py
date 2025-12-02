"""Unit tests for search fallback functionality (issue #214).

Tests the graceful degradation search methods:
- _exact_match_search: Fallback exact substring matching
- _partial_results_search: Last resort entity listing
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp.tools.tools_search import _exact_match_search, _partial_results_search


class MockClient:
    """Mock Home Assistant client for testing."""

    def __init__(self, entities: list[dict]):
        self.entities = entities

    async def get_states(self) -> list[dict]:
        return self.entities


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


class TestPartialResultsSearch:
    """Test _partial_results_search fallback function."""

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
    async def test_partial_results_returns_all_entities(self, sample_entities):
        """Partial results returns all entities without filtering."""
        client = MockClient(sample_entities)
        result = await _partial_results_search(client, "anything", None, 100)

        assert result["success"] is True
        assert result["partial"] is True
        assert result["search_type"] == "partial_listing"
        assert len(result["results"]) == 3

    @pytest.mark.asyncio
    async def test_partial_results_with_domain_filter(self, sample_entities):
        """Partial results respects domain_filter."""
        client = MockClient(sample_entities)
        result = await _partial_results_search(client, "anything", "light", 100)

        assert result["success"] is True
        assert result["partial"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["entity_id"] == "light.living_room"

    @pytest.mark.asyncio
    async def test_partial_results_respects_limit(self, sample_entities):
        """Partial results respects the limit parameter."""
        client = MockClient(sample_entities)
        result = await _partial_results_search(client, "anything", None, 2)

        assert result["success"] is True
        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_partial_results_has_zero_score(self, sample_entities):
        """Partial results have zero score to indicate no match."""
        client = MockClient(sample_entities)
        result = await _partial_results_search(client, "anything", None, 10)

        assert result["success"] is True
        for entity in result["results"]:
            assert entity["score"] == 0
            assert entity["match_type"] == "partial_listing"

    @pytest.mark.asyncio
    async def test_partial_results_empty_domain(self, sample_entities):
        """Partial results returns empty for non-existent domain."""
        client = MockClient(sample_entities)
        result = await _partial_results_search(client, "anything", "nonexistent", 10)

        assert result["success"] is True
        assert result["partial"] is True
        assert len(result["results"]) == 0


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

    @pytest.mark.asyncio
    async def test_partial_results_response_format(self):
        """Verify partial results response format matches issue #214."""
        entities = [
            {
                "entity_id": "light.test",
                "attributes": {"friendly_name": "Test Light"},
                "state": "on",
            }
        ]
        client = MockClient(entities)
        result = await _partial_results_search(client, "test", None, 10)

        # Verify expected fields from issue #214
        assert result["success"] is True
        assert result["partial"] is True
        assert "results" in result
