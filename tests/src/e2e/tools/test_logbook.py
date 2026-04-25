"""
Tests for ha_get_logs tool - log access with multiple sources and pagination.
"""

import json
import logging

import pytest
from fastmcp.exceptions import ToolError

from ..utilities.assertions import assert_mcp_success, safe_call_tool

logger = logging.getLogger(__name__)


def get_logbook_data(result_data: dict) -> dict:
    """Extract logbook data from MCP result, handling nested structure."""
    # Handle nested data structure from MCP response
    if "data" in result_data and isinstance(result_data["data"], dict):
        return result_data["data"]
    return result_data


@pytest.mark.asyncio
async def test_logbook_basic(mcp_client):
    """Test basic logbook retrieval with default parameters."""
    logger.info("Testing basic logbook retrieval")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"hours_back": 1},
    )

    raw_data = assert_mcp_success(result, "Basic logbook retrieval")
    data = get_logbook_data(raw_data)

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
        "ha_get_logs",
        {"hours_back": 1, "limit": 10},
    )

    raw_data = assert_mcp_success(result, "Logbook with custom limit")
    data = get_logbook_data(raw_data)

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
        "ha_get_logs",
        {"hours_back": 1, "limit": 1000},  # Request more than maximum
    )

    raw_data = assert_mcp_success(result, "Logbook with excessive limit")
    data = get_logbook_data(raw_data)

    # Verify limit is capped at 500
    assert data["limit"] == 500, f"Limit should be capped at 500, got {data['limit']}"

    logger.info(f"Limit correctly capped at {data['limit']}")


@pytest.mark.asyncio
async def test_logbook_minimum_limit(mcp_client):
    """Test that logbook limit of 0 is rejected (must be at least 1)."""
    logger.info("Testing logbook minimum limit")

    data = await safe_call_tool(
        mcp_client,
        "ha_get_logs",
        {"hours_back": 1, "limit": 0},
    )

    # limit=0 should be rejected by coerce_int_param (min_value=1)
    assert not data.get("success"), "limit=0 should be rejected"

    logger.info("Zero limit correctly rejected")


@pytest.mark.asyncio
async def test_logbook_pagination_with_offset(mcp_client):
    """Test logbook pagination using offset."""
    logger.info("Testing logbook pagination with offset")

    # Get first page (use safe_call_tool to handle empty logbook in fresh containers)
    first_raw = await safe_call_tool(
        mcp_client,
        "ha_get_logs",
        {"hours_back": 24, "limit": 5, "offset": 0},
    )
    first_data = get_logbook_data(first_raw)

    # Skip test if no entries or not enough for pagination
    if not first_data.get("success") or first_data.get("total_entries", 0) <= 5:
        logger.info(
            f"Skipping pagination test - only {first_data.get('total_entries', 0)} entries"
        )
        pytest.skip("Not enough logbook entries to test pagination")

    # Get second page
    second_raw = await safe_call_tool(
        mcp_client,
        "ha_get_logs",
        {"hours_back": 24, "limit": 5, "offset": 5},
    )
    second_data = get_logbook_data(second_raw)

    # Verify offset is applied
    assert second_data["offset"] == 5, "Offset should be 5"

    # Verify first and second page entries are different
    first_entries = first_data.get("entries", [])
    second_entries = second_data.get("entries", [])

    if first_entries and second_entries:
        # Compare first entry of each page - should be different
        first_entry = first_entries[0]
        second_entry = second_entries[0]
        assert first_entry != second_entry, (
            "First and second page should have different entries"
        )

    logger.info(
        f"Pagination working: page 1 has {len(first_entries)} entries, "
        f"page 2 has {len(second_entries)} entries"
    )


@pytest.mark.asyncio
async def test_logbook_negative_offset(mcp_client):
    """Test that negative offset is rejected."""
    logger.info("Testing logbook with negative offset")

    data = await safe_call_tool(
        mcp_client,
        "ha_get_logs",
        {"hours_back": 1, "limit": 10, "offset": -5},
    )

    # Negative offset should be rejected by coerce_int_param (min_value=0)
    assert not data.get("success"), "Negative offset should be rejected"

    logger.info("Negative offset correctly rejected")


