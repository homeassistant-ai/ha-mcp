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
    # is_truncated should be True since we limited the results
    assert data_limited.get("is_truncated") is True, "Expected is_truncated=True when limit < total_matches"

    logger.info(f"Limit correctly applied: 2 results of {total_lights} total, is_truncated={data_limited.get('is_truncated')}")


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


# ============================================================================
# Tests for graceful degradation (issue #214)
# ============================================================================


@pytest.mark.asyncio
async def test_search_entities_successful_fuzzy_search_no_warning(mcp_client):
    """Test that successful fuzzy search returns no warning or partial flag.

    Issue #214: Normal fuzzy search should work without fallback indicators.
    """
    logger.info("Testing successful fuzzy search has no fallback indicators")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "limit": 5},
    )
    raw_data = assert_mcp_success(result, "Fuzzy search success")
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert data.get("search_type") == "fuzzy_search"
    # Normal search should NOT have warning or partial flag
    assert "warning" not in data or data.get("warning") is None
    assert "partial" not in data or data.get("partial") is not True

    logger.info("Fuzzy search succeeded without fallback indicators")


@pytest.mark.asyncio
async def test_search_entities_response_structure_issue_214(mcp_client):
    """Test that search response has the expected structure from issue #214.

    The response should include:
    - success: boolean
    - results: array
    - search_type: string indicating which method was used
    """
    logger.info("Testing response structure for issue #214")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "limit": 5},
    )
    raw_data = assert_mcp_success(result, "Response structure check")
    data = raw_data.get("data", raw_data)

    # Verify required fields
    assert "success" in data, "Response must include 'success' field"
    assert "results" in data, "Response must include 'results' field"
    assert "search_type" in data, "Response must include 'search_type' field"
    assert isinstance(data["results"], list), "Results must be a list"

    # search_type should be one of the expected values
    valid_search_types = ["fuzzy_search", "exact_match", "partial_listing", "domain_listing"]
    assert data["search_type"] in valid_search_types, \
        f"search_type '{data['search_type']}' not in {valid_search_types}"

    logger.info(f"Response structure valid with search_type: {data['search_type']}")


@pytest.mark.asyncio
async def test_search_entities_fallback_fields_when_present(mcp_client):
    """Test that fallback fields have correct types when present.

    Issue #214: When fallback is used, response should include:
    - partial: true
    - warning: string explaining what happened
    """
    logger.info("Testing fallback field types")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "light", "limit": 5},
    )
    raw_data = assert_mcp_success(result, "Fallback field types")
    data = raw_data.get("data", raw_data)

    # If warning is present, it should be a string
    if "warning" in data and data["warning"] is not None:
        assert isinstance(data["warning"], str), "warning must be a string"
        logger.info(f"Warning present: {data['warning']}")

    # If partial is present, it should be a boolean
    if "partial" in data and data["partial"] is not None:
        assert isinstance(data["partial"], bool), "partial must be a boolean"
        logger.info(f"Partial flag: {data['partial']}")

    logger.info("Fallback field types are correct")


@pytest.mark.asyncio
async def test_search_entities_truncation_indicator(mcp_client):
    """Test that is_truncated field accurately indicates when results are truncated.

    This test verifies the fix for the truncation indicator issue where
    total_matches would incorrectly report the limited count instead of the
    actual total number of matches.
    """
    logger.info("Testing truncation indicator")

    # Search for a common term that should match many entities
    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "sensor", "limit": 3},
    )
    raw_data = assert_mcp_success(result, "Search with small limit")
    data = raw_data.get("data", raw_data)

    # Verify is_truncated field exists
    assert "is_truncated" in data, "Response must include is_truncated field"
    assert isinstance(data["is_truncated"], bool), "is_truncated must be a boolean"

    results_count = len(data.get("results", []))
    total_matches = data.get("total_matches", 0)

    # If total_matches > results count, is_truncated should be True
    if total_matches > results_count:
        assert data["is_truncated"] is True, \
            f"Expected is_truncated=True when total_matches ({total_matches}) > results ({results_count})"
        logger.info(f"Truncation correctly indicated: {results_count} of {total_matches} shown, is_truncated=True")
    else:
        # If all matches fit within limit, is_truncated should be False
        assert data["is_truncated"] is False, \
            f"Expected is_truncated=False when total_matches ({total_matches}) <= results ({results_count})"
        logger.info(f"No truncation: {results_count} of {total_matches} shown, is_truncated=False")

    # Also test that total_matches reports the actual total, not the limited count
    # This should always be >= results_count
    assert total_matches >= results_count, \
        f"total_matches ({total_matches}) should be >= results count ({results_count})"

    logger.info("Truncation indicator test passed")
