"""
E2E tests for BM25 fuzzy search (issue #851).

Tests that BM25 scoring improves search quality for:
- Multi-word queries where terms exist but are not adjacent (the "dryer override" case)
- Underscore/space equivalence in tokenization
- Noise reduction (returning 0 instead of hundreds of false positives)
- ha_deep_search fuzzy path with config dict scoring
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success
from ..utilities.wait_helpers import wait_for_tool_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_fuzzy_search_multi_word_non_adjacent(mcp_client):
    """BM25 finds entities where query terms exist in different fields.

    This is the core improvement over SequenceMatcher: multi-word queries
    where terms are not adjacent substrings but exist independently.
    """
    # Search for "light kitchen" with fuzzy matching
    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light kitchen", "exact_match": False, "limit": 10},
    )
    raw_data = assert_mcp_success(result, "Multi-word fuzzy search")
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    results = data.get("results", [])
    # BM25 should return results where both terms appear (tokenized matching)
    # Even if "light kitchen" is not a contiguous substring
    logger.info(
        f"Multi-word fuzzy search returned {len(results)} results "
        f"(total_matches={data.get('total_matches', 0)})"
    )


@pytest.mark.asyncio
async def test_fuzzy_search_reduces_noise(mcp_client):
    """BM25 returns fewer false positives than SequenceMatcher for unrelated queries."""
    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "query": "xyznonexistent",
            "exact_match": False,
            "limit": 10,
        },
    )
    raw_data = assert_mcp_success(result, "Noise reduction fuzzy search")
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    total = data.get("total_matches", 0)
    # BM25 should return 0 for a completely unrelated query
    # (SequenceMatcher would have returned many false positives from partial character matches)
    assert total == 0, (
        f"Expected 0 matches for nonsense query, got {total}. "
        "BM25 should not match tokens that don't exist in the corpus."
    )


@pytest.mark.asyncio
async def test_fuzzy_search_underscore_space_equivalence(mcp_client):
    """Queries with underscores and spaces should return the same results."""
    result_underscore = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "input_boolean", "exact_match": False, "limit": 20},
    )
    result_space = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "input boolean", "exact_match": False, "limit": 20},
    )

    data_u = assert_mcp_success(result_underscore, "Underscore query").get("data", {})
    data_s = assert_mcp_success(result_space, "Space query").get("data", {})

    total_u = data_u.get("total_matches", 0)
    total_s = data_s.get("total_matches", 0)

    # With BM25 unified tokenization, both should return the same count
    assert total_u == total_s, (
        f"Underscore query ({total_u}) and space query ({total_s}) "
        "should return the same number of results with unified tokenization."
    )


@pytest.mark.asyncio
async def test_deep_search_fuzzy_multi_word(mcp_client):
    """Deep search with fuzzy matching finds automations where terms are not adjacent.

    Mirrors the "dryer override" case from issue #851: query terms exist in the
    config but not as a contiguous substring.
    """
    # Create an automation with terms that are non-adjacent
    automation_config = {
        "alias": "BM25 Test Load Sharing",
        "trigger": [
            {
                "platform": "state",
                "entity_id": "sensor.bm25_dryer_power",
                "to": "high",
            }
        ],
        "action": [
            {
                "service": "input_boolean.toggle",
                "target": {"entity_id": "input_boolean.bm25_override_flag"},
            }
        ],
    }

    create_result = await mcp_client.call_tool(
        "ha_config_set_automation",
        {"config": automation_config},
    )
    assert_mcp_success(create_result, "Create BM25 test automation")

    try:
        # Search for "dryer override" — terms exist in config but not adjacent
        data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_deep_search",
            arguments={
                "query": "dryer override",
                "search_types": ["automation"],
                "limit": 10,
                "exact_match": False,
            },
            predicate=lambda d: len(d.get("automations", [])) > 0,
            description="BM25 deep search finds non-adjacent terms",
        )

        automations = data.get("automations", [])
        assert len(automations) > 0, (
            "BM25 should find the automation where 'dryer' and 'override' "
            "exist in different config fields"
        )

        # Verify exact_match=True does NOT find it (contiguous substring required)
        exact_result = await mcp_client.call_tool(
            "ha_deep_search",
            {
                "query": "dryer override",
                "search_types": ["automation"],
                "limit": 10,
                "exact_match": True,
            },
        )
        exact_data = assert_mcp_success(exact_result, "Exact deep search")
        exact_automations = exact_data.get("automations", [])
        # "dryer override" is not a contiguous substring, so exact should miss it
        found_exact = any(
            "BM25 Test" in a.get("friendly_name", "") for a in exact_automations
        )
        assert not found_exact, (
            "Exact match should NOT find 'dryer override' since the terms are not adjacent"
        )

    finally:
        # Cleanup
        try:
            await mcp_client.call_tool(
                "ha_config_remove_automation",
                {"entity_id": "automation.bm25_test_load_sharing"},
            )
        except Exception:
            logger.debug("Cleanup of BM25 test automation failed (may not exist)")


@pytest.mark.asyncio
async def test_deep_search_exact_match_still_works(mcp_client):
    """Verify exact_match=True path is unaffected by BM25 changes."""
    automation_config = {
        "alias": "BM25 Exact Match Test",
        "trigger": [
            {
                "platform": "state",
                "entity_id": "sensor.bm25_exact_test_sensor",
            }
        ],
        "action": [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.bm25_exact_test_light"},
            }
        ],
    }

    create_result = await mcp_client.call_tool(
        "ha_config_set_automation",
        {"config": automation_config},
    )
    assert_mcp_success(create_result, "Create exact match test automation")

    try:
        data = await wait_for_tool_result(
            mcp_client,
            tool_name="ha_deep_search",
            arguments={
                "query": "bm25_exact_test_sensor",
                "search_types": ["automation"],
                "limit": 10,
                "exact_match": True,
            },
            predicate=lambda d: len(d.get("automations", [])) > 0,
            description="exact match deep search still works",
        )

        automations = data.get("automations", [])
        assert len(automations) > 0, "Exact match should find the test automation"

        found = any(
            "BM25 Exact Match Test" in a.get("friendly_name", "")
            for a in automations
        )
        assert found, "Should find the specific test automation by exact match"

    finally:
        try:
            await mcp_client.call_tool(
                "ha_config_remove_automation",
                {"entity_id": "automation.bm25_exact_match_test"},
            )
        except Exception:
            logger.debug("Cleanup of exact match test automation failed")