@pytest.mark.asyncio
async def test_logbook_has_more_indicator(mcp_client):
    """Test that has_more indicator works correctly."""
    logger.info("Testing has_more indicator")

    # Use safe_call_tool — pagination metadata is included in both success
    # and RESOURCE_NOT_FOUND error responses (fresh containers may have no entries)
    raw_data = await safe_call_tool(
        mcp_client,
        "ha_get_logs",
        {"hours_back": 24, "limit": 2, "offset": 0},
    )
    data = get_logbook_data(raw_data)

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

    logger.info(f"has_more={has_more} (total={total}, limit=2, offset=0)")


@pytest.mark.asyncio
async def test_logbook_entity_filter(mcp_client):
    """Test logbook filtering by entity_id."""
    logger.info("Testing logbook entity filter")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"hours_back": 24, "entity_id": "sun.sun", "limit": 50},
    )
    raw_data = assert_mcp_success(result, "Logbook entity filter")
    data = get_logbook_data(raw_data)

    # Verify entity filter is recorded in response
    assert data["entity_filter"] == "sun.sun", (
        f"Entity filter should be 'sun.sun', got: {data['entity_filter']}"
    )

    # If there are entries, verify they are for the filtered entity
    entries = data.get("entries", [])
    for entry in entries:
        if "entity_id" in entry:
            assert entry["entity_id"] == "sun.sun", (
                f"Entry should be for sun.sun, got {entry['entity_id']}"
            )
    logger.info(f"Entity filter applied: {len(entries)} entries for sun.sun")


@pytest.mark.asyncio
async def test_logbook_response_metadata(mcp_client):
    """Test that logbook response includes proper metadata."""
    logger.info("Testing logbook response metadata")

    # Use safe_call_tool — fresh CI containers may have no logbook entries
    raw_data = await safe_call_tool(
        mcp_client,
        "ha_get_logs",
        {"hours_back": 2, "limit": 10},
    )
    data = get_logbook_data(raw_data)

    # Skip if no entries — can't verify full metadata on an empty response
    if not data.get("success"):
        logger.info("No logbook entries in test period, skipping metadata check")
        pytest.skip("No logbook entries available to verify metadata")

    # Verify all expected metadata fields in the data section
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

    # Verify timezone metadata is included (may be in raw_data or data)
    has_timezone = (
        "metadata" in raw_data
        or "home_assistant_timezone" in data
        or "ha_timezone" in data
    )
    assert has_timezone, "Timezone metadata should be included"

    logger.info("All required metadata fields present")


@pytest.mark.asyncio
async def test_logbook_empty_result(mcp_client):
    """Test logbook with non-existent entity returns empty success."""
    logger.info("Testing logbook with non-existent entity")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {
            "hours_back": 1,
            "entity_id": "sensor.nonexistent_entity_xyz_12345",
            "limit": 10,
        },
    )

    raw_data = assert_mcp_success(result, "Logbook empty result")
    data = get_logbook_data(raw_data)

    # Empty results are a valid success — not an error
    assert data["success"] is True, "Empty logbook should return success"
    entries = data.get("entries", [])
    assert len(entries) == 0, "Should have no entries for non-existent entity"
    assert data["total_entries"] == 0, "total_entries should be 0"
    assert data["returned_entries"] == 0, "returned_entries should be 0"
    assert data["has_more"] is False, "has_more should be False"

    logger.info("Empty logbook correctly returns success with no entries")


# ---- Tests for new log sources ----


@pytest.mark.asyncio
async def test_logs_system_source(mcp_client):
    """Test system log retrieval via source='system'."""
    logger.info("Testing system log source")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "system"},
    )

    raw_data = assert_mcp_success(result, "System log retrieval")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "system", "Source should be 'system'"
    assert "entries" in data, "Response should contain entries"
    assert "total_entries" in data, "Response should contain total_entries"
    assert "returned_entries" in data, "Response should contain returned_entries"
    assert "limit" in data, "Response should contain limit"

    logger.info(f"Retrieved {data['returned_entries']} system log entries")


