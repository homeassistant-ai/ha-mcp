"""
E2E tests for ha_automation_report diagnostic tool.

Tests automation health reporting including stale detection,
disabled tracking, and optional trace summaries.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_automation_report_basic(mcp_client):
    """Test basic automation report returns expected structure."""
    logger.info("Testing ha_automation_report basic output")

    result = await mcp_client.call_tool("ha_automation_report", {})
    data = assert_mcp_success(result, "Automation report")

    assert data.get("success") is True

    # Verify structure
    assert "automations" in data
    assert isinstance(data["automations"], list)
    assert "total_automations" in data
    assert isinstance(data["total_automations"], int)
    assert data["total_automations"] == len(data["automations"])

    # Verify summary
    assert "summary" in data
    summary = data["summary"]
    assert "healthy" in summary
    assert "disabled" in summary
    assert "stale" in summary
    assert "never_triggered" in summary

    assert "stale_threshold_days" in data

    logger.info(
        f"Automation report: {data['total_automations']} total, "
        f"healthy={summary['healthy']}, disabled={summary['disabled']}, "
        f"stale={summary['stale']}"
    )


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_automation_report_custom_stale_days(mcp_client):
    """Test automation report with custom stale threshold."""
    logger.info("Testing ha_automation_report with custom stale_days")

    result = await mcp_client.call_tool(
        "ha_automation_report",
        {"stale_days": 7},
    )
    data = assert_mcp_success(result, "Automation report with custom stale_days")

    assert data.get("success") is True
    assert data["stale_threshold_days"] == 7

    logger.info("Custom stale_days applied correctly")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_automation_report_entry_structure(mcp_client):
    """Test that automation entries have expected fields."""
    logger.info("Testing automation entry structure")

    result = await mcp_client.call_tool("ha_automation_report", {})
    data = assert_mcp_success(result, "Automation report for structure check")

    for auto in data.get("automations", []):
        assert "entity_id" in auto
        assert auto["entity_id"].startswith("automation."), (
            f"Expected automation entity, got {auto['entity_id']}"
        )
        assert "state" in auto
        assert "status" in auto
        assert auto["status"] in (
            "healthy",
            "disabled",
            "stale",
            "never_triggered",
            "errored",
        )
        assert "friendly_name" in auto

    logger.info("Automation entry structures verified")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_automation_report_summary_counts(mcp_client):
    """Test that summary counts are consistent with automation list."""
    logger.info("Testing automation report summary consistency")

    result = await mcp_client.call_tool("ha_automation_report", {})
    data = assert_mcp_success(result, "Automation report for count check")

    automations = data.get("automations", [])
    summary = data.get("summary", {})

    # Count statuses from the list
    status_counts: dict[str, int] = {}
    for auto in automations:
        status = auto.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    # Disabled count should match
    assert summary.get("disabled", 0) == status_counts.get("disabled", 0), (
        f"Disabled count mismatch: summary={summary.get('disabled')}, "
        f"actual={status_counts.get('disabled', 0)}"
    )

    # Never triggered count should match
    assert summary.get("never_triggered", 0) == status_counts.get("never_triggered", 0)

    logger.info("Summary counts are consistent")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_automation_report_with_traces(mcp_client):
    """Test automation report with trace inclusion enabled."""
    logger.info("Testing ha_automation_report with include_traces=True")

    result = await mcp_client.call_tool(
        "ha_automation_report",
        {"include_traces": True},
    )
    data = assert_mcp_success(result, "Automation report with traces")

    assert data.get("success") is True
    assert data.get("include_traces") is True

    # Check that latest_trace is present for automations that have traces
    for auto in data.get("automations", []):
        if "latest_trace" in auto:
            trace = auto["latest_trace"]
            assert "run_id" in trace
            assert "timestamp" in trace
            assert "state" in trace

    logger.info("Trace inclusion working correctly")
