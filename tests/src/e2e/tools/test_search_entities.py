"""
Tests for ha_search_entities tool - entity search with fuzzy matching and domain filtering.

Includes regression test for issue #158: empty query with domain_filter should list all
entities of that domain, not return empty results.
"""

import logging
import uuid

import pytest

from ..utilities.assertions import assert_mcp_success, parse_mcp_result, safe_call_tool
from ..utilities.wait_helpers import wait_for_tool_result

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
    assert data.get("search_type") == "domain_listing", (
        f"Expected search_type 'domain_listing', got '{data.get('search_type')}'"
    )
    assert "results" in data
    results = data.get("results", [])

    # The test environment should have at least one light entity
    assert len(results) > 0, "Expected at least one light entity in results"

    # Verify all results are from the correct domain
    for entity in results:
        entity_id = entity.get("entity_id", "")
        assert entity_id.startswith("light."), (
            f"Entity {entity_id} should be in light domain"
        )
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
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": None},
        {"query": "", "domain_filter": None, "area_filter": None},
    ],
    ids=["all-omitted", "empty-query", "whitespace-query", "null-query", "all-none"],
)
async def test_search_entities_all_filters_empty_rejected(mcp_client, params):
    """Calling with no usable query and no filters returns a validation error.

    Locks down the equivalence of empty / whitespace / None / omitted forms
    through the ``query = query or ""`` + ``.strip()`` normalization.
    """
    logger.info(f"Testing validation: {params}")

    data = await safe_call_tool(mcp_client, "ha_search_entities", params)
    inner = data.get("data", data)

    assert inner.get("success") is False, f"Should fail validation: {inner}"
    error = inner.get("error", {})
    assert isinstance(error, dict) and error.get("code") == "VALIDATION_FAILED", (
        f"Should be VALIDATION_FAILED: {inner}"
    )


@pytest.mark.asyncio
async def test_search_entities_area_filter_only(mcp_client):
    """area_filter alone (no query, no domain_filter) returns entities in that area.

    Smoke test for the standalone form legitimized by the new docstring.
    Accepts zero matches (demo env may lack areas) as long as search_type
    is 'area_only' and success=True.
    """
    logger.info("Testing area_filter alone")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"area_filter": "kitchen", "limit": 10},
    )
    raw_data = assert_mcp_success(result, "area_filter alone")
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert data.get("search_type") == "area_only", (
        f"Expected search_type 'area_only', got '{data.get('search_type')}'"
    )

    logger.info(
        f"area_filter='kitchen' returned {data.get('total_matches', 0)} matches"
    )


@pytest.mark.asyncio
async def test_search_entities_domain_filter_with_query(mcp_client):
    """Test domain_filter combined with a non-empty query."""
    logger.info("Testing domain_filter with query")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "bed", "domain_filter": "light", "limit": 10, "exact_match": False},
    )
    raw_data = assert_mcp_success(result, "Domain filter with query")
    # Tool returns {"data": {...}, "metadata": {...}} structure via add_timezone_metadata
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    # With exact_match=False, it should use fuzzy search
    assert data.get("search_type") == "fuzzy_search"

    # All results should be from the filtered domain
    for entity in data.get("results", []):
        entity_id = entity.get("entity_id", "")
        assert entity_id.startswith("light."), (
            f"Entity {entity_id} should be in light domain"
        )

    logger.info(f"Found {len(data.get('results', []))} lights matching 'bed'")


@pytest.mark.asyncio
async def test_search_entities_group_by_domain(mcp_client):
    """Test group_by_domain option with empty query and domain_filter."""
    logger.info("Testing group_by_domain with empty query")

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {"domain_filter": "light", "group_by_domain": True, "limit": 50},
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
        {"domain_filter": "nonexistent_domain_xyz", "limit": 10},
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
        {"domain_filter": "light", "limit": 1000},
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
        {"domain_filter": "light", "limit": 2},
    )
    raw_data_limited = assert_mcp_success(result_limited, "Limited lights")
    data_limited = raw_data_limited.get("data", raw_data_limited)

    assert len(data_limited.get("results", [])) == 2, (
        "Expected exactly 2 results with limit=2"
    )
    # total_matches should still show the actual count
    assert data_limited.get("total_matches") == total_lights
    # has_more should be True since we limited the results
    assert data_limited.get("has_more") is True, (
        "Expected has_more=True when limit < total_matches"
    )
    assert data_limited.get("count") == 2, "Expected count=2"
    assert data_limited.get("next_offset") == 2, "Expected next_offset=2"

    logger.info(
        f"Limit correctly applied: 2 results of {total_lights} total, has_more={data_limited.get('has_more')}"
    )


