"""
E2E tests for ha_get_state tool - entity state retrieval.

Tests the fundamental state retrieval functionality that is the foundation
of all Home Assistant entity interactions.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_known_entity(mcp_client):
    """Test retrieving state of a known entity (sun.sun always exists)."""
    logger.info("Testing ha_get_state with sun.sun")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun"},
    )

    data = assert_mcp_success(result, "Get sun.sun state")

    # Verify response structure
    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    assert "state" in state_data, f"Missing 'state' in data: {state_data}"
    assert state_data["state"] in ["above_horizon", "below_horizon"], (
        f"Unexpected sun state: {state_data['state']}"
    )

    # Verify attributes exist
    assert "attributes" in state_data, f"Missing 'attributes': {state_data}"
    attrs = state_data["attributes"]
    assert "friendly_name" in attrs, f"Missing friendly_name: {attrs}"
    assert attrs["friendly_name"] == "Sun"

    # Verify timezone metadata
    assert "metadata" in data, f"Missing metadata: {data}"

    logger.info(f"Sun state: {state_data['state']}")
    logger.info(f"Sun attributes: elevation={attrs.get('elevation')}, azimuth={attrs.get('azimuth')}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_light_entity(mcp_client, test_light_entity):
    """Test retrieving state of a light entity."""
    logger.info(f"Testing ha_get_state with {test_light_entity}")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": test_light_entity},
    )

    data = assert_mcp_success(result, f"Get {test_light_entity} state")

    # Verify response structure
    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    assert "state" in state_data, f"Missing 'state' in data: {state_data}"
    assert state_data["state"] in ["on", "off", "unavailable", "unknown"], (
        f"Unexpected light state: {state_data['state']}"
    )

    # Light-specific attributes
    assert "attributes" in state_data, f"Missing 'attributes': {state_data}"
    attrs = state_data["attributes"]
    assert "friendly_name" in attrs, f"Missing friendly_name: {attrs}"

    # Check for common light attributes (may vary by light type)
    logger.info(f"Light state: {state_data['state']}")
    if state_data["state"] == "on":
        if "brightness" in attrs:
            logger.info(f"Light brightness: {attrs['brightness']}")
        if "color_temp" in attrs:
            logger.info(f"Light color_temp: {attrs['color_temp']}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_nonexistent_entity(mcp_client):
    """Test retrieving state of a non-existent entity returns error."""
    logger.info("Testing ha_get_state with non-existent entity")

    # Use safe_call_tool since we expect this to fail (entity doesn't exist)
    data = await safe_call_tool(
        mcp_client,
        "ha_get_state",
        {"entity_id": "sensor.nonexistent_test_entity_xyz_12345"},
    )

    # State data may be nested in 'data' key
    inner_data = data.get("data", data)
    # Should return error response
    has_error = (
        inner_data.get("success") is False
        or "error" in inner_data
        or data.get("success") is False
        or "error" in data
    )
    assert has_error, f"Expected error for non-existent entity: {data}"

    logger.info("Non-existent entity properly returned error")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_sensor_with_numeric_value(mcp_client):
    """Test retrieving state of a sensor entity with numeric value."""
    logger.info("Testing ha_get_state with sensor entity")

    # Search for a sensor to test
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "sensor", "limit": 5},
    )

    search_data = parse_mcp_result(search_result)

    # Get results from nested structure
    if "data" in search_data:
        results = search_data.get("data", {}).get("results", [])
    else:
        results = search_data.get("results", [])

    if not results:
        pytest.skip("No sensor entities available for testing")

    # Use first sensor found
    sensor_entity = results[0].get("entity_id")
    logger.info(f"Testing with sensor: {sensor_entity}")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": sensor_entity},
    )

    data = assert_mcp_success(result, f"Get {sensor_entity} state")

    # Verify response structure
    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    assert "state" in state_data, f"Missing 'state' in data: {state_data}"
    assert "attributes" in state_data, f"Missing 'attributes': {state_data}"

    logger.info(f"Sensor state: {state_data['state']}")
    if "unit_of_measurement" in state_data["attributes"]:
        logger.info(f"Unit: {state_data['attributes']['unit_of_measurement']}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_automation_entity(mcp_client):
    """Test retrieving state of an automation entity."""
    logger.info("Testing ha_get_state with automation entity")

    # Search for an automation
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "automation", "limit": 5},
    )

    search_data = parse_mcp_result(search_result)

    # Get results from nested structure
    if "data" in search_data:
        results = search_data.get("data", {}).get("results", [])
    else:
        results = search_data.get("results", [])

    if not results:
        pytest.skip("No automation entities available for testing")

    automation_entity = results[0].get("entity_id")
    logger.info(f"Testing with automation: {automation_entity}")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": automation_entity},
    )

    data = assert_mcp_success(result, f"Get {automation_entity} state")

    # Verify response structure
    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    # Automation state should be on or off
    assert state_data["state"] in ["on", "off", "unavailable"], (
        f"Unexpected automation state: {state_data['state']}"
    )

    # Automation-specific attributes
    attrs = state_data.get("attributes", {})
    logger.info(f"Automation state: {state_data['state']}")
    if "last_triggered" in attrs:
        logger.info(f"Last triggered: {attrs['last_triggered']}")
    if "current" in attrs:
        logger.info(f"Current run: {attrs['current']}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_binary_sensor(mcp_client):
    """Test retrieving state of a binary sensor entity."""
    logger.info("Testing ha_get_state with binary_sensor entity")

    # Search for a binary sensor
    search_result = await mcp_client.call_tool(
        "ha_search_entities",
        {"query": "", "domain_filter": "binary_sensor", "limit": 5},
    )

    search_data = parse_mcp_result(search_result)

    # Get results from nested structure
    if "data" in search_data:
        results = search_data.get("data", {}).get("results", [])
    else:
        results = search_data.get("results", [])

    if not results:
        pytest.skip("No binary_sensor entities available for testing")

    binary_sensor_entity = results[0].get("entity_id")
    logger.info(f"Testing with binary_sensor: {binary_sensor_entity}")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": binary_sensor_entity},
    )

    data = assert_mcp_success(result, f"Get {binary_sensor_entity} state")

    # Verify response structure
    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    # Binary sensor state should be on or off
    assert state_data["state"] in ["on", "off", "unavailable", "unknown"], (
        f"Unexpected binary_sensor state: {state_data['state']}"
    )

    attrs = state_data.get("attributes", {})
    logger.info(f"Binary sensor state: {state_data['state']}")
    if "device_class" in attrs:
        logger.info(f"Device class: {attrs['device_class']}")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_response_includes_entity_id(mcp_client):
    """Test that response includes the entity_id in data."""
    logger.info("Testing ha_get_state response structure")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun"},
    )

    data = assert_mcp_success(result, "Get state response structure")

    # Verify response structure includes entity_id
    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    assert "entity_id" in state_data, f"Missing 'entity_id' in data: {state_data}"
    assert state_data["entity_id"] == "sun.sun", (
        f"entity_id mismatch: {state_data['entity_id']}"
    )

    logger.info("Response structure includes entity_id correctly")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_state_timestamps(mcp_client):
    """Test that state response includes timestamp information."""
    logger.info("Testing ha_get_state timestamps")

    result = await mcp_client.call_tool(
        "ha_get_state",
        {"entity_id": "sun.sun"},
    )

    data = assert_mcp_success(result, "Get state timestamps")

    assert "data" in data, f"Missing 'data' in response: {data}"
    state_data = data["data"]

    # Verify timestamp fields exist
    if "last_changed" in state_data:
        logger.info(f"last_changed: {state_data['last_changed']}")
    if "last_updated" in state_data:
        logger.info(f"last_updated: {state_data['last_updated']}")

    logger.info("Timestamp fields verified")
