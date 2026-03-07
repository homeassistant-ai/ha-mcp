"""
E2E tests for ha_find_anomalous_entities diagnostic tool.

Tests anomaly detection for impossible values, out-of-range readings,
and frozen sensors.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success

logger = logging.getLogger(__name__)


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_find_anomalous_entities_basic(mcp_client):
    """Test basic anomaly detection returns expected structure."""
    logger.info("Testing ha_find_anomalous_entities basic output")

    result = await mcp_client.call_tool("ha_find_anomalous_entities", {})
    data = assert_mcp_success(result, "Find anomalous entities")

    assert data.get("success") is True

    # Verify structure
    assert "total_anomalies" in data
    assert isinstance(data["total_anomalies"], int)

    assert "anomalies" in data
    anomalies = data["anomalies"]
    assert "impossible_values" in anomalies
    assert "out_of_range" in anomalies
    assert "frozen_sensors" in anomalies

    assert isinstance(anomalies["impossible_values"], list)
    assert isinstance(anomalies["out_of_range"], list)
    assert isinstance(anomalies["frozen_sensors"], list)

    assert "counts" in data
    assert "thresholds" in data

    logger.info(f"Found {data['total_anomalies']} total anomalies")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_find_anomalous_entities_custom_temp_range(mcp_client):
    """Test anomaly detection with custom temperature range."""
    logger.info("Testing ha_find_anomalous_entities with custom temp range")

    result = await mcp_client.call_tool(
        "ha_find_anomalous_entities",
        {"temp_min": -10, "temp_max": 40},
    )
    data = assert_mcp_success(result, "Find anomalies with custom temp range")

    assert data.get("success") is True
    assert data["thresholds"]["temperature_range_celsius"] == [-10.0, 40.0]

    logger.info("Custom temperature range applied correctly")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_find_anomalous_entities_anomaly_structure(mcp_client):
    """Test that individual anomaly entries have expected fields."""
    logger.info("Testing anomaly entry structure")

    result = await mcp_client.call_tool("ha_find_anomalous_entities", {})
    data = assert_mcp_success(result, "Find anomalies for structure check")

    anomalies = data.get("anomalies", {})

    # Check impossible_values entries
    for entry in anomalies.get("impossible_values", []):
        assert "entity_id" in entry
        assert "value" in entry
        assert "reason" in entry

    # Check out_of_range entries
    for entry in anomalies.get("out_of_range", []):
        assert "entity_id" in entry
        assert "value" in entry
        assert "reason" in entry

    # Check frozen_sensors entries
    for entry in anomalies.get("frozen_sensors", []):
        assert "entity_id" in entry
        assert "hours_frozen" in entry
        assert "last_updated" in entry

    logger.info("Anomaly entry structures verified")


@pytest.mark.diagnostics
@pytest.mark.asyncio
async def test_find_anomalous_entities_counts_match(mcp_client):
    """Test that total_anomalies equals sum of individual counts."""
    logger.info("Testing anomaly count consistency")

    result = await mcp_client.call_tool("ha_find_anomalous_entities", {})
    data = assert_mcp_success(result, "Find anomalies for count check")

    counts = data.get("counts", {})
    total = data.get("total_anomalies", 0)

    expected_total = (
        counts.get("impossible_values", 0)
        + counts.get("out_of_range", 0)
        + counts.get("frozen_sensors", 0)
    )

    assert total == expected_total, (
        f"total_anomalies ({total}) != sum of counts ({expected_total})"
    )

    logger.info(f"Count consistency verified: {total} total anomalies")