@pytest.mark.asyncio
async def test_logs_system_source_with_level_filter(mcp_client):
    """Test system log filtering by severity level."""
    logger.info("Testing system log with level filter")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "system", "level": "ERROR"},
    )

    raw_data = assert_mcp_success(result, "System log with level filter")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "system"

    # If filters were applied, verify they're reported
    filters = data.get("filters_applied", {})
    if filters:
        assert filters.get("level") == "ERROR"

    logger.info(f"Retrieved {data['returned_entries']} ERROR-level entries")


@pytest.mark.asyncio
async def test_logs_error_log_source(mcp_client):
    """Test raw error log retrieval via source='error_log'."""
    logger.info("Testing error_log source")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "error_log", "limit": 20},
    )

    raw_data = assert_mcp_success(result, "Error log retrieval")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "error_log", "Source should be 'error_log'"
    assert "log" in data, "Response should contain log text"
    assert "total_lines" in data, "Response should contain total_lines"
    assert "returned_lines" in data, "Response should contain returned_lines"
    assert data["limit"] == 20, f"Limit should be 20, got {data['limit']}"

    logger.info(
        f"Retrieved {data['returned_lines']} of {data['total_lines']} log lines"
    )


@pytest.mark.asyncio
async def test_logs_invalid_source(mcp_client):
    """Test that invalid source returns validation error (schema or tool-level)."""
    logger.info("Testing invalid source parameter")

    with pytest.raises((ToolError, Exception)):
        await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "invalid_source"},
        )

    logger.info("Invalid source correctly raises error")


@pytest.mark.asyncio
async def test_logs_invalid_level(mcp_client):
    """Test that invalid level returns validation error."""
    logger.info("Testing invalid level parameter")

    with pytest.raises(ToolError) as exc_info:
        await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "system", "level": "INVALID"},
        )

    assert (
        "invalid" in str(exc_info.value).lower()
        or "level" in str(exc_info.value).lower()
    )
    logger.info("Invalid level correctly raises ToolError")


@pytest.mark.asyncio
async def test_logs_supervisor_missing_slug(mcp_client):
    """Test that supervisor source without slug returns validation error."""
    logger.info("Testing supervisor source without slug")

    with pytest.raises(ToolError) as exc_info:
        await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "supervisor"},
        )

    assert "slug" in str(exc_info.value).lower()
    logger.info("Supervisor without slug correctly raises ToolError")


@pytest.mark.asyncio
async def test_logs_supervisor_propagates_api_error_to_structured_tool_error(
    mcp_client,
):
    """Supervisor-less Core still pins the full error chain end-to-end.

    The testcontainer runs HA Core with no Supervisor attached, so hitting
    `/api/hassio/addons/<anything>/logs` returns a non-2xx that must travel
    `_raw_request → HomeAssistantAPIError → exception_to_structured_error`
    and surface as a `ToolError` whose payload carries the slug context and
    the `ha_get_addon()` / Supervisor suggestions. Regression guard for the
    #950 chain (see PR #951).
    """
    logger.info("Testing supervisor error path is translated to structured ToolError")

    with pytest.raises(ToolError) as exc_info:
        await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "supervisor", "slug": "nonexistent_addon_slug"},
        )

    # ToolError payload is JSON-serialized — parse to assert on structure.
    payload = json.loads(str(exc_info.value))
    assert payload["success"] is False
    assert payload.get("slug") == "nonexistent_addon_slug"
    assert payload.get("source") == "supervisor"

    suggestions = payload["error"].get("suggestions") or []
    # At minimum the error must point the caller at the add-on tool.
    assert any("ha_get_addon" in s for s in suggestions), (
        f"expected ha_get_addon() suggestion, got: {suggestions}"
    )
    assert any("Supervisor" in s for s in suggestions), (
        f"expected Supervisor availability hint, got: {suggestions}"
    )
    logger.info("Supervisor error path correctly surfaces structured ToolError")


@pytest.mark.asyncio
async def test_logs_default_source_is_logbook(mcp_client):
    """Test that default source (no source param) returns logbook data."""
    logger.info("Testing default source is logbook")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"hours_back": 1},
    )

    raw_data = assert_mcp_success(result, "Default source logbook")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "logbook", "Default source should be 'logbook'"
    assert "entries" in data
    assert "has_more" in data

    logger.info("Default source correctly returns logbook data")


