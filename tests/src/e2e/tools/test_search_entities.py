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
    # has_more should be True since we limited the results
    assert data_limited.get("has_more") is True, "Expected has_more=True when limit < total_matches"
    assert data_limited.get("count") == 2, "Expected count=2"
    assert data_limited.get("next_offset") == 2, "Expected next_offset=2"

    logger.info(f"Limit correctly applied: 2 results of {total_lights} total, has_more={data_limited.get('has_more')}")


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
    # Strong matches should not include suggestions
    assert "suggestions" not in data, "Strong matches should not include suggestions"

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
async def test_search_entities_pagination_metadata(mcp_client):
    """Test that pagination metadata fields are present and correct.

    Verifies the standardized pagination response (issue #605):
    total_matches, offset, limit, count, has_more, next_offset.
    """
    logger.info("Testing pagination metadata")

    # Search for a common term that should match many entities
    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "sensor", "limit": 3},
    )
    raw_data = assert_mcp_success(result, "Search with small limit")
    data = raw_data.get("data", raw_data)

    # Verify pagination fields exist
    assert "has_more" in data, "Response must include has_more field"
    assert isinstance(data["has_more"], bool), "has_more must be a boolean"
    assert "count" in data, "Response must include count field"
    assert "offset" in data, "Response must include offset field"
    assert "limit" in data, "Response must include limit field"

    results_count = len(data.get("results", []))
    total_matches = data.get("total_matches", 0)

    # count should match actual results length
    assert data["count"] == results_count, \
        f"count ({data['count']}) should equal results length ({results_count})"

    # If total_matches > results count, has_more should be True
    if total_matches > results_count:
        assert data["has_more"] is True, \
            f"Expected has_more=True when total_matches ({total_matches}) > results ({results_count})"
        assert data["next_offset"] is not None, "next_offset should be set when has_more=True"
        logger.info(f"Pagination: {results_count} of {total_matches} shown, has_more=True, next_offset={data['next_offset']}")
    else:
        assert data["has_more"] is False, \
            f"Expected has_more=False when total_matches ({total_matches}) <= results ({results_count})"
        assert data.get("next_offset") is None, "next_offset should be None when has_more=False"
        logger.info(f"No pagination needed: {results_count} of {total_matches} shown, has_more=False")

    # total_matches should always be >= results_count
    assert total_matches >= results_count, \
        f"total_matches ({total_matches}) should be >= results count ({results_count})"

    logger.info("Pagination metadata test passed")


@pytest.mark.asyncio
async def test_search_entities_offset_pagination(mcp_client):
    """Test that offset parameter works for paginating through results.

    Issue #605: Verify that offset skips results and pages don't overlap.
    """
    logger.info("Testing offset pagination")

    # Get first page
    result1 = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "light", "limit": 2, "offset": 0},
    )
    raw_data1 = assert_mcp_success(result1, "First page")
    data1 = raw_data1.get("data", raw_data1)

    total = data1.get("total_matches", 0)
    if total <= 2:
        pytest.skip("Need more than 2 light entities to test offset pagination")

    # Get second page
    result2 = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "light", "limit": 2, "offset": 2},
    )
    raw_data2 = assert_mcp_success(result2, "Second page")
    data2 = raw_data2.get("data", raw_data2)

    # Pages should not overlap
    ids1 = {r["entity_id"] for r in data1.get("results", [])}
    ids2 = {r["entity_id"] for r in data2.get("results", [])}
    assert ids1.isdisjoint(ids2), f"Pages overlap: {ids1 & ids2}"

    # Both pages should have correct total_matches
    assert data1["total_matches"] == data2["total_matches"]
    assert data1["offset"] == 0
    assert data2["offset"] == 2

    logger.info(f"Offset pagination works: page1={ids1}, page2={ids2}")