@pytest.mark.asyncio
async def test_search_entities_multiple_domains(mcp_client):
    """Test that different domains work correctly with empty query."""
    logger.info("Testing multiple domains")

    domains_to_test = ["light", "switch", "sensor", "binary_sensor"]
    results_summary = {}

    for domain in domains_to_test:
        result = await mcp_client.call_tool(
            "ha_search_entities",
            {"domain_filter": domain, "limit": 100},
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
                assert entity_id.startswith(f"{domain}."), (
                    f"Entity {entity_id} should be in {domain} domain"
                )

    logger.info(f"Domain listing results: {results_summary}")

    # At least one domain should have results
    assert any(count > 0 for count in results_summary.values()), (
        "Expected at least one domain to have entities"
    )


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
        {"query": "light", "limit": 5, "exact_match": False},
    )
    raw_data = assert_mcp_success(result, "Fuzzy search success")
    data = raw_data.get("data", raw_data)

    assert data.get("success") is True
    assert data.get("search_type") == "fuzzy_search"
    # Normal fuzzy search should NOT have warning or partial flag
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

    # search_type should be one of the expected values.
    # Note: `partial_listing` was removed in #1170 (finding 6) — its
    # behavior was a useless score=0 entity dump that masked errors;
    # exceptions now propagate to callers instead.
    valid_search_types = [
        "fuzzy_search",
        "exact_match",
        "domain_listing",
        "area_only",
        "area_filtered_query",
    ]
    assert data["search_type"] in valid_search_types, (
        f"search_type '{data['search_type']}' not in {valid_search_types}"
    )

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
    assert data["count"] == results_count, (
        f"count ({data['count']}) should equal results length ({results_count})"
    )

    # If total_matches > results count, has_more should be True
    if total_matches > results_count:
        assert data["has_more"] is True, (
            f"Expected has_more=True when total_matches ({total_matches}) > results ({results_count})"
        )
        assert data["next_offset"] is not None, (
            "next_offset should be set when has_more=True"
        )
        logger.info(
            f"Pagination: {results_count} of {total_matches} shown, has_more=True, next_offset={data['next_offset']}"
        )
    else:
        assert data["has_more"] is False, (
            f"Expected has_more=False when total_matches ({total_matches}) <= results ({results_count})"
        )
        assert data.get("next_offset") is None, (
            "next_offset should be None when has_more=False"
        )
        logger.info(
            f"No pagination needed: {results_count} of {total_matches} shown, has_more=False"
        )

    # total_matches should always be >= results_count
    assert total_matches >= results_count, (
        f"total_matches ({total_matches}) should be >= results count ({results_count})"
    )

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
        {"domain_filter": "light", "limit": 2, "offset": 0},
    )
    raw_data1 = assert_mcp_success(result1, "First page")
    data1 = raw_data1.get("data", raw_data1)

    total = data1.get("total_matches", 0)
    if total <= 2:
        pytest.skip("Need more than 2 light entities to test offset pagination")

    # Get second page
    result2 = await mcp_client.call_tool(
        "ha_search_entities",
        {"domain_filter": "light", "limit": 2, "offset": 2},
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


@pytest.mark.asyncio
class TestSearchEntitiesLimitValidation:
    """Negative-input tests for ha_search_entities limit parameter.

    Covers two invalid-limit paths added by the fix in tools_search.py:
    coerce_int_param(limit, "limit", default=10, min_value=1).
    Both inputs raise ValueError → exception_to_structured_error → VALIDATION_FAILED.
    No prior hard coverage in unit or E2E suite.
    """

    async def test_negative_limit_rejected(self, mcp_client) -> None:
        """ha_search_entities with limit=-1 returns VALIDATION_FAILED.

        Before fix: results[0:-1] silently drops the last entity, success=True.
        After fix: coerce_int_param(min_value=1) raises ValueError → VALIDATION_FAILED.
        Code path: tools_search.py — coerce_int_param(limit, "limit", default=10, min_value=1)
        → ValueError("limit must be at least 1, got -1")
        → outer except Exception → exception_to_structured_error → VALIDATION_FAILED.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_search_entities",
            {"query": "", "domain_filter": "light", "limit": -1},
        )

        inner = result.get("data", result)

        assert inner["success"] is False, (
            f"Expected success=False for limit=-1, got: {inner}"
        )
        assert inner["error"]["code"] == "VALIDATION_FAILED", (
            f"Expected VALIDATION_FAILED, got: {inner}"
        )

    async def test_zero_limit_rejected(self, mcp_client) -> None:
        """ha_search_entities with limit=0 returns VALIDATION_FAILED.

        Before fix: results[0:0] returns empty list, success=True, count=0.
        After fix: coerce_int_param(min_value=1) raises ValueError → VALIDATION_FAILED.
        Code path: identical to limit=-1 — same coerce_int_param branch.
        """
        result = await safe_call_tool(
            mcp_client,
            "ha_search_entities",
            {"query": "", "domain_filter": "light", "limit": 0},
        )

        inner = result.get("data", result)

        assert inner["success"] is False, (
            f"Expected success=False for limit=0, got: {inner}"
        )
        assert inner["error"]["code"] == "VALIDATION_FAILED", (
            f"Expected VALIDATION_FAILED, got: {inner}"
        )


# ============================================================================
# Regression tests: area_filter + domain_filter interaction
# ============================================================================


@pytest.fixture
async def area_with_mixed_domains(mcp_client):
    """Create a test area with helpers in two distinct domains.

    Yields a dict with:
        - area_id: the created area's id
        - boolean_id: input_boolean.* entity in the area
        - number_id: input_number.* entity in the area

    Cleans up area + helpers afterwards.
    """
    suffix = uuid.uuid4().hex[:8]
    area_name = f"e2e_1162_{suffix}"

    area_result = await mcp_client.call_tool(
        "ha_set_area_or_floor",
        {"kind": "area", "name": area_name, "icon": "mdi:test-tube"},
    )
    area_data = assert_mcp_success(area_result, "Create test area")
    area_id = area_data["area_id"]

    boolean_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": f"e2e 1162 bool {suffix}",
            "area_id": area_id,
        },
    )
    boolean_data = assert_mcp_success(boolean_result, "Create input_boolean")
    boolean_id = (
        boolean_data.get("entity_id")
        or f"input_boolean.{boolean_data['helper_data']['id']}"
    )

    number_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_number",
            "name": f"e2e 1162 num {suffix}",
            "area_id": area_id,
            "min_value": 0,
            "max_value": 10,
            "step": 1,
        },
    )
    number_data = assert_mcp_success(number_result, "Create input_number")
    number_id = (
        number_data.get("entity_id")
        or f"input_number.{number_data['helper_data']['id']}"
    )

    # Wait until both entities are visible under this area in the registries
    # consulted by ha_search_entities (helper creation + area assignment is
    # eventually-consistent across the entity / device / area registries).
    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_search_entities",
        arguments={"area_filter": area_id, "limit": 50},
        predicate=lambda d: {
            e.get("entity_id") for e in d.get("data", d).get("results", [])
        }.issuperset({boolean_id, number_id}),
        description="both helpers visible in area search",
    )

    yield {
        "area_id": area_id,
        "area_name": area_name,
        "boolean_id": boolean_id,
        "number_id": number_id,
    }

    for entity_id, helper_type in (
        (boolean_id, "input_boolean"),
        (number_id, "input_number"),
    ):
        try:
            await mcp_client.call_tool(
                "ha_delete_helpers_integrations",
                {
                    "target": entity_id,
                    "helper_type": helper_type,
                    "confirm": True,
                },
            )
        except Exception as exc:  # pragma: no cover — cleanup best-effort
            logger.warning(f"Cleanup failed for {entity_id}: {exc}")
    try:
        await mcp_client.call_tool(
            "ha_remove_area_or_floor", {"kind": "area", "id": area_id}
        )
    except Exception as exc:  # pragma: no cover — cleanup best-effort
        logger.warning(f"Cleanup failed for area {area_id}: {exc}")


PAGINATION_FIELDS = (
    "total_matches",
    "offset",
    "limit",
    "count",
    "has_more",
    "next_offset",
)


@pytest.mark.asyncio
async def test_area_filter_with_domain_filter_no_query(
    mcp_client, area_with_mixed_domains
):
    """area_filter + domain_filter (no query) returns only domain matches.

    by_domain must be absent when group_by_domain is not requested.
    """
    fixture = area_with_mixed_domains

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": fixture["area_id"],
            "domain_filter": "input_boolean",
            "limit": 50,
        },
    )
    raw = assert_mcp_success(result, "area+domain filter, no query")
    data = raw.get("data", raw)

    assert data["search_type"] == "area_only"
    assert data.get("domain_filter") == "input_boolean", (
        f"Response should echo domain_filter, got: {data}"
    )
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["boolean_id"] in entity_ids
    assert fixture["number_id"] not in entity_ids
    assert all(eid.startswith("input_boolean.") for eid in entity_ids), (
        f"Non-boolean entities leaked through domain_filter: {entity_ids}"
    )
    assert "by_domain" not in data, (
        f"by_domain must only appear when group_by_domain=True: {data}"
    )


@pytest.mark.asyncio
async def test_area_filter_with_domain_filter_group_by_domain(
    mcp_client, area_with_mixed_domains
):
    """area_filter + domain_filter + group_by_domain restricts by_domain keys.

    Verifies the by_domain rebuild is also restricted to the filtered domain,
    not just the flat results list.
    """
    fixture = area_with_mixed_domains

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": fixture["area_id"],
            "domain_filter": "input_boolean",
            "group_by_domain": True,
            "limit": 50,
        },
    )
    raw = assert_mcp_success(result, "area+domain+group_by_domain")
    data = raw.get("data", raw)

    assert data["search_type"] == "area_only"
    assert "by_domain" in data, (
        f"by_domain must appear when group_by_domain=True: {data}"
    )
    by_domain = data["by_domain"]
    assert set(by_domain) == {"input_boolean"}, (
        f"by_domain must be restricted to filtered domain: {list(by_domain)}"
    )
    assert all(
        e["entity_id"].startswith("input_boolean.") for e in by_domain["input_boolean"]
    )


@pytest.mark.asyncio
async def test_area_filter_with_domain_filter_and_query(
    mcp_client, area_with_mixed_domains
):
    """area_filter + domain_filter + query also respects the domain."""
    fixture = area_with_mixed_domains

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": fixture["area_id"],
            "domain_filter": "input_boolean",
            "query": "1162",
            "exact_match": False,
            "limit": 50,
        },
    )
    raw = assert_mcp_success(result, "area+domain+query")
    data = raw.get("data", raw)

    assert data["search_type"] == "area_filtered_query"
    assert data.get("domain_filter") == "input_boolean"
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["number_id"] not in entity_ids, (
        f"input_number leaked through domain_filter on area+query branch: {entity_ids}"
    )
    assert all(eid.startswith("input_boolean.") for eid in entity_ids)
    # Fixture has one input_boolean in the area; domain_filter must drop the
    # input_number before fuzzy search, so total_matches == 1.
    assert data["total_matches"] == 1, (
        f"Expected exactly 1 match after domain_filter pre-filtered: {data}"
    )


@pytest.mark.asyncio
async def test_area_filter_query_with_domain_filter_group_by_domain(
    mcp_client, area_with_mixed_domains
):
    """area_filter + domain_filter + query + group_by_domain restricts by_domain keys.

    Mirror of test_area_filter_with_domain_filter_group_by_domain for the
    area_filtered_query branch — verifies the grouped view in the with-query
    code path is also restricted to the filtered domain.
    """
    fixture = area_with_mixed_domains

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": fixture["area_id"],
            "domain_filter": "input_boolean",
            "query": "1162",
            "exact_match": False,
            "group_by_domain": True,
            "limit": 50,
        },
    )
    raw = assert_mcp_success(result, "area+domain+query+group_by_domain")
    data = raw.get("data", raw)

    assert data["search_type"] == "area_filtered_query"
    assert "by_domain" in data
    by_domain = data["by_domain"]
    assert set(by_domain) <= {"input_boolean"}, (
        f"by_domain leaked non-matching domains: {list(by_domain)}"
    )


@pytest.mark.asyncio
async def test_area_filter_only_paginates(mcp_client, area_with_mixed_domains):
    """area_only branch respects limit/offset and emits full pagination metadata."""
    fixture = area_with_mixed_domains

    page = await mcp_client.call_tool(
        "ha_search_entities",
        {"area_filter": fixture["area_id"], "limit": 1, "offset": 0},
    )
    raw = assert_mcp_success(page, "area_only with limit=1")
    data = raw.get("data", raw)

    for field in PAGINATION_FIELDS:
        assert field in data, f"Missing pagination field {field}: {data}"

    assert data["limit"] == 1
    assert data["offset"] == 0
    assert data["count"] == len(data["results"]) == 1
    # Fixture provisions exactly two entities into the unique area, so
    # total_matches must equal 2 — anything else means the area leaked.
    assert data["total_matches"] == 2, (
        f"Expected exactly 2 entities in fixture area: {data}"
    )
    assert data["has_more"] is True
    assert data["next_offset"] == 1

    # Second page should not overlap with first.
    page2 = await mcp_client.call_tool(
        "ha_search_entities",
        {"area_filter": fixture["area_id"], "limit": 1, "offset": 1},
    )
    raw2 = assert_mcp_success(page2, "area_only second page")
    data2 = raw2.get("data", raw2)

    ids1 = {r["entity_id"] for r in data["results"]}
    ids2 = {r["entity_id"] for r in data2["results"]}
    assert ids1.isdisjoint(ids2), f"Pages overlap: {ids1 & ids2}"


@pytest.mark.asyncio
async def test_area_filter_empty_area_response_shape(mcp_client):
    """Empty-area branch emits full pagination metadata + domain_filter echo.

    Covers the `area_result["areas"]` empty path: a regression there would
    silently ship a response without has_more / next_offset / count or
    domain_filter echo, breaking consistency with the populated branch.
    """
    nonexistent_area = f"e2e_1162_no_such_area_{uuid.uuid4().hex[:8]}"

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": nonexistent_area,
            "domain_filter": "input_boolean",
            "limit": 5,
        },
    )
    raw = assert_mcp_success(result, "empty-area branch")
    data = raw.get("data", raw)

    assert data["search_type"] == "area_only"
    assert data["total_matches"] == 0
    assert data["results"] == []
    assert data.get("domain_filter") == "input_boolean", (
        f"Empty-area response must still echo domain_filter: {data}"
    )
    for field in PAGINATION_FIELDS:
        assert field in data, f"Missing pagination field {field}: {data}"
    assert data["has_more"] is False
    assert data["next_offset"] is None
    # group_by_domain not requested, so by_domain must be absent.
    assert "by_domain" not in data, (
        f"by_domain must only appear when group_by_domain=True: {data}"
    )


@pytest.fixture
async def two_areas_fuzzy_match(mcp_client):
    """Two areas sharing a name prefix, each with one input_boolean helper.

    Used to exercise fuzzy area-name resolution: a query that fuzzy-matches
    multiple registered areas (`partial_ratio >= 80` in
    `get_entities_by_area`) should yield all of them, and downstream
    domain_filter logic must continue to work in that multi-area shape.
    """
    suffix = uuid.uuid4().hex[:8]
    prefix = f"e2e_fuzzy_{suffix}"
    created: list[tuple[str, str]] = []  # (kind, id)

    helpers: list[dict[str, str]] = []
    for tag in ("alpha", "beta"):
        area_result = await mcp_client.call_tool(
            "ha_set_area_or_floor",
            {"kind": "area", "name": f"{prefix}_{tag}", "icon": "mdi:test-tube"},
        )
        area_data = assert_mcp_success(area_result, f"Create area {tag}")
        area_id = area_data["area_id"]
        created.append(("area", area_id))

        bool_result = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": f"{prefix} bool {tag}",
                "area_id": area_id,
            },
        )
        bool_data = assert_mcp_success(bool_result, f"Create boolean {tag}")
        bool_id = (
            bool_data.get("entity_id")
            or f"input_boolean.{bool_data['helper_data']['id']}"
        )
        created.append(("input_boolean", bool_id))
        helpers.append({"area_id": area_id, "boolean_id": bool_id, "tag": tag})

    expected_ids = {h["boolean_id"] for h in helpers}
    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_search_entities",
        arguments={
            "area_filter": prefix,
            "domain_filter": "input_boolean",
            "query": "bool",
            "exact_match": False,
            "limit": 50,
        },
        predicate=lambda d: expected_ids.issubset(
            {e.get("entity_id") for e in d.get("data", d).get("results", [])}
        ),
        description="both fuzzy-matched helpers visible",
    )

    yield {"prefix": prefix, "helpers": helpers}

    for kind, oid in reversed(created):
        try:
            if kind == "area":
                await mcp_client.call_tool(
                    "ha_remove_area_or_floor", {"kind": "area", "id": oid}
                )
            else:
                await mcp_client.call_tool(
                    "ha_delete_helpers_integrations",
                    {"target": oid, "helper_type": kind, "confirm": True},
                )
        except Exception as exc:  # pragma: no cover — cleanup best-effort
            logger.warning(f"Cleanup failed for {kind} {oid}: {exc}")


@pytest.mark.asyncio
async def test_area_filter_fuzzy_multi_area_with_query(
    mcp_client, two_areas_fuzzy_match
):
    """Fuzzy area_filter resolving to multiple areas + domain_filter + query.

    Exercises the with-query branch's iteration over `area_result["areas"]`
    when `get_entities_by_area` resolves the fuzzy name to multiple areas.
    Domain filter must apply across all matched areas.
    """
    fixture = two_areas_fuzzy_match

    result = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": fixture["prefix"],
            "domain_filter": "input_boolean",
            "query": "bool",
            "exact_match": False,
            "limit": 50,
        },
    )
    raw = assert_mcp_success(result, "fuzzy multi-area + domain + query")
    data = raw.get("data", raw)

    assert data["search_type"] == "area_filtered_query"
    assert data.get("domain_filter") == "input_boolean"
    for field in PAGINATION_FIELDS:
        assert field in data, f"Missing pagination field {field}: {data}"
    entity_ids = {r["entity_id"] for r in data["results"]}
    expected = {h["boolean_id"] for h in fixture["helpers"]}
    assert expected.issubset(entity_ids), (
        f"Both fuzzy-matched areas' helpers should appear: missing {expected - entity_ids}"
    )
    assert all(eid.startswith("input_boolean.") for eid in entity_ids)
    assert data["total_matches"] == 2, (
        f"Both fuzzy-matched areas contribute one input_boolean each: {data}"
    )


# ============================================================================
# Issue #1170: ha_search_entities triage findings — regression tests
# ============================================================================
# Each test below locks in one of the 10 behaviors triaged in #1170.
# All would fail against master HEAD prior to the fix-PR.


@pytest.mark.asyncio
async def test_domain_filter_uppercase_normalized_issue_1170(mcp_client):
    """domain_filter is lowercased so agents that capitalize from a user
    phrase (e.g. ``"turn on the Lights"``) don't hit a silent zero-result.
    (#1170 finding 1.)"""
    lower = await mcp_client.call_tool(
        "ha_search_entities", {"domain_filter": "light", "limit": 50}
    )
    upper = await mcp_client.call_tool(
        "ha_search_entities", {"domain_filter": "Light", "limit": 50}
    )
    lower_data = assert_mcp_success(lower, "domain_filter=light").get("data", {})
    upper_data = assert_mcp_success(upper, "domain_filter=Light").get("data", {})
    assert lower_data.get("total_matches", 0) > 0, (
        f"baseline: lowercase should return results: {lower_data}"
    )
    assert upper_data["total_matches"] == lower_data["total_matches"], (
        f"uppercase domain_filter must match lowercase result count: "
        f"upper={upper_data}, lower={lower_data}"
    )
    # Echoed in normalized form so callers can see the canonical value.
    assert upper_data["domain_filter"] == "light"


@pytest.fixture
async def populated_area_for_shape_test(mcp_client):
    """Create an area with a single helper assigned, for response-shape checks."""
    suffix = uuid.uuid4().hex[:8]
    area_name = f"e2e_1170_shape_{suffix}"
    area_result = await mcp_client.call_tool(
        "ha_set_area_or_floor", {"kind": "area", "name": area_name}
    )
    area_data = assert_mcp_success(area_result, "Create shape-test area")
    area_id = area_data["area_id"]
    helper_result = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": f"e2e 1170 shape {suffix}",
            "area_id": area_id,
        },
    )
    helper_data = assert_mcp_success(helper_result, "Create shape-test helper")
    boolean_id = (
        helper_data.get("entity_id")
        or f"input_boolean.{helper_data['helper_data']['id']}"
    )
    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_search_entities",
        arguments={"area_filter": area_id, "limit": 10},
        predicate=lambda d: any(
            r.get("entity_id") == boolean_id
            for r in d.get("data", d).get("results", [])
        ),
        description="shape-test helper visible",
    )
    yield {"area_id": area_id, "boolean_id": boolean_id}
    try:
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"target": boolean_id, "helper_type": "input_boolean", "confirm": True},
        )
    except Exception as exc:  # pragma: no cover — cleanup best-effort
        logger.warning(f"Cleanup of {boolean_id} failed: {exc}")
    try:
        await mcp_client.call_tool(
            "ha_remove_area_or_floor", {"kind": "area", "id": area_id}
        )
    except Exception as exc:  # pragma: no cover — cleanup best-effort
        logger.warning(f"Cleanup of area {area_id} failed: {exc}")


