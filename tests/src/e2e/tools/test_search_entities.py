"""
Tests for ha_search_entities tool - entity search with fuzzy matching and domain filtering.

Includes regression test for issue #158: empty query with domain_filter should list all
entities of that domain, not return empty results.
"""

import logging

import pytest
from ..utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_search_entities_basic_query(mcp_client):
    """Test basic entity search with a query string."""
    logger.info("Testing basic entity search")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "limit": 5},
    )
    raw_data = assert_mcp_success(result, "Basic entity search")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert "results" in data
    logger.info(f"Found {data.get('total_matches', 0)} matches for 'light'")


@pytest.mark.asyncio
async def test_search_entities_empty_query_with_domain_filter(mcp_client):
    """
    Test that empty query with domain_filter returns all entities of that domain.

    Regression test for issue #158: ha_search_entities returns empty results
    with domain_filter='calendar' and query=''.
    """
    logger.info("Testing empty query with domain_filter (issue #158)")

    # Test with 'light' domain which should always have entities in the test environment
    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "light", "limit": 50},
    )
    raw_data = assert_mcp_success(result, "Empty query with domain_filter=light")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert data.get("search_type") == "domain_listing", \
        f"Expected search_type 'domain_listing', got '{data.get('search_type')}'"
    assert "results" in data
    results = data.get("results", [])

    # The test environment should have at least one light entity
    assert len(results) > 0, "Expected at least one light entity in results"

    # Verify all results are from the correct domain
    for entity in results:
        entity_id = entity.get("entity_id", "")
        assert entity_id.startswith("light."), \
            f"Entity {entity_id} should be in light domain"
        assert entity.get("domain") == "light"
        assert entity.get("match_type") == "domain_listing"

    logger.info(f"Found {len(results)} light entities with empty query + domain_filter")


@pytest.mark.asyncio
async def test_search_entities_whitespace_query_with_domain_filter(mcp_client):
    """Test that whitespace-only query with domain_filter behaves like empty query."""
    logger.info("Testing whitespace query with domain_filter")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "   ", "domain_filter": "light", "limit": 50},
    )
    raw_data = assert_mcp_success(result, "Whitespace query with domain_filter")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert data.get("search_type") == "domain_listing"
    assert len(data.get("results", [])) > 0, "Expected at least one light entity"

    logger.info("Whitespace query correctly treated as domain listing")


@pytest.mark.asyncio
async def test_search_entities_domain_filter_with_query(mcp_client):
    """Test domain_filter combined with a non-empty query."""
    logger.info("Testing domain_filter with query")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "bed", "domain_filter": "light", "limit": 10},
    )
    raw_data = assert_mcp_success(result, "Domain filter with query")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    # When there's a query, it should use fuzzy search
    assert data.get("search_type") == "fuzzy_search"

    # All results should be from the filtered domain
    for entity in data.get("results", []):
        entity_id = entity.get("entity_id", "")
        assert entity_id.startswith("light."), \
            f"Entity {entity_id} should be in light domain"

    logger.info(f"Found {len(data.get('results', []))} lights matching 'bed'")


@pytest.mark.asyncio
async def test_search_entities_group_by_domain(mcp_client):
    """Test group_by_domain option with empty query and domain_filter."""
    logger.info("Testing group_by_domain with empty query")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "light", "group_by_domain": True, "limit": 50},
    )
    raw_data = assert_mcp_success(result, "Group by domain")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert "by_domain" in data
    by_domain = data.get("by_domain", {})

    # Should only have one domain: light
    assert "light" in by_domain
    assert len(by_domain) == 1, "Expected only one domain in by_domain when filtering"

    logger.info(f"Group by domain: {list(by_domain.keys())}")


@pytest.mark.asyncio
async def test_search_entities_nonexistent_domain(mcp_client):
    """Test empty query with a domain that has no entities."""
    logger.info("Testing nonexistent domain")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "nonexistent_domain_xyz", "limit": 10},
    )
    raw_data = assert_mcp_success(result, "Nonexistent domain")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert data.get("total_matches") == 0
    assert len(data.get("results", [])) == 0

    logger.info("Nonexistent domain correctly returns empty results")


@pytest.mark.asyncio
async def test_search_entities_limit_respected(mcp_client):
    """Test that limit parameter is respected for domain listing."""
    logger.info("Testing limit with domain listing")

    # First, get all lights to see how many exist
    result_all = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "light", "limit": 1000},
    )
    raw_data_all = assert_mcp_success(result_all, "Get all lights")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data_all = raw_data_all.get("data", raw_data_all)
    total_lights = data_all.get("total_matches", 0)

    if total_lights <= 2:
        pytest.skip("Need more than 2 light entities to test limit")

    # Now test with a small limit
    result_limited = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "light", "limit": 2},
    )
    raw_data_limited = assert_mcp_success(result_limited, "Limited lights")
    data_limited = raw_data_limited.get("data", raw_data_limited)

    assert len(data_limited.get("results", [])) == 2, "Expected exactly 2 results with limit=2"
    # total_matches should still show the actual count
    assert data_limited.get("total_matches") == total_lights

    logger.info(f"Limit correctly applied: 2 results of {total_lights} total")


@pytest.mark.asyncio
async def test_search_entities_multiple_domains(mcp_client):
    """Test that different domains work correctly with empty query."""
    logger.info("Testing multiple domains")

    domains_to_test = ["light", "switch", "sensor", "binary_sensor"]
    results_summary = {}

    for domain in domains_to_test:
        result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": domain, "limit": 100},
        )
        raw_data = parse_mcp_result(result)
        # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
        data = raw_data.get("data", raw_data)

        if data.get("success"):
            count = len(data.get("results", []))
            results_summary[domain] = count

            # Verify all results match the domain
            for entity in data.get("results", []):
                entity_id = entity.get("entity_id", "")
                assert entity_id.startswith(f"{domain}."), \
                    f"Entity {entity_id} should be in {domain} domain"

    logger.info(f"Domain listing results: {results_summary}")

    # At least one domain should have results
    assert any(count > 0 for count in results_summary.values()), \
        "Expected at least one domain to have entities"
