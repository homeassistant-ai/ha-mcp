"""
E2E tests for ha_get_error_log diagnostic tool.

Tests structured system log retrieval with severity filtering.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_get_error_log_all(mcp_client):
    """Test retrieving all system log entries."""
    logger.info("Testing ha_get_error_log with no filters")

    result = await mcp_client.call_tool("ha_get_error_log", {})
    data = assert_mcp_success(result, "Get all error log entries")

    assert data.get("success") is True
    assert "entries" in data
    assert isinstance(data["entries"], list)
    assert "summary" in data
    assert "total_entries" in data
    assert "returned_entries" in data

    logger.info(
        f"Retrieved {data['returned_entries']} of {data['total_entries']} log entries"
    )


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_get_error_log_severity_filter(mcp_client):
    """Test severity filtering returns only matching entries."""
    logger.info("Testing ha_get_error_log with severity=error")

    result = await mcp_client.call_tool(
        "ha_get_error_log",
        {"severity": "error"},
    )
    data = assert_mcp_success(result, "Get error-level log entries")

    assert data.get("success") is True
    assert data.get("severity_filter") == "error"

    # All returned entries should be error level or above
    for entry in data.get("entries", []):
        assert entry.get("level", "").upper() in ("ERROR", "CRITICAL", "FATAL"), (
            f"Expected error-level entry, got {entry.get('level')}"
        )

    logger.info(f"Retrieved {data['returned_entries']} error-level entries")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_get_error_log_limit(mcp_client):
    """Test that limit parameter is respected."""
    logger.info("Testing ha_get_error_log with limit=5")

    result = await mcp_client.call_tool(
        "ha_get_error_log",
        {"limit": 5},
    )
    data = assert_mcp_success(result, "Get limited log entries")

    assert data.get("success") is True
    assert data.get("returned_entries", 0) <= 5

    logger.info(f"Limit respected: got {data['returned_entries']} entries")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_get_error_log_invalid_severity(mcp_client):
    """Test that invalid severity returns an error."""
    logger.info("Testing ha_get_error_log with invalid severity")

    result = await mcp_client.call_tool(
        "ha_get_error_log",
        {"severity": "nonexistent"},
    )
    data = parse_mcp_result(result)

    assert data.get("success") is False
    assert "valid_severities" in data

    logger.info("Invalid severity correctly rejected")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_get_error_log_entry_structure(mcp_client):
    """Test that log entries have the expected structure."""
    logger.info("Testing error log entry structure")

    result = await mcp_client.call_tool(
        "ha_get_error_log",
        {"limit": 10},
    )
    data = assert_mcp_success(result, "Get log entries for structure check")

    for entry in data.get("entries", []):
        # Each entry should have these fields
        assert "timestamp" in entry, "Entry missing timestamp"
        assert "level" in entry, "Entry missing level"
        assert "message" in entry, "Entry missing message"
        assert "count" in entry, "Entry missing count"

    logger.info("Log entry structure verified")