@pytest.mark.asyncio
async def test_area_only_results_have_score_and_match_type_issue_1170(
    mcp_client, populated_area_for_shape_test
):
    """area_only branch results carry score+match_type, matching the other
    four search-type branches. (#1170 finding 3.)"""
    fixture = populated_area_for_shape_test
    res = await mcp_client.call_tool(
        "ha_search_entities", {"area_filter": fixture["area_id"], "limit": 50}
    )
    data = assert_mcp_success(res, "area_only shape").get("data", {})
    assert data["search_type"] == "area_only"
    assert data["results"], "fixture should yield at least one result"
    for r in data["results"]:
        assert r.get("score") == 100, f"score missing/wrong on area_only result: {r}"
        assert r.get("match_type") == "area_match", (
            f"match_type missing/wrong on area_only result: {r}"
        )


@pytest.mark.asyncio
async def test_area_filtered_query_no_per_result_area_filter_issue_1170(
    mcp_client, populated_area_for_shape_test
):
    """area_filtered_query: top-level ``area_filter`` only, not per-result.

    The redundant per-result echo was the only branch emitting it — see
    #1170 finding 4. Top-level field is still expected.
    """
    fixture = populated_area_for_shape_test
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "area_filter": fixture["area_id"],
            "query": "1170",
            "exact_match": False,
            "limit": 50,
        },
    )
    data = assert_mcp_success(res, "area_filtered_query shape").get("data", {})
    assert data["search_type"] == "area_filtered_query"
    assert data.get("area_filter") == fixture["area_id"], (
        f"top-level area_filter should still echo: {data}"
    )
    for r in data["results"]:
        assert "area_filter" not in r, (
            f"per-result area_filter should be dropped: {r}"
        )


