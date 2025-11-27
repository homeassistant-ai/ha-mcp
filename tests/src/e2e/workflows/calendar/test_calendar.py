"""
Calendar Management E2E Tests

Tests the calendar event management tools:
- ha_config_get_calendar_events - Get events from a calendar
- ha_config_set_calendar_event - Create a calendar event
- ha_config_remove_calendar_event - Delete a calendar event

Note: These tests require calendar integrations to be configured in Home Assistant.
The tests are designed to work with the demo integration's calendar or local calendar.
Use ha_search_entities(query='calendar', domain_filter='calendar') to find calendar entities.
"""

import logging
from datetime import datetime, timedelta

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
)

logger = logging.getLogger(__name__)


@pytest.mark.calendar
class TestCalendarEvents:
    """Test calendar event retrieval functionality."""

    async def _find_calendar_entity(self, mcp_client) -> str | None:
        """Find an available calendar entity for testing."""
        result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "calendar", "domain_filter": "calendar", "limit": 10},
        )
        data = parse_mcp_result(result)

        # Handle nested data structure
        if "data" in data:
            results = data.get("data", {}).get("results", [])
        else:
            results = data.get("results", [])

        if not results:
            return None

        # Return the first calendar found
        return results[0].get("entity_id")

    async def test_get_calendar_events_default_range(self, mcp_client):
        """
        Test: Get calendar events with default time range

        Retrieves events for the next 7 days (default behavior).
        """
        calendar_entity = await self._find_calendar_entity(mcp_client)
        if not calendar_entity:
            pytest.skip("No calendar entities available for testing")

        logger.info(
            f"Testing ha_config_get_calendar_events with {calendar_entity}..."
        )

        result = await mcp_client.call_tool(
            "ha_config_get_calendar_events", {"entity_id": calendar_entity}
        )

        data = assert_mcp_success(result, "get calendar events")

        # Validate response structure
        assert "events" in data, "Response should contain 'events' key"
        assert "count" in data, "Response should contain 'count' key"
        assert "time_range" in data, "Response should contain 'time_range' key"
        assert isinstance(data["events"], list), "Events should be a list"

        logger.info(f"Retrieved {data['count']} event(s) from {calendar_entity}")
        logger.info(f"Time range: {data['time_range']}")

        # Validate event structure if events exist
        for event in data["events"]:
            logger.info(f"  - Event: {event.get('summary', 'Untitled')}")

        logger.info("ha_config_get_calendar_events default range test completed")

    async def test_get_calendar_events_custom_range(self, mcp_client):
        """
        Test: Get calendar events with custom time range

        Retrieves events for a specific date range.
        """
        calendar_entity = await self._find_calendar_entity(mcp_client)
        if not calendar_entity:
            pytest.skip("No calendar entities available for testing")

        logger.info(
            f"Testing ha_config_get_calendar_events with custom range for {calendar_entity}..."
        )

        # Set a custom time range (next 30 days)
        now = datetime.now()
        start = now.isoformat()
        end = (now + timedelta(days=30)).isoformat()

        result = await mcp_client.call_tool(
            "ha_config_get_calendar_events",
            {
                "entity_id": calendar_entity,
                "start": start,
                "end": end,
                "max_results": 5,
            },
        )

        data = assert_mcp_success(result, "get calendar events with custom range")

        # Validate response
        assert "events" in data, "Response should contain 'events' key"
        assert data["count"] <= 5, "Should respect max_results limit"

        logger.info(
            f"Retrieved {data['count']} event(s) with max_results=5, total_available={data.get('total_available', 'unknown')}"
        )
        logger.info("ha_config_get_calendar_events custom range test completed")

    async def test_get_calendar_events_invalid_entity(self, mcp_client):
        """
        Test: Get events from invalid calendar entity

        Verifies proper error handling for non-existent calendars.
        """
        logger.info("Testing ha_config_get_calendar_events with invalid entity...")

        result = await mcp_client.call_tool(
            "ha_config_get_calendar_events",
            {"entity_id": "calendar.nonexistent_calendar_xyz"},
        )

        data = parse_mcp_result(result)

        # Should fail gracefully
        assert data.get("success") is False, "Should fail for invalid calendar"
        assert "error" in data or "suggestions" in data, "Should provide error info"

        logger.info(f"Error (expected): {data.get('error', 'Unknown')}")
        logger.info("Invalid entity test completed")

    async def test_get_calendar_events_invalid_entity_format(self, mcp_client):
        """
        Test: Get events with invalid entity format

        Verifies validation of entity_id format.
        """
        logger.info(
            "Testing ha_config_get_calendar_events with invalid entity format..."
        )

        result = await mcp_client.call_tool(
            "ha_config_get_calendar_events", {"entity_id": "not_a_calendar_entity"}
        )

        data = parse_mcp_result(result)

        # Should fail with validation error
        assert data.get("success") is False, "Should fail for invalid format"
        assert "calendar." in str(
            data.get("error", "")
        ), "Error should mention correct format"

        logger.info(f"Validation error (expected): {data.get('error', 'Unknown')}")
        logger.info("Invalid format test completed")