@pytest.mark.asyncio
async def test_logs_system_source_with_search(mcp_client):
    """Test system log search filtering."""
    logger.info("Testing system log with search filter")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "system", "search": "homeassistant"},
    )

    raw_data = assert_mcp_success(result, "System log with search")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "system"

    filters = data.get("filters_applied", {})
    if filters:
        assert filters.get("search") == "homeassistant"

    logger.info(f"Search returned {data['returned_entries']} entries")


@pytest.mark.asyncio
async def test_logs_error_log_with_level_filter(mcp_client):
    """Test error log filtering by level."""
    logger.info("Testing error_log with level filter")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "error_log", "level": "WARNING"},
    )

    raw_data = assert_mcp_success(result, "Error log with level filter")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "error_log"
    assert "log" in data

    filters = data.get("filters_applied", {})
    if filters:
        assert filters.get("level") == "WARNING"

    logger.info(f"Retrieved {data['returned_lines']} WARNING-level lines")


@pytest.mark.asyncio
async def test_logs_logbook_with_search(mcp_client):
    """Test logbook source with search keyword filtering."""
    logger.info("Testing logbook with search filter")

    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "logbook", "hours_back": 24, "search": "sun"},
    )

    raw_data = assert_mcp_success(result, "Logbook with search")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "logbook"
    assert "entries" in data

    logger.info(f"Logbook search returned {data.get('returned_entries', 0)} entries")


# -------------------- source="logger" (logger/log_info) --------------------


@pytest.mark.asyncio
async def test_logs_logger_source_basic(mcp_client):
    """ha_get_logs(source='logger') returns per-integration log levels."""
    result = await mcp_client.call_tool("ha_get_logs", {"source": "logger"})
    raw_data = assert_mcp_success(result, "Logger source retrieval")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "logger"
    assert "loggers" in data, "Logger source should return a 'loggers' list"
    assert isinstance(data["loggers"], list)
    assert "total_entries" in data
    assert "returned_entries" in data

    # Every entry has domain + level string
    for entry in data["loggers"]:
        assert "domain" in entry and isinstance(entry["domain"], str)
        assert "level" in entry and isinstance(entry["level"], str)
        # level_raw is int or None
        assert "level_raw" in entry

    logger.info(f"Retrieved {data['returned_entries']} logger entries")


@pytest.mark.asyncio
async def test_logs_logger_source_reflects_set_level(mcp_client):
    """After logger.set_level, source='logger' shows the new level for the target domain."""
    target_domain = "homeassistant"

    await mcp_client.call_tool(
        "ha_call_service",
        {
            "domain": "logger",
            "service": "set_level",
            "data": {target_domain: "debug"},
            "wait": False,
        },
    )

    try:
        result = await mcp_client.call_tool(
            "ha_get_logs",
            {"source": "logger", "search": target_domain},
        )
        raw_data = assert_mcp_success(result, "Logger source with search filter")
        data = get_logbook_data(raw_data)

        assert data.get("source") == "logger"
        filters = data.get("filters_applied") or {}
        assert filters.get("search") == target_domain

        matching = [e for e in data["loggers"] if e["domain"] == target_domain]
        assert matching, f"Expected an entry for domain={target_domain}"
        assert matching[0]["level"] == "DEBUG"
    finally:
        # Restore default
        await mcp_client.call_tool(
            "ha_call_service",
            {
                "domain": "logger",
                "service": "set_level",
                "data": {target_domain: "info"},
                "wait": False,
            },
        )


@pytest.mark.asyncio
async def test_logs_logger_search_empty_result(mcp_client):
    """Unknown search string returns 0 loggers but still succeeds."""
    result = await mcp_client.call_tool(
        "ha_get_logs",
        {"source": "logger", "search": "nonexistent_xyz_integration_12345"},
    )
    raw_data = assert_mcp_success(result, "Logger source with empty search")
    data = get_logbook_data(raw_data)

    assert data["success"] is True
    assert data.get("source") == "logger"
    assert data.get("returned_entries") == 0
    assert data.get("loggers") == []