@pytest.mark.asyncio
async def test_fuzzy_rejects_low_coverage_garbage_issue_1170(mcp_client):
    """Multi-token nonsense queries don't surface entities that only one
    token grazes. (#1170 finding 5.)

    Pre-fix, ``query="xyz_irrelevant_garbage"`` returned ``cover.garage_door``
    at score 92 via typo_fallback because the single token "garbage" matched
    "garage" at SequenceMatcher ratio 92. The new multi-token coverage
    gate rejects entities that only explain <50% of the query tokens.
    """
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "xyz_irrelevant_garbage", "exact_match": False, "limit": 5},
    )
    data = assert_mcp_success(res, "garbage query").get("data", {})
    assert data["total_matches"] == 0, (
        f"low-coverage garbage query must be rejected: {data}"
    )


@pytest.fixture
async def two_areas_with_shared_prefix(mcp_client):
    """Create two areas sharing a prefix (``bedroom_X_*``, ``bedroom_Y_*``)
    each with a helper, so a single ``area_filter="bedroom_<suffix>"``
    fuzzy-matches both."""
    suffix = uuid.uuid4().hex[:8]
    areas: list[str] = []
    helpers: list[str] = []
    for label in ("X", "Y"):
        area_res = await mcp_client.call_tool(
            "ha_set_area_or_floor",
            {"kind": "area", "name": f"bedroom {label} {suffix}"},
        )
        area_data = assert_mcp_success(area_res, f"Create area {label}")
        areas.append(area_data["area_id"])
        h_res = await mcp_client.call_tool(
            "ha_config_set_helper",
            {
                "helper_type": "input_boolean",
                "name": f"e2e 1170 bedroom {label} {suffix}",
                "area_id": area_data["area_id"],
            },
        )
        h_data = assert_mcp_success(h_res, f"Create helper {label}")
        helpers.append(
            h_data.get("entity_id") or f"input_boolean.{h_data['helper_data']['id']}"
        )
    # Wait for both helpers to be visible under the shared prefix
    await wait_for_tool_result(
        mcp_client,
        tool_name="ha_search_entities",
        arguments={"area_filter": f"bedroom_{suffix}", "limit": 10},
        predicate=lambda d: {
            r.get("entity_id") for r in d.get("data", d).get("results", [])
        }.issuperset(set(helpers)),
        description="both shared-prefix areas' helpers visible",
    )
    yield {"areas": areas, "helpers": helpers, "suffix": suffix}
    for h in helpers:
        try:
            await mcp_client.call_tool(
                "ha_delete_helpers_integrations",
                {"target": h, "helper_type": "input_boolean", "confirm": True},
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"Cleanup of {h} failed: {exc}")
    for area in areas:
        try:
            await mcp_client.call_tool(
                "ha_remove_area_or_floor", {"kind": "area", "id": area}
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"Cleanup of area {area} failed: {exc}")