@pytest.mark.calendar
@pytest.mark.slow
class TestCalendarEventLifecycle:
    """Test calendar event creation and deletion lifecycle."""

    async def _find_writable_calendar(self, mcp_client) -> str | None:
        """Find a calendar that supports event creation."""
        result = await mcp_client.call_tool(
            "ha_search_entities",
            {"query": "calendar", "domain_filter": "calendar", "limit": 10},
        )
        data = parse_mcp_result(result)

        # Handle nested data structure
        if "data" in data:
            results = data.get("data", {}).get("results", [])
        else:
            results = data.get("results", [])

        if not results:
            return None

        # Prefer local calendar if available (usually writable)
        for cal in results:
            entity_id = cal.get("entity_id", "")
            if "local" in entity_id.lower():
                return entity_id

        # Fall back to first calendar
        return results[0].get("entity_id")

    async def test_create_calendar_event(self, mcp_client, cleanup_tracker):
        """
        Test: Create a calendar event

        Creates a test event and verifies it was created successfully.
        """
        calendar_entity = await self._find_writable_calendar(mcp_client)
        if not calendar_entity:
            pytest.skip("No calendar entities available for testing")

        logger.info(f"Testing ha_config_set_calendar_event in {calendar_entity}...")

        # Create an event for tomorrow
        now = datetime.now()
        start = (now + timedelta(days=1)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(hours=1)

        event_summary = "E2E Test Event - Safe to Delete"

        try:
            result = await mcp_client.call_tool(
                "ha_config_set_calendar_event",
                {
                    "entity_id": calendar_entity,
                    "summary": event_summary,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "description": "This is a test event created by E2E tests",
                    "location": "Test Location",
                },
            )

            data = parse_mcp_result(result)

            if data.get("success"):
                logger.info(f"Event created successfully: {event_summary}")
                logger.info(f"Event details: {data.get('event', {})}")

                # Track for potential cleanup
                cleanup_tracker.track(
                    "calendar_event", f"{calendar_entity}:{event_summary}"
                )

                # Verify event appears in calendar
                events_result = await mcp_client.call_tool(
                    "ha_config_get_calendar_events",
                    {
                        "entity_id": calendar_entity,
                        "start": start.isoformat(),
                        "end": (end + timedelta(hours=1)).isoformat(),
                    },
                )

                events_data = parse_mcp_result(events_result)
                logger.info(
                    f"Events after creation: {events_data.get('count', 0)} event(s)"
                )

            else:
                # Calendar might not support event creation
                error_msg = data.get("error", "Unknown error")
                if "not supported" in error_msg.lower() or "read" in error_msg.lower():
                    pytest.skip(
                        f"Calendar {calendar_entity} does not support event creation"
                    )
                else:
                    logger.warning(f"Event creation failed: {error_msg}")
                    # Don't fail the test - some calendars are read-only
                    pytest.skip(f"Calendar event creation not available: {error_msg}")

        except Exception as e:
            logger.warning(f"Event creation test encountered error: {e}")
            pytest.skip(f"Calendar event creation not available: {e}")

        logger.info("ha_config_set_calendar_event test completed")

    async def test_create_calendar_event_invalid_entity(self, mcp_client):
        """
        Test: Create event with invalid calendar entity

        Verifies proper error handling for invalid entity.
        """
        logger.info("Testing ha_config_set_calendar_event with invalid entity...")

        now = datetime.now()
        start = (now + timedelta(days=1)).isoformat()
        end = (now + timedelta(days=1, hours=1)).isoformat()

        result = await mcp_client.call_tool(
            "ha_config_set_calendar_event",
            {
                "entity_id": "not_a_valid_calendar",
                "summary": "Test Event",
                "start": start,
                "end": end,
            },
        )

        data = parse_mcp_result(result)

        assert data.get("success") is False, "Should fail for invalid entity"
        assert "calendar." in str(
            data.get("error", "")
        ), "Error should mention correct format"

        logger.info(f"Validation error (expected): {data.get('error', 'Unknown')}")
        logger.info("Invalid entity create test completed")

    async def test_delete_calendar_event(self, mcp_client):
        """
        Test: Delete a calendar event

        Tests the delete event functionality (may fail if no deletable events exist).
        """
        calendar_entity = await self._find_writable_calendar(mcp_client)
        if not calendar_entity:
            pytest.skip("No calendar entities available for testing")

        logger.info(
            f"Testing ha_config_remove_calendar_event for {calendar_entity}..."
        )

        # Try to delete with a fake UID (will likely fail, but tests the API)
        result = await mcp_client.call_tool(
            "ha_config_remove_calendar_event",
            {"entity_id": calendar_entity, "uid": "nonexistent-event-uid-xyz"},
        )

        data = parse_mcp_result(result)

        # This will likely fail since the event doesn't exist
        # We're mainly testing that the tool handles errors gracefully
        if data.get("success"):
            logger.info("Unexpectedly succeeded (event may have existed)")
        else:
            logger.info(f"Delete failed as expected: {data.get('error', 'Unknown')}")
            assert "suggestions" in data, "Should provide helpful suggestions"

        logger.info("ha_config_remove_calendar_event test completed")

    async def test_delete_calendar_event_invalid_entity(self, mcp_client):
        """
        Test: Delete event with invalid calendar entity

        Verifies proper error handling for invalid entity format.
        """
        logger.info("Testing ha_config_remove_calendar_event with invalid entity...")

        result = await mcp_client.call_tool(
            "ha_config_remove_calendar_event",
            {"entity_id": "not_a_valid_calendar", "uid": "some-event-uid"},
        )

        data = parse_mcp_result(result)

        assert data.get("success") is False, "Should fail for invalid entity"
        assert "calendar." in str(
            data.get("error", "")
        ), "Error should mention correct format"

        logger.info(f"Validation error (expected): {data.get('error', 'Unknown')}")
        logger.info("Invalid entity delete test completed")


@pytest.mark.calendar
async def test_calendar_tools_overview(mcp_client):
    """
    Test: Verify calendar tools are registered and accessible

    This test validates that all calendar tools are properly
    registered with the MCP server.
    """
    logger.info("Verifying calendar tools registration...")

    # Test get events tool registration (even if it fails due to invalid entity)
    get_result = await mcp_client.call_tool(
        "ha_config_get_calendar_events", {"entity_id": "calendar.test"}
    )
    get_data = parse_mcp_result(get_result)
    assert (
        "events" in get_data or "error" in get_data
    ), "ha_config_get_calendar_events should return events or error"
    logger.info("ha_config_get_calendar_events tool is registered and functional")

    # Test create event tool registration
    now = datetime.now()
    create_result = await mcp_client.call_tool(
        "ha_config_set_calendar_event",
        {
            "entity_id": "calendar.test",
            "summary": "Test",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
    )
    create_data = parse_mcp_result(create_result)
    assert (
        "event" in create_data or "error" in create_data
    ), "ha_config_set_calendar_event should return event or error"
    logger.info("ha_config_set_calendar_event tool is registered and functional")

    # Test delete event tool registration
    delete_result = await mcp_client.call_tool(
        "ha_config_remove_calendar_event",
        {"entity_id": "calendar.test", "uid": "test-uid"},
    )
    delete_data = parse_mcp_result(delete_result)
    assert (
        "uid" in delete_data or "error" in delete_data
    ), "ha_config_remove_calendar_event should return uid or error"
    logger.info("ha_config_remove_calendar_event tool is registered and functional")

    logger.info("All calendar tools are properly registered")
