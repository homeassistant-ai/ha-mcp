"""
E2E tests for ha_get_history and ha_get_statistics tools.

Tests the historical data retrieval functionality for accessing
state change history and long-term statistics.
"""

import logging
from datetime import datetime, timedelta, UTC

import pytest

from ...utilities.assertions import assert_mcp_success, parse_mcp_result

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
@pytest.mark.core
class TestGetHistory:
    """Test ha_get_history tool functionality."""

    async def test_get_history_single_entity(self, mcp_client):
        """Test retrieving history for a single entity."""
        logger.info("Testing ha_get_history with single entity")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun",
                "start_time": "24h",  # Last 24 hours
            },
        )

        data = assert_mcp_success(result, "Get history for sun.sun")

        # Verify response structure - history data is nested in 'data' key
        inner_data = data.get("data", data)
        assert "entities" in inner_data, f"Missing 'entities' in response: {data}"
        assert isinstance(inner_data["entities"], list), (
            f"entities should be a list: {inner_data}"
        )

        if inner_data["entities"]:
            entity_history = inner_data["entities"][0]
            assert "entity_id" in entity_history, (
                f"Missing entity_id: {entity_history}"
            )
            assert entity_history["entity_id"] == "sun.sun", (
                f"Entity ID mismatch: {entity_history}"
            )
            assert "states" in entity_history, f"Missing states: {entity_history}"

            state_count = entity_history.get("count", len(entity_history.get("states", [])))
            logger.info(f"Retrieved {state_count} state changes for sun.sun")

            if entity_history.get("states"):
                first_state = entity_history["states"][0]
                logger.info(f"First state: {first_state.get('state')} at {first_state.get('last_changed')}")
        else:
            logger.info("No history data available (may be normal for short periods)")

    async def test_get_history_with_iso_datetime(self, mcp_client):
        """Test retrieving history with ISO datetime format."""
        logger.info("Testing ha_get_history with ISO datetime")

        # Use yesterday as start time
        yesterday = datetime.now(UTC) - timedelta(days=1)
        start_time = yesterday.isoformat()

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun",
                "start_time": start_time,
            },
        )

        data = assert_mcp_success(result, "Get history with ISO datetime")

        # History data is nested in 'data' key
        inner_data = data.get("data", data)
        assert "period" in inner_data, f"Missing period info: {data}"
        logger.info(f"Query period: {inner_data.get('period')}")

    async def test_get_history_relative_time_formats(self, mcp_client):
        """Test various relative time formats."""
        logger.info("Testing ha_get_history relative time formats")

        time_formats = ["1h", "2h", "12h", "1d", "7d"]

        for time_format in time_formats:
            result = await mcp_client.call_tool(
                "ha_get_history",
                {
                    "entity_ids": "sun.sun",
                    "start_time": time_format,
                    "limit": 5,
                },
            )

            data = parse_mcp_result(result)

            # Check nested data for success
            inner_data = data.get("data", data)
            if inner_data.get("success") or "entities" in inner_data:
                logger.info(f"Time format '{time_format}' accepted")
            else:
                logger.warning(f"Time format '{time_format}' may not be supported")

    async def test_get_history_multiple_entities(self, mcp_client):
        """Test retrieving history for multiple entities."""
        logger.info("Testing ha_get_history with multiple entities")

        # Search for a sensor to add to the query
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "sensor", "limit": 2},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            sensors = search_data.get("data", {}).get("results", [])
        else:
            sensors = search_data.get("results", [])

        entities = ["sun.sun"]
        if sensors:
            entities.append(sensors[0].get("entity_id"))

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": entities,
                "start_time": "1h",
                "limit": 10,
            },
        )

        data = assert_mcp_success(result, "Get history for multiple entities")

        # History data is nested in 'data' key
        inner_data = data.get("data", data)
        assert "entities" in inner_data, f"Missing 'entities': {data}"
        # Should have results for each entity
        logger.info(f"Retrieved history for {len(inner_data['entities'])} entities")

    async def test_get_history_with_limit(self, mcp_client):
        """Test history retrieval respects limit parameter."""
        logger.info("Testing ha_get_history with limit")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun",
                "start_time": "7d",  # Wide range
                "limit": 5,  # But limited results
            },
        )

        data = assert_mcp_success(result, "Get history with limit")

        # History data is nested in 'data' key
        inner_data = data.get("data", data)
        if inner_data.get("entities"):
            entity_history = inner_data["entities"][0]
            states = entity_history.get("states", [])
            total_available = entity_history.get("total_available", len(states))

            logger.info(f"Returned {len(states)} states (total available: {total_available})")

            # Should respect limit
            assert len(states) <= 5, f"Limit not respected: {len(states)} states"

            # Check truncated flag if more data was available
            if entity_history.get("truncated"):
                logger.info("Response correctly marked as truncated")

    async def test_get_history_minimal_response(self, mcp_client):
        """Test history with minimal_response option."""
        logger.info("Testing ha_get_history with minimal_response")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun",
                "start_time": "1h",
                "minimal_response": True,
            },
        )

        data = assert_mcp_success(result, "Get history with minimal_response")

        # History data is nested in 'data' key
        inner_data = data.get("data", data)
        # Minimal response should have fewer attributes
        if inner_data.get("entities") and inner_data["entities"][0].get("states"):
            first_state = inner_data["entities"][0]["states"][0]
            logger.info(f"Minimal response state fields: {list(first_state.keys())}")

    async def test_get_history_full_response(self, mcp_client):
        """Test history with full attributes (minimal_response=False)."""
        logger.info("Testing ha_get_history with full attributes")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun",
                "start_time": "1h",
                "minimal_response": False,
                "limit": 2,
            },
        )

        data = assert_mcp_success(result, "Get history with full attributes")

        # History data is nested in 'data' key
        inner_data = data.get("data", data)
        if inner_data.get("entities") and inner_data["entities"][0].get("states"):
            first_state = inner_data["entities"][0]["states"][0]
            logger.info(f"Full response state fields: {list(first_state.keys())}")
            # Full response should include attributes
            if "attributes" in first_state:
                logger.info(f"Attributes included: {list(first_state['attributes'].keys())}")

    async def test_get_history_nonexistent_entity(self, mcp_client):
        """Test history for non-existent entity."""
        logger.info("Testing ha_get_history with non-existent entity")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sensor.nonexistent_test_xyz_12345",
                "start_time": "1h",
            },
        )

        data = parse_mcp_result(result)

        # History data may be nested in 'data' key
        inner_data = data.get("data", data)
        # Should succeed but return empty history
        if inner_data.get("success") or "entities" in inner_data:
            if inner_data.get("entities"):
                entity_history = inner_data["entities"][0]
                states = entity_history.get("states", [])
                logger.info(f"Non-existent entity returned {len(states)} states (expected 0)")
        else:
            logger.info("Non-existent entity properly handled")

    async def test_get_history_entity_ids_as_comma_string(self, mcp_client):
        """Test history with comma-separated entity_ids string."""
        logger.info("Testing ha_get_history with comma-separated entities")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun,person.test",  # Comma-separated
                "start_time": "1h",
                "limit": 5,
            },
        )

        data = parse_mcp_result(result)

        # History data may be nested in 'data' key
        inner_data = data.get("data", data)
        if inner_data.get("success") or "entities" in inner_data:
            logger.info(f"Comma-separated entities accepted: {len(inner_data.get('entities', []))} entities")
        else:
            logger.info("Comma-separated format may not be supported")

    async def test_get_history_timestamps_present(self, mcp_client):
        """Test that history returns valid timestamps for last_changed and last_updated.

        This is a regression test for issue #447 where timestamps were null/missing.
        """
        logger.info("Testing ha_get_history includes valid timestamps")

        result = await mcp_client.call_tool(
            "ha_get_history",
            {
                "entity_ids": "sun.sun",
                "start_time": "24h",
                "minimal_response": False,
                "significant_changes_only": False,
                "limit": 10,
            },
        )

        data = assert_mcp_success(result, "Get history with timestamps")

        # History data is nested in 'data' key
        inner_data = data.get("data", data)
        assert "entities" in inner_data, f"Missing 'entities' in response: {data}"
        assert len(inner_data["entities"]) > 0, "No entities in response"

        entity_history = inner_data["entities"][0]
        assert "states" in entity_history, f"Missing states: {entity_history}"
        states = entity_history["states"]

        if len(states) > 0:
            logger.info(f"Checking {len(states)} state entries for valid timestamps")

            for idx, state in enumerate(states):
                # Verify both timestamp fields are present
                assert "last_changed" in state, f"State {idx} missing 'last_changed': {state}"
                assert "last_updated" in state, f"State {idx} missing 'last_updated': {state}"

                # Verify timestamps are not null
                last_changed = state["last_changed"]
                last_updated = state["last_updated"]

                assert last_changed is not None, f"State {idx} has null last_changed: {state}"
                assert last_updated is not None, f"State {idx} has null last_updated: {state}"

                # Verify timestamps are valid ISO 8601 strings
                assert isinstance(last_changed, str), (
                    f"State {idx} last_changed not a string: {type(last_changed)}"
                )
                assert isinstance(last_updated, str), (
                    f"State {idx} last_updated not a string: {type(last_updated)}"
                )

                # Verify timestamps can be parsed as ISO datetime
                try:
                    datetime.fromisoformat(last_changed.replace("Z", "+00:00"))
                except ValueError as e:
                    pytest.fail(f"State {idx} last_changed not valid ISO format: {last_changed}: {e}")

                try:
                    datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                except ValueError as e:
                    pytest.fail(f"State {idx} last_updated not valid ISO format: {last_updated}: {e}")

            logger.info("✓ All state entries have valid last_changed and last_updated timestamps")
            logger.info(f"Sample: last_changed={states[0]['last_changed']}, last_updated={states[0]['last_updated']}")
        else:
            logger.warning("No state history available for test (may be normal for short periods)")