@pytest.mark.asyncio
async def test_area_only_aggregates_all_matched_areas_issue_1170(
    mcp_client, two_areas_with_shared_prefix
):
    """area_only branch returns entities from ALL fuzzy-matched areas, not
    just the first. (#1170 finding 7 — the most user-visible bug in the
    triage; pre-fix, the user's intended area was silently skipped when
    another area had a similar prefix.)"""
    fixture = two_areas_with_shared_prefix
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {"area_filter": f"bedroom_{fixture['suffix']}", "limit": 50},
    )
    data = assert_mcp_success(res, "two-area aggregation").get("data", {})
    assert data["search_type"] == "area_only"
    # New plural field carries every matched area. The seeded `Bedroom`
    # area also matches the `bedroom_<suffix>` prefix at partial_ratio
    # 100 (the literal token "bedroom" is a substring of the query) and
    # legitimately appears alongside the two fixture areas — that's the
    # whole point of the fix: aggregate, don't drop. So we assert the
    # two fixture-created area names are *both* present rather than
    # asserting an exact count.
    assert isinstance(data.get("area_names"), list), (
        f"area_names must be a list: {data}"
    )
    assert len(data["area_names"]) >= 2, (
        f"at least the two fixture areas should be reported "
        f"in area_names: {data['area_names']}"
    )
    entity_ids = {r["entity_id"] for r in data["results"]}
    assert set(fixture["helpers"]).issubset(entity_ids), (
        f"both fixture areas' helpers should appear: missing "
        f"{set(fixture['helpers']) - entity_ids}; got {entity_ids}"
    )

    # Determinism: same query twice must yield the same area ordering.
    # The pre-fix code iterated a `set` which left ordering at CPython's
    # hash-randomization mercy. Asserting equality across two calls is
    # the only way to lock in the sorted-iteration fix.
    res2 = await mcp_client.call_tool(
        "ha_search_entities",
        {"area_filter": f"bedroom_{fixture['suffix']}", "limit": 50},
    )
    data2 = assert_mcp_success(res2, "two-area aggregation rerun").get("data", {})
    assert data["area_names"] == data2["area_names"], (
        f"area_names ordering must be deterministic across calls: "
        f"{data['area_names']!r} vs {data2['area_names']!r}"
    )


