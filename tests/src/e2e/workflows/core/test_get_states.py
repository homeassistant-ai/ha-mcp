"""
E2E tests for ha_get_states tool - bulk entity state retrieval.

Tests the bulk state retrieval functionality that fetches multiple entity
states in a single call using parallel requests.
"""

import logging

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result, safe_call_tool

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.core
class TestGetStates:
    """Test ha_get_states bulk entity state retrieval."""

    async def test_multiple_known_entities(self, mcp_client):
        """Retrieve states for multiple known entities; all succeed."""
        logger.info("Testing ha_get_states with sun.sun + a sensor")

        # Find a sensor entity to pair with sun.sun
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "sensor", "limit": 1},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            results = search_data.get("data", {}).get("results", [])
        else:
            results = search_data.get("results", [])

        if not results:
            pytest.skip("No sensor entities available for testing")

        sensor_id = results[0]["entity_id"]
        entity_ids = ["sun.sun", sensor_id]
        logger.info(f"Testing with entities: {entity_ids}")

        result = await mcp_client.call_tool(
            "ha_get_states",
            {"entity_ids": entity_ids},
        )

        data = assert_mcp_success(result, "Get multiple entity states")

        assert "data" in data, f"Missing 'data' in response: {data}"
        inner = data["data"]

        assert inner["success"] is True, f"Expected success: {inner}"
        assert inner["count"] == 2, f"Expected count 2: {inner}"
        assert isinstance(inner["states"], dict), f"states should be a dict: {inner}"
        assert "sun.sun" in inner["states"], f"Missing sun.sun: {inner['states']}"
        assert sensor_id in inner["states"], f"Missing {sensor_id}: {inner['states']}"

        # Verify sun.sun state data
        sun_state = inner["states"]["sun.sun"]
        assert "state" in sun_state, f"Missing state in sun data: {sun_state}"
        assert sun_state["state"] in ["above_horizon", "below_horizon"]

        # No errors should be present
        assert "errors" not in inner, f"Unexpected errors: {inner.get('errors')}"
        assert "partial" not in inner, f"Unexpected partial flag: {inner}"

        # Verify metadata from add_timezone_metadata
        assert "metadata" in data, f"Missing metadata: {data}"

        logger.info(f"Retrieved {inner['count']} states successfully")

    async def test_partial_failure_with_nonexistent_entity(self, mcp_client):
        """Mix of real and nonexistent entities returns partial success."""
        logger.info("Testing ha_get_states with partial failure")

        result = await mcp_client.call_tool(
            "ha_get_states",
            {"entity_ids": ["sun.sun", "sensor.nonexistent_test_xyz_99999"]},
        )

        data = assert_mcp_success(result, "Partial failure get_states")

        assert "data" in data, f"Missing 'data': {data}"
        inner = data["data"]

        assert inner["success"] is True, f"Partial should still be success: {inner}"
        assert inner["count"] == 1, f"Only one entity should succeed: {inner}"
        assert "sun.sun" in inner["states"], f"sun.sun should be present: {inner}"
        assert inner["partial"] is True, f"Should have partial flag: {inner}"
        assert inner["error_count"] == 1, f"Should have 1 error: {inner}"
        assert len(inner["errors"]) == 1, f"errors list should have 1 entry: {inner}"
        assert inner["errors"][0]["entity_id"] == "sensor.nonexistent_test_xyz_99999"
        assert "suggestions" in inner, f"Should have suggestions: {inner}"

        logger.info("Partial failure handled correctly")

    async def test_all_nonexistent_entities(self, mcp_client):
        """All entities nonexistent returns success=False."""
        logger.info("Testing ha_get_states with all nonexistent entities")

        result = await safe_call_tool(
            mcp_client,
            "ha_get_states",
            {"entity_ids": ["sensor.fake_aaa_111", "sensor.fake_bbb_222"]},
        )

        inner = result.get("data", result)

        assert inner.get("success") is False, f"Should be failure: {inner}"
        assert inner.get("count") == 0, f"No states should be returned: {inner}"
        assert len(inner.get("states", {})) == 0, f"states should be empty: {inner}"
        assert inner.get("error_count") == 2, f"Should have 2 errors: {inner}"
        assert "partial" not in inner, f"Should not have partial flag: {inner}"

        logger.info("All-fail case handled correctly")

    async def test_empty_entity_ids_rejected(self, mcp_client):
        """Empty entity_ids list returns validation error."""
        logger.info("Testing ha_get_states with empty list")

        result = await safe_call_tool(
            mcp_client,
            "ha_get_states",
            {"entity_ids": []},
        )

        inner = result.get("data", result)

        assert inner.get("success") is False, f"Should fail validation: {inner}"
        assert inner.get("error", {}).get("code") == "VALIDATION_FAILED", (
            f"Should be VALIDATION_FAILED: {inner}"
        )

        logger.info("Empty list validation works correctly")

    async def test_response_states_keyed_by_entity_id(self, mcp_client):
        """Verify states dict is keyed by entity_id, not a list."""
        logger.info("Testing ha_get_states response structure")

        result = await mcp_client.call_tool(
            "ha_get_states",
            {"entity_ids": ["sun.sun"]},
        )

        data = assert_mcp_success(result, "Single entity get_states")

        inner = data["data"]
        assert isinstance(inner["states"], dict), f"states must be dict: {type(inner['states'])}"
        assert "sun.sun" in inner["states"], f"Key should be entity_id: {inner['states']}"

        sun_data = inner["states"]["sun.sun"]
        assert "entity_id" in sun_data, f"State data should contain entity_id: {sun_data}"
        assert "state" in sun_data, f"State data should contain state: {sun_data}"
        assert "attributes" in sun_data, f"State data should contain attributes: {sun_data}"

        logger.info("Response structure is correct")

    async def test_duplicate_entity_ids_deduplicated(self, mcp_client):
        """Duplicate IDs are deduplicated; only one state returned per unique ID."""
        logger.info("Testing ha_get_states deduplication")

        result = await mcp_client.call_tool(
            "ha_get_states",
            {"entity_ids": ["sun.sun", "sun.sun", "sun.sun"]},
        )

        data = assert_mcp_success(result, "Deduplicated get_states")

        inner = data["data"]
        assert inner["count"] == 1, f"Should have 1 unique state: {inner}"
        assert len(inner["states"]) == 1, f"states dict should have 1 entry: {inner}"
        assert "sun.sun" in inner["states"]

        logger.info("Deduplication works correctly")
