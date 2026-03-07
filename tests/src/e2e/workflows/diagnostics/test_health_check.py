"""
E2E tests for ha_system_health_check diagnostic tool.

Tests composite health check output covering unavailable entities,
battery levels, stale sensors, system log, repairs, and updates.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_system_health_check_basic(mcp_client):
    """Test basic health check returns all expected sections."""
    logger.info("Testing ha_system_health_check basic output")

    result = await mcp_client.call_tool("ha_system_health_check", {})
    data = assert_mcp_success(result, "System health check")

    assert data.get("success") is True

    # Verify all sections are present
    assert "overall_status" in data
    assert data["overall_status"] in ("healthy", "warnings", "critical")

    assert "issue_count" in data
    assert isinstance(data["issue_count"], int)

    assert "unavailable_entities" in data
    assert "count" in data["unavailable_entities"]
    assert "by_domain" in data["unavailable_entities"]

    assert "battery" in data
    assert "critical" in data["battery"]
    assert "low" in data["battery"]

    assert "stale_sensors" in data
    assert "threshold_hours" in data["stale_sensors"]

    assert "system_log" in data
    assert "error_count" in data["system_log"]
    assert "warning_count" in data["system_log"]

    assert "repairs" in data
    assert "open_count" in data["repairs"]

    assert "pending_updates" in data
    assert "count" in data["pending_updates"]

    logger.info(
        f"Health check complete: status={data['overall_status']}, "
        f"issues={data['issue_count']}"
    )


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_system_health_check_custom_thresholds(mcp_client):
    """Test health check with custom thresholds."""
    logger.info("Testing ha_system_health_check with custom thresholds")

    result = await mcp_client.call_tool(
        "ha_system_health_check",
        {
            "stale_threshold_hours": 0.5,
            "battery_warning_pct": 50,
            "battery_critical_pct": 25,
        },
    )
    data = assert_mcp_success(result, "Health check with custom thresholds")

    assert data.get("success") is True
    assert data["stale_sensors"]["threshold_hours"] == 0.5

    logger.info("Custom thresholds applied correctly")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_system_health_check_unavailable_entities_structure(mcp_client):
    """Test that unavailable entities section has correct structure."""
    logger.info("Testing unavailable entities structure")

    result = await mcp_client.call_tool("ha_system_health_check", {})
    data = assert_mcp_success(result, "Health check for unavailable check")

    unavailable = data.get("unavailable_entities", {})
    assert isinstance(unavailable["count"], int)
    assert isinstance(unavailable["by_domain"], dict)
    assert isinstance(unavailable["entities"], list)

    # Each unavailable entity should have entity_id, state, and domain
    for ent in unavailable["entities"]:
        assert "entity_id" in ent
        assert "state" in ent
        assert ent["state"] in ("unavailable", "unknown")

    logger.info(f"Found {unavailable['count']} unavailable entities")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_system_health_check_battery_structure(mcp_client):
    """Test that battery section has correct structure."""
    logger.info("Testing battery section structure")

    result = await mcp_client.call_tool("ha_system_health_check", {})
    data = assert_mcp_success(result, "Health check for battery check")

    battery = data.get("battery", {})
    assert isinstance(battery["critical"], list)
    assert isinstance(battery["low"], list)
    assert isinstance(battery["critical_count"], int)
    assert isinstance(battery["low_count"], int)
    assert battery["critical_count"] == len(battery["critical"])
    assert battery["low_count"] == len(battery["low"])

    # Each battery entry should have entity_id and level
    for ent in battery["critical"] + battery["low"]:
        assert "entity_id" in ent
        assert "level" in ent
        assert isinstance(ent["level"], (int, float))

    logger.info(
        f"Battery: {battery['critical_count']} critical, {battery['low_count']} low"
    )