@pytest.fixture
async def helper_with_alias(mcp_client):
    """Create an input_boolean helper, then set an alias on it via
    ``ha_set_entity``. The alias is what the search query targets."""
    suffix = uuid.uuid4().hex[:8]
    alias = f"e2e1170alias{suffix}"
    helper_res = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": f"e2e 1170 alias src {suffix}"},
    )
    helper_data = assert_mcp_success(helper_res, "Create alias-target helper")
    eid = (
        helper_data.get("entity_id")
        or f"input_boolean.{helper_data['helper_data']['id']}"
    )
    await mcp_client.call_tool(
        "ha_set_entity", {"entity_id": eid, "aliases": [alias]}
    )
    yield {"entity_id": eid, "alias": alias}
    try:
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"target": eid, "helper_type": "input_boolean", "confirm": True},
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"Cleanup of {eid} failed: {exc}")


@pytest.mark.asyncio
async def test_search_concat_token_elision_issue_1170(mcp_client):
    """End-to-end pipeline preserves the BM25 concat-token enrichment so
    a separator-elided query (``bedlight``) lands on its target
    (``light.bed_light``). Unit coverage exists in
    ``test_bm25_search.TestFuzzySearcherIssue1170::
    test_finding_2_elided_separator_query_finds_target_uniquely``;
    this E2E variant catches a regression where alias-batch enrichment
    or the hidden-filter survivor pass strips the concat token between
    layers."""
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "bedlight", "exact_match": False, "limit": 10},
    )
    data = assert_mcp_success(res, "concat-token elision").get("data", {})
    entity_ids = [r["entity_id"] for r in data.get("results", [])]
    # `light.bed_light` is part of the initial_test_state seed.
    assert "light.bed_light" in entity_ids, (
        f"separator-elided query 'bedlight' should match light.bed_light: "
        f"{entity_ids}"
    )


@pytest.fixture
async def area_with_alias(mcp_client):
    """Create an area with a unique alias plus a helper assigned to it."""
    suffix = uuid.uuid4().hex[:8]
    area_name = f"e2e_1170_areaalias_{suffix}"
    area_alias = f"e2e1170areaalias{suffix}"
    area_res = await mcp_client.call_tool(
        "ha_set_area_or_floor",
        {"kind": "area", "name": area_name, "aliases": [area_alias]},
    )
    area_data = assert_mcp_success(area_res, "Create area with alias")
    area_id = area_data["area_id"]

    h_res = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": f"e2e 1170 areaalias {suffix}",
            "area_id": area_id,
        },
    )
    h_data = assert_mcp_success(h_res, "Create helper in alias-area")
    eid = h_data.get("entity_id") or f"input_boolean.{h_data['helper_data']['id']}"

    try:
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search_entities",
            arguments={"area_filter": area_id, "limit": 10},
            predicate=lambda d: any(
                r.get("entity_id") == eid
                for r in d.get("data", d).get("results", [])
            ),
            description="helper visible in alias-area",
        )
        yield {
            "area_id": area_id,
            "alias": area_alias,
            "entity_id": eid,
        }
    finally:
        try:
            await mcp_client.call_tool(
                "ha_delete_helpers_integrations",
                {"target": eid, "helper_type": "input_boolean", "confirm": True},
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"Cleanup of {eid} failed: {exc}")
        try:
            await mcp_client.call_tool(
                "ha_remove_area_or_floor", {"kind": "area", "id": area_id}
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"Cleanup of area {area_id} failed: {exc}")


@pytest.mark.asyncio
async def test_area_filter_resolves_by_area_alias_issue_1170(
    mcp_client, area_with_alias
):
    """Area-registry aliases are honoured by ``area_filter``. Without
    this, an area whose primary identity is ``Primary Suite`` and whose
    voice alias is ``Guest Quarters`` would resolve only via the primary
    name, defeating the alias's purpose. Locks down the area-side half
    of the fix that the entity-side alias test does not cover."""
    fixture = area_with_alias
    res = await mcp_client.call_tool(
        "ha_search_entities", {"area_filter": fixture["alias"], "limit": 10}
    )
    data = assert_mcp_success(res, "search by area alias").get("data", {})
    assert data["search_type"] == "area_only"
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["entity_id"] in entity_ids, (
        f"area_filter by alias did not resolve to the aliased area: {data}"
    )


@pytest.mark.asyncio
async def test_search_finds_entity_by_alias_issue_1170(mcp_client, helper_with_alias):
    """Entity registry aliases are honoured in fuzzy search and labelled
    ``alias_match``. (#1170 finding 8 — closes #1166.)"""
    fixture = helper_with_alias
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": fixture["alias"], "exact_match": False, "limit": 5},
    )
    data = assert_mcp_success(res, "search by alias").get("data", {})
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["entity_id"] in entity_ids, (
        f"alias query did not surface target entity: {data}"
    )
    target = next(r for r in data["results"] if r["entity_id"] == fixture["entity_id"])
    assert target["match_type"] == "alias_match", (
        f"expected match_type=alias_match, got: {target}"
    )


