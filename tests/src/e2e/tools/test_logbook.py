"""
Tests for ha_get_logbook tool - logbook entries with pagination.
"""

import logging

import pytest

from ..utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_logbook_basic(mcp_client):
    """Test basic logbook retrieval with default parameters."""
    logger.info("Testing basic logbook retrieval")

    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 1},
    )

    data = assert_mcp_success(result, "Basic logbook retrieval")

    # Verify response structure
    assert "entries" in data, "Response should contain entries"
    assert "total_entries" in data, "Response should contain total_entries"
    assert "returned_entries" in data, "Response should contain returned_entries"
    assert "limit" in data, "Response should contain limit"
    assert "offset" in data, "Response should contain offset"
    assert "has_more" in data, "Response should contain has_more"
    assert "period" in data, "Response should contain period"

    # Verify default limit is applied
    assert data["limit"] == 50, f"Default limit should be 50, got {data['limit']}"
    assert data["offset"] == 0, f"Default offset should be 0, got {data['offset']}"

    logger.info(
        f"Retrieved {data['returned_entries']} of {data['total_entries']} entries"
    )


@pytest.mark.asyncio
async def test_logbook_with_custom_limit(mcp_client):
    """Test logbook retrieval with custom limit."""
    logger.info("Testing logbook with custom limit")

    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 1, "limit": 10},
    )

    data = assert_mcp_success(result, "Logbook with custom limit")

    # Verify custom limit is applied
    assert data["limit"] == 10, f"Limit should be 10, got {data['limit']}"
    assert data["returned_entries"] <= 10, (
        f"Returned entries should be <= 10, got {data['returned_entries']}"
    )

    logger.info(f"Retrieved {data['returned_entries']} entries with limit=10")


@pytest.mark.asyncio
async def test_logbook_limit_capped_at_maximum(mcp_client):
    """Test that logbook limit is capped at maximum (500)."""
    logger.info("Testing logbook limit cap at maximum")

    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 1, "limit": 1000},  # Request more than maximum
    )

    data = assert_mcp_success(result, "Logbook with excessive limit")

    # Verify limit is capped at 500
    assert data["limit"] == 500, (
        f"Limit should be capped at 500, got {data['limit']}"
    )

    logger.info(f"Limit correctly capped at {data['limit']}")


@pytest.mark.asyncio
async def test_logbook_minimum_limit(mcp_client):
    """Test that logbook limit has a minimum of 1."""
    logger.info("Testing logbook minimum limit")

    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 1, "limit": 0},  # Request zero
    )

    data = assert_mcp_success(result, "Logbook with zero limit")

    # Verify limit is at least 1
    assert data["limit"] >= 1, f"Limit should be at least 1, got {data['limit']}"

    logger.info(f"Minimum limit enforced: {data['limit']}")


@pytest.mark.asyncio
async def test_logbook_pagination_with_offset(mcp_client):
    """Test logbook pagination using offset."""
    logger.info("Testing logbook pagination with offset")

    # Get first page
    first_page = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 24, "limit": 5, "offset": 0},
    )
    first_data = assert_mcp_success(first_page, "First page")

    # Skip test if not enough entries for pagination
    if first_data["total_entries"] <= 5:
        logger.info(
            f"Skipping pagination test - only {first_data['total_entries']} entries"
        )
        pytest.skip("Not enough logbook entries to test pagination")

    # Get second page
    second_page = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 24, "limit": 5, "offset": 5},
    )
    second_data = assert_mcp_success(second_page, "Second page")

    # Verify offset is applied
    assert second_data["offset"] == 5, "Offset should be 5"

    # Verify first and second page entries are different
    first_entries = first_data.get("entries", [])
    second_entries = second_data.get("entries", [])

    if first_entries and second_entries:
        # Compare first entry of each page - should be different
        first_entry = first_entries[0] if first_entries else None
        second_entry = second_entries[0] if second_entries else None

        if first_entry and second_entry:
            # Entries should be different
            assert first_entry != second_entry, (
                "First and second page should have different entries"
            )

    logger.info(
        f"Pagination working: page 1 has {len(first_entries)} entries, "
        f"page 2 has {len(second_entries)} entries"
    )


@pytest.mark.asyncio
async def test_logbook_has_more_indicator(mcp_client):
    """Test that has_more indicator works correctly."""
    logger.info("Testing has_more indicator")

    # Get entries with small limit
    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 24, "limit": 2, "offset": 0},
    )
    data = assert_mcp_success(result, "Small limit query")

    total = data["total_entries"]
    has_more = data["has_more"]

    # has_more should be True if total > limit + offset
    expected_has_more = total > 2
    assert has_more == expected_has_more, (
        f"has_more should be {expected_has_more} when total={total}, limit=2, offset=0"
    )

    if has_more:
        assert "pagination_hint" in data, (
            "Should include pagination_hint when has_more is True"
        )
        logger.info(f"Pagination hint: {data['pagination_hint']}")

    logger.info(
        f"has_more={has_more} (total={total}, limit=2, offset=0)"
    )


@pytest.mark.asyncio
async def test_logbook_entity_filter(mcp_client):
    """Test logbook filtering by entity_id."""
    logger.info("Testing logbook entity filter")

    # Query for sun.sun which should always exist
    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 24, "entity_id": "sun.sun", "limit": 50},
    )
    data = assert_mcp_success(result, "Entity filtered query")

    # Verify entity filter is recorded in response
    assert data.get("entity_filter") == "sun.sun", (
        "Entity filter should be recorded in response"
    )

    # If entries exist, verify they are for the filtered entity
    entries = data.get("entries", [])
    for entry in entries:
        if "entity_id" in entry:
            assert entry["entity_id"] == "sun.sun", (
                f"Entry should be for sun.sun, got {entry['entity_id']}"
            )

    logger.info(
        f"Entity filter applied: {len(entries)} entries for sun.sun"
    )


@pytest.mark.asyncio
async def test_logbook_response_metadata(mcp_client):
    """Test that logbook response includes proper metadata."""
    logger.info("Testing logbook response metadata")

    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {"hours_back": 2, "limit": 10},
    )
    data = assert_mcp_success(result, "Metadata check")

    # Verify all expected metadata fields
    required_fields = [
        "success",
        "entries",
        "period",
        "start_time",
        "end_time",
        "entity_filter",
        "total_entries",
        "returned_entries",
        "limit",
        "offset",
        "has_more",
    ]

    for field in required_fields:
        assert field in data, f"Missing required field: {field}"

    # Verify timezone metadata is included
    assert "timezone" in data or "ha_timezone" in data, (
        "Timezone metadata should be included"
    )

    logger.info("All required metadata fields present")


@pytest.mark.asyncio
async def test_logbook_empty_result(mcp_client):
    """Test logbook with non-existent entity returns appropriate error."""
    logger.info("Testing logbook with non-existent entity")

    result = await mcp_client.call_tool(
        "ha_get_logbook",
        {
            "hours_back": 1,
            "entity_id": "sensor.nonexistent_entity_xyz_12345",
            "limit": 10,
        },
    )

    # Parse result - may be success with no entries or error
    data = parse_mcp_result(result)

    # Either success with empty entries or explicit error is acceptable
    if data.get("success"):
        entries = data.get("entries", [])
        assert len(entries) == 0, "Should have no entries for non-existent entity"
        logger.info("Got success with empty entries for non-existent entity")
    else:
        # Error case is also acceptable
        logger.info(f"Got error for non-existent entity: {data.get('error')}")

    logger.info("Non-existent entity handling verified")