@pytest.mark.asyncio
@pytest.mark.core
class TestGetStatistics:
    """Test ha_get_statistics tool functionality."""

    async def test_get_statistics_single_entity(self, mcp_client):
        """Test retrieving statistics for a sensor with state_class."""
        logger.info("Testing ha_get_statistics")

        # Search for a sensor with state_class (numeric sensors)
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "temperature", "domain_filter": "sensor", "limit": 5},
        )
        search_data = parse_mcp_result(search_result)

        if "data" in search_data:
            sensors = search_data.get("data", {}).get("results", [])
        else:
            sensors = search_data.get("results", [])

        # Try to find a numeric sensor
        test_sensor = None
        for sensor in sensors:
            entity_id = sensor.get("entity_id", "")
            if entity_id:
                test_sensor = entity_id
                break

        if not test_sensor:
            # Fallback: try any sensor
            search_result = await mcp_client.call_tool(
                "ha_search_entities",
                {"query": "", "domain_filter": "sensor", "limit": 5},
            )
            search_data = parse_mcp_result(search_result)
            if "data" in search_data:
                sensors = search_data.get("data", {}).get("results", [])
            else:
                sensors = search_data.get("results", [])
            if sensors:
                test_sensor = sensors[0].get("entity_id")

        if not test_sensor:
            pytest.skip("No sensor entities available for statistics test")

        logger.info(f"Testing statistics with: {test_sensor}")

        result = await mcp_client.call_tool(
            "ha_get_statistics",
            {
                "entity_ids": test_sensor,
                "start_time": "7d",
                "period": "day",
            },
        )

        data = parse_mcp_result(result)

        # Statistics data may be nested in 'data' key
        inner_data = data.get("data", data)
        if inner_data.get("success") or "entities" in inner_data:
            assert "entities" in inner_data, f"Missing 'entities': {data}"
            logger.info(f"Statistics retrieved for {len(inner_data.get('entities', []))} entities")

            if inner_data["entities"]:
                stats_data = inner_data["entities"][0]
                stats_count = stats_data.get("count", len(stats_data.get("statistics", [])))
                logger.info(f"Retrieved {stats_count} statistical periods")
                logger.info(f"Period type: {stats_data.get('period')}")
                if stats_data.get("unit_of_measurement"):
                    logger.info(f"Unit: {stats_data['unit_of_measurement']}")
        else:
            # Statistics may not be available for all sensors
            logger.info(f"Statistics not available: {inner_data.get('error', 'Unknown error')}")
            if "warnings" in inner_data or "suggestions" in inner_data:
                logger.info("This is expected for sensors without state_class")

    async def test_get_statistics_different_periods(self, mcp_client):
        """Test statistics with different aggregation periods."""
        logger.info("Testing ha_get_statistics with different periods")

        # Find a sensor
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "sensor", "limit": 1},
        )
        search_data = parse_mcp_result(search_result)
        if "data" in search_data:
            sensors = search_data.get("data", {}).get("results", [])
        else:
            sensors = search_data.get("results", [])

        if not sensors:
            pytest.skip("No sensors available for test")

        test_sensor = sensors[0].get("entity_id")

        periods = ["5minute", "hour", "day", "week", "month"]

        for period in periods:
            result = await mcp_client.call_tool(
                "ha_get_statistics",
                {
                    "entity_ids": test_sensor,
                    "start_time": "30d",
                    "period": period,
                },
            )

            data = parse_mcp_result(result)

            # Statistics data may be nested in 'data' key
            inner_data = data.get("data", data)
            if inner_data.get("success") or "entities" in inner_data:
                logger.info(f"Period '{period}' accepted")
            else:
                # 5minute may not be available for older data
                logger.info(f"Period '{period}' may not have data: {str(inner_data.get('error', ''))[:50]}")

    async def test_get_statistics_specific_types(self, mcp_client):
        """Test statistics with specific statistic types."""
        logger.info("Testing ha_get_statistics with specific types")

        # Find a sensor
        search_result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "", "domain_filter": "sensor", "limit": 1},
        )
        search_data = parse_mcp_result(search_result)
        if "data" in search_data:
            sensors = search_data.get("data", {}).get("results", [])
        else:
            sensors = search_data.get("results", [])

        if not sensors:
            pytest.skip("No sensors available for test")

        test_sensor = sensors[0].get("entity_id")

        result = await mcp_client.call_tool(
            "ha_get_statistics",
            {
                "entity_ids": test_sensor,
                "start_time": "7d",
                "period": "day",
                "statistic_types": ["mean", "min", "max"],
            },
        )

        data = parse_mcp_result(result)

        # Statistics data may be nested in 'data' key
        inner_data = data.get("data", data)
        if inner_data.get("success") or "entities" in inner_data:
            assert "statistic_types" in inner_data or "entities" in inner_data, (
                f"Missing expected fields: {data}"
            )
            logger.info("Specific statistic types query succeeded")

            # Check if requested types are in response
            if inner_data.get("entities") and inner_data["entities"][0].get("statistics"):
                first_stat = inner_data["entities"][0]["statistics"][0]
                logger.info(f"Statistic fields returned: {list(first_stat.keys())}")
        else:
            logger.info(f"Statistics query failed (may be expected): {str(inner_data.get('error', ''))[:50]}")

    async def test_get_statistics_invalid_period(self, mcp_client):
        """Test statistics with invalid period."""
        logger.info("Testing ha_get_statistics with invalid period")

        result = await mcp_client.call_tool(
            "ha_get_statistics",
            {
                "entity_ids": "sun.sun",
                "start_time": "7d",
                "period": "invalid_period",
            },
        )

        data = parse_mcp_result(result)

        # Statistics data may be nested in 'data' key
        inner_data = data.get("data", data)
        # Should return error for invalid period
        has_error = (
            inner_data.get("success") is False
            or "error" in inner_data
            or data.get("success") is False
            or "error" in data
        )
        assert has_error, f"Expected error for invalid period: {data}"

        if "valid_periods" in inner_data:
            logger.info(f"Valid periods listed: {inner_data['valid_periods']}")

        logger.info("Invalid period properly rejected")

    async def test_get_statistics_entity_without_state_class(self, mcp_client):
        """Test statistics for entity without state_class (should return warning)."""
        logger.info("Testing ha_get_statistics with entity without state_class")

        # sun.sun doesn't have state_class
        result = await mcp_client.call_tool(
            "ha_get_statistics",
            {
                "entity_ids": "sun.sun",
                "start_time": "7d",
                "period": "day",
            },
        )

        data = parse_mcp_result(result)

        # Statistics data may be nested in 'data' key
        inner_data = data.get("data", data)
        # May succeed but with warnings or empty data
        if inner_data.get("success") or "entities" in inner_data:
            if inner_data.get("warnings"):
                logger.info(f"Properly warned about no statistics: {inner_data['warnings']}")
            entities_data = inner_data.get("entities", [])
            if entities_data and entities_data[0].get("count") == 0:
                logger.info("Entity returned 0 statistics (expected for non-numeric entity)")
        else:
            logger.info("Properly returned error for entity without state_class")


@pytest.mark.asyncio
@pytest.mark.core
async def test_get_history_query_params_in_response(mcp_client):
    """Test that query parameters are included in response."""
    logger.info("Testing ha_get_history includes query params in response")

    result = await mcp_client.call_tool(
        "ha_get_history",
        {
            "entity_ids": "sun.sun",
            "start_time": "1h",
            "minimal_response": True,
            "significant_changes_only": True,
            "limit": 10,
        },
    )

    data = assert_mcp_success(result, "Get history with all params")

    # History data is nested in 'data' key
    inner_data = data.get("data", data)
    # Verify query_params in response
    if "query_params" in inner_data:
        params = inner_data["query_params"]
        logger.info(f"Query params in response: {params}")
        assert params.get("minimal_response") is True, f"minimal_response mismatch: {params}"
        assert params.get("significant_changes_only") is True, (
            f"significant_changes_only mismatch: {params}"
        )
        assert params.get("limit") == 10, f"limit mismatch: {params}"
    else:
        logger.info("query_params not in response (may be by design)")