@pytest.fixture
async def hidden_helper(mcp_client):
    """Create an input_boolean helper, then hide it via the entity registry."""
    suffix = uuid.uuid4().hex[:8]
    distinctive = f"e2e1170hidden{suffix}"
    res = await mcp_client.call_tool(
        "ha_config_set_helper",
        {"helper_type": "input_boolean", "name": f"e2e 1170 hidden {suffix}"},
    )
    data = assert_mcp_success(res, "Create hidden-target helper")
    eid = data.get("entity_id") or f"input_boolean.{data['helper_data']['id']}"
    # Rename so the search query has a unique substring to grep on.
    await mcp_client.call_tool(
        "ha_set_entity", {"entity_id": eid, "name": distinctive, "hidden": True}
    )
    yield {"entity_id": eid, "distinctive": distinctive}
    try:
        await mcp_client.call_tool(
            "ha_delete_helpers_integrations",
            {"target": eid, "helper_type": "input_boolean", "confirm": True},
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(f"Cleanup of {eid} failed: {exc}")


@pytest.mark.asyncio
async def test_search_excludes_hidden_by_default_issue_1170(
    mcp_client, hidden_helper
):
    """Entities with ``hidden_by`` set are excluded from search by default.
    (#1170 finding 9.)"""
    fixture = hidden_helper
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": fixture["distinctive"], "exact_match": True, "limit": 5},
    )
    data = assert_mcp_success(res, "default hidden exclusion").get("data", {})
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["entity_id"] not in entity_ids, (
        f"hidden entity surfaced under default include_hidden=False: {data}"
    )


@pytest.mark.asyncio
async def test_search_include_hidden_opt_in_issue_1170(mcp_client, hidden_helper):
    """``include_hidden=True`` re-surfaces entities with ``hidden_by`` set.
    (#1170 finding 9.)"""
    fixture = hidden_helper
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "query": fixture["distinctive"],
            "exact_match": True,
            "include_hidden": True,
            "limit": 5,
        },
    )
    data = assert_mcp_success(res, "include_hidden=True").get("data", {})
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["entity_id"] in entity_ids, (
        f"include_hidden=True did not surface hidden entity: {data}"
    )


@pytest.mark.asyncio
async def test_search_fuzzy_mode_excludes_hidden_issue_1170(
    mcp_client, hidden_helper
):
    """Fuzzy mode (``exact_match=False``) also honors the hidden filter.

    Default-True ``exact_match`` and the fuzzy path are SEPARATE code
    paths in tools_search.py — substring matching vs ``smart_entity_search``
    (BM25). Both must filter hidden entities or one path silently
    regresses while the other test passes.
    """
    fixture = hidden_helper
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {
            "query": fixture["distinctive"],
            "exact_match": False,
            "limit": 5,
        },
    )
    data = assert_mcp_success(res, "fuzzy mode hidden filter").get("data", {})
    entity_ids = [r["entity_id"] for r in data["results"]]
    assert fixture["entity_id"] not in entity_ids, (
        f"fuzzy mode leaked hidden entity: {data}"
    )


@pytest.mark.asyncio
async def test_search_area_only_excludes_hidden_issue_1170(mcp_client):
    """Area branch (``area_filter`` only, no query) honors the hidden
    filter. The area resolver uses the entity registry's ``area_id``;
    we must skip ``hidden_by`` entries before composing the per-area
    list. Otherwise a UI-hidden entity assigned to an area still
    surfaces under ``area_filter=<that-area>``."""
    suffix = uuid.uuid4().hex[:8]
    area_name = f"e2e_1170_areahidden_{suffix}"

    # Create area + a helper assigned to it; then hide the helper.
    area_res = await mcp_client.call_tool(
        "ha_set_area_or_floor", {"kind": "area", "name": area_name}
    )
    area_data = assert_mcp_success(area_res, "Create area")
    area_id = area_data["area_id"]

    h_res = await mcp_client.call_tool(
        "ha_config_set_helper",
        {
            "helper_type": "input_boolean",
            "name": f"e2e 1170 areahidden {suffix}",
            "area_id": area_id,
        },
    )
    h_data = assert_mcp_success(h_res, "Create helper")
    eid = h_data.get("entity_id") or f"input_boolean.{h_data['helper_data']['id']}"

    try:
        # Wait for helper to be visible in the area before hiding it.
        await wait_for_tool_result(
            mcp_client,
            tool_name="ha_search_entities",
            arguments={"area_filter": area_id, "limit": 10},
            predicate=lambda d: any(
                r.get("entity_id") == eid
                for r in d.get("data", d).get("results", [])
            ),
            description="area entity visible before hide",
        )
        await mcp_client.call_tool(
            "ha_set_entity", {"entity_id": eid, "hidden": True}
        )

        # Default include_hidden=False
        res = await mcp_client.call_tool(
            "ha_search_entities", {"area_filter": area_id, "limit": 10}
        )
        data = assert_mcp_success(res, "area_only default hidden").get("data", {})
        entity_ids = [r["entity_id"] for r in data["results"]]
        assert eid not in entity_ids, (
            f"area_only branch leaked hidden entity: {data}"
        )

        # include_hidden=True surfaces it again
        res2 = await mcp_client.call_tool(
            "ha_search_entities",
            {"area_filter": area_id, "include_hidden": True, "limit": 10},
        )
        data2 = assert_mcp_success(res2, "area_only include_hidden").get(
            "data", {}
        )
        entity_ids2 = [r["entity_id"] for r in data2["results"]]
        assert eid in entity_ids2, (
            f"include_hidden=True did not surface hidden entity in area: {data2}"
        )
    finally:
        # Cleanup
        try:
            await mcp_client.call_tool(
                "ha_delete_helpers_integrations",
                {"target": eid, "helper_type": "input_boolean", "confirm": True},
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"Cleanup of {eid} failed: {exc}")
        try:
            await mcp_client.call_tool(
                "ha_remove_area_or_floor", {"kind": "area", "id": area_id}
            )
        except Exception as exc:  # pragma: no cover
            logger.warning(f"Cleanup of area {area_id} failed: {exc}")


@pytest.mark.asyncio
async def test_area_only_total_matches_aggregates_issue_1170(
    mcp_client, two_areas_with_shared_prefix
):
    """``total_matches`` in the area_only branch reflects the FULL
    multi-area aggregate, not a single area's count. Locks down the
    finding-7 fix at the pagination metadata layer — a future change
    that paginated per-area instead of the flattened all_results would
    silently drop the cross-area count."""
    fixture = two_areas_with_shared_prefix
    res = await mcp_client.call_tool(
        "ha_search_entities",
        {"area_filter": f"bedroom_{fixture['suffix']}", "limit": 50},
    )
    data = assert_mcp_success(res, "multi-area total").get("data", {})
    # Each fixture area has exactly 1 helper; total_matches must be
    # at least 2 (could be more if the seed Bedroom area also matched
    # the prefix and has assigned entities, but never less).
    assert data["total_matches"] >= 2, (
        f"total_matches did not aggregate across matched areas: {data}"
    )
    assert data["count"] == data["total_matches"] or data["has_more"], (
        f"count/has_more inconsistent with total_matches: {data}"
    )


# ============================================================================
# Issue #1170 finding 10: e2e coverage at realistic seeded-area populations
# ============================================================================


class TestSearchEntitiesSeededAreasIssue1170:
    """E2E tests against a multi-domain populated area at the scale of the
    initial_test_state seed.

    Pre-fix coverage gap: existing area tests used ad-hoc fixtures with one
    or two helpers per fresh area. None exercised the area_filter branches
    against an area populated with multiple domains, which is the realistic
    shape on a production HA install. (#1170 finding 10.)
    """

    SEED_ASSIGNMENTS = (
        ("light.bed_light", "bedroom"),
        ("light.ceiling_lights", "bedroom"),
        ("switch.ac", "bedroom"),
        ("cover.garage_door", "bedroom"),
    )

    @pytest.fixture
    async def seeded_bedroom(self, mcp_client):
        """Assign 4 entities of 3 different domains to the seeded
        ``bedroom`` area, then unassign on teardown.

        Cleanup is wrapped in try/finally so an unexpected fixture-body
        failure (e.g. wait_for_tool_result timing out) still unwinds the
        seed-area assignments — otherwise subsequent tests inherit the
        leaked assignment.
        """
        try:
            for eid, area_id in self.SEED_ASSIGNMENTS:
                try:
                    await mcp_client.call_tool(
                        "ha_set_entity", {"entity_id": eid, "area_id": area_id}
                    )
                except Exception as exc:
                    pytest.skip(f"Seed entity {eid} unavailable: {exc}")
            # Wait until all 4 are visible in the bedroom area
            expected = {eid for eid, _ in self.SEED_ASSIGNMENTS}
            await wait_for_tool_result(
                mcp_client,
                tool_name="ha_search_entities",
                arguments={"area_filter": "bedroom", "limit": 50},
                predicate=lambda d: expected.issubset(
                    {
                        r.get("entity_id")
                        for r in d.get("data", d).get("results", [])
                    }
                ),
                description="all seed assignments visible under bedroom",
            )
            yield {
                "area_id": "bedroom",
                "entity_ids": [eid for eid, _ in self.SEED_ASSIGNMENTS],
            }
        finally:
            # Cleanup runs even on pytest.skip or fixture-body exceptions.
            # ha_set_entity requires at least one update field, so we set
            # area_id to empty-string (which the tool maps to "no area")
            # rather than None.
            for eid, _ in self.SEED_ASSIGNMENTS:
                try:
                    await mcp_client.call_tool(
                        "ha_set_entity", {"entity_id": eid, "area_id": ""}
                    )
                except Exception as exc:  # pragma: no cover — cleanup best-effort
                    logger.warning(
                        f"Cleanup of area assignment for {eid} failed: {exc}"
                    )

    @pytest.mark.asyncio
    async def test_area_only_returns_all_assigned_domains(
        self, mcp_client, seeded_bedroom
    ):
        """area_only returns every assigned entity, spanning all domains."""
        fixture = seeded_bedroom
        res = await mcp_client.call_tool(
            "ha_search_entities", {"area_filter": fixture["area_id"], "limit": 50}
        )
        data = assert_mcp_success(res, "populated area").get("data", {})
        entity_ids = {r["entity_id"] for r in data["results"]}
        assert set(fixture["entity_ids"]).issubset(entity_ids), (
            f"all assigned entities should appear: missing "
            f"{set(fixture['entity_ids']) - entity_ids}"
        )
        domains = {r["domain"] for r in data["results"]}
        assert {"light", "switch", "cover"}.issubset(domains), (
            f"all 3 assigned domains expected in results: {domains}"
        )

    @pytest.mark.asyncio
    async def test_area_with_query_filters_within_area(
        self, mcp_client, seeded_bedroom
    ):
        """area_filter + query restricts to query matches inside the area."""
        fixture = seeded_bedroom
        res = await mcp_client.call_tool(
            "ha_search_entities",
            {
                "area_filter": fixture["area_id"],
                "query": "light",
                "exact_match": False,
                "limit": 50,
            },
        )
        data = assert_mcp_success(res, "area + query").get("data", {})
        entity_ids = [r["entity_id"] for r in data["results"]]
        assert "light.bed_light" in entity_ids
        assert "light.ceiling_lights" in entity_ids
        assert "switch.ac" not in entity_ids, (
            f"switch should not match query='light': {entity_ids}"
        )
        assert "cover.garage_door" not in entity_ids, (
            f"cover should not match query='light': {entity_ids}"
        )

    @pytest.mark.asyncio
    async def test_area_only_pagination_realistic(
        self, mcp_client, seeded_bedroom
    ):
        """Pagination works correctly at realistic populated-area scale."""
        fixture = seeded_bedroom
        res = await mcp_client.call_tool(
            "ha_search_entities", {"area_filter": fixture["area_id"], "limit": 2}
        )
        data = assert_mcp_success(res, "limit=2 page 1").get("data", {})
        assert len(data["results"]) == 2
        assert data["has_more"] is True
        assert data["next_offset"] == 2
        res2 = await mcp_client.call_tool(
            "ha_search_entities",
            {
                "area_filter": fixture["area_id"],
                "limit": 50,
                "offset": 2,
            },
        )
        data2 = assert_mcp_success(res2, "offset=2").get("data", {})
        assert len(data2["results"]) >= 2, (
            f"page 2 should have at least the remaining entities: {data2}"
        )
