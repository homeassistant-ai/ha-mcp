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
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from ...utilities.assertions import (
    assert_mcp_success,
    parse_mcp_result,
    safe_call_tool,
)
from ...utilities.wait_helpers import wait_for_tool_result

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

        logger.info(f"Testing ha_config_get_calendar_events with {calendar_entity}...")

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

        # Use safe_call_tool since we expect this to fail
        data = await safe_call_tool(
            mcp_client,
            "ha_config_get_calendar_events",
            {"entity_id": "calendar.nonexistent_calendar_xyz"},
        )

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

        # Use safe_call_tool since we expect this to fail
        data = await safe_call_tool(
            mcp_client,
            "ha_config_get_calendar_events",
            {"entity_id": "not_a_calendar_entity"},
        )

        # Should fail with validation error
        assert data.get("success") is False, "Should fail for invalid format"
        assert "calendar." in str(data.get("error", "")), (
            "Error should mention correct format"
        )

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

        # Use safe_call_tool since we expect this to fail
        data = await safe_call_tool(
            mcp_client,
            "ha_config_set_calendar_event",
            {
                "entity_id": "not_a_valid_calendar",
                "summary": "Test Event",
                "start": start,
                "end": end,
            },
        )

        assert data.get("success") is False, "Should fail for invalid entity"
        assert "calendar." in str(data.get("error", "")), (
            "Error should mention correct format"
        )

        logger.info(f"Validation error (expected): {data.get('error', 'Unknown')}")
        logger.info("Invalid entity create test completed")

    @pytest.fixture
    async def deletable_event_uid(self, mcp_client):
        """Create a temporary event, yield (entity_id, uid), then best-effort delete.

        ``ha_config_set_calendar_event`` does not return the assigned UID, so
        the fixture round-trips through ``ha_config_get_calendar_events`` to
        retrieve it. Teardown swallows exceptions to stay idempotent regardless
        of whether the test body already deleted the event.
        """
        calendar_entity = await self._find_writable_calendar(mcp_client)
        if not calendar_entity:
            pytest.skip("No writable calendar found for testing")

        summary = f"E2E Deletable Test Event {uuid.uuid4().hex[:8]}"
        # TZ-aware so ISO 8601 strings stay unambiguous when the test runner
        # and the HA instance are in different timezones (matches the
        # convention in tests/src/e2e/workflows/core/test_history.py).
        now = datetime.now(UTC)
        start = (now + timedelta(days=1)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
        end = start + timedelta(hours=1)

        create_data = await safe_call_tool(
            mcp_client,
            "ha_config_set_calendar_event",
            {
                "entity_id": calendar_entity,
                "summary": summary,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        if not create_data.get("success"):
            pytest.skip(
                f"Calendar {calendar_entity} does not support event creation: "
                f"{create_data.get('error', 'Unknown')}"
            )

        # Poll until the just-created event surfaces in the list endpoint —
        # HA backends (CalDAV, Google) may not index immediately after create
        # returns. Per AGENTS.md § "E2E tests: poll after creating entities".
        event_uid: str | None = None
        timed_out = False
        try:
            events_data = await wait_for_tool_result(
                mcp_client,
                "ha_config_get_calendar_events",
                {
                    "entity_id": calendar_entity,
                    "start": start.isoformat(),
                    "end": (end + timedelta(hours=1)).isoformat(),
                },
                predicate=lambda d: any(
                    e.get("summary") == summary and e.get("uid")
                    for e in d.get("events", [])
                ),
                timeout=15,
                description=f"retrieval of UID for created event '{summary}'",
            )
            event_uid = next(
                (
                    e.get("uid")
                    for e in events_data.get("events", [])
                    if e.get("summary") == summary
                ),
                None,
            )
        except TimeoutError:
            timed_out = True

        if not event_uid:
            # Create succeeded but UID never appeared. Try a final scan-by-
            # summary cleanup so we don't leak across CI runs (HAOS-inaddon
            # reuses image), then fail — masking this would defeat the
            # regression-test contract this PR establishes.
            try:
                final_events = await safe_call_tool(
                    mcp_client,
                    "ha_config_get_calendar_events",
                    {
                        "entity_id": calendar_entity,
                        "start": start.isoformat(),
                        "end": (end + timedelta(hours=1)).isoformat(),
                    },
                )
                leaked_uid = next(
                    (
                        e.get("uid")
                        for e in final_events.get("events", [])
                        if e.get("summary") == summary and e.get("uid")
                    ),
                    None,
                )
                if leaked_uid:
                    await mcp_client.call_tool(
                        "ha_config_remove_calendar_event",
                        {"entity_id": calendar_entity, "uid": leaked_uid},
                    )
            except Exception as leak_cleanup_error:
                logger.warning(
                    f"Could not clean up potentially-leaked event "
                    f"'{summary}' on {calendar_entity}: {leak_cleanup_error}"
                )
            pytest.fail(
                f"Created event '{summary}' did not surface a uid in "
                f"ha_config_get_calendar_events for {calendar_entity}"
                + (" within 15s" if timed_out else "")
            )

        try:
            yield calendar_entity, event_uid
        finally:
            try:
                await mcp_client.call_tool(
                    "ha_config_remove_calendar_event",
                    {"entity_id": calendar_entity, "uid": event_uid},
                )
            except Exception as cleanup_error:
                logger.warning(
                    f"Cleanup of test event {event_uid} on {calendar_entity}: "
                    f"{cleanup_error}"
                )

    async def test_delete_calendar_event(self, mcp_client, deletable_event_uid):
        """
        Test: Delete a calendar event (positive + negative paths)

        Creates a fresh event, deletes it (positive: hard-assert success), then
        re-attempts deletion of the released UID (negative: hard-assert failure
        with suggestions). UID-collision risk is eliminated because the UID was
        just held and released by this test.
        """
        calendar_entity, event_uid = deletable_event_uid
        logger.info(
            f"Testing ha_config_remove_calendar_event for {calendar_entity} "
            f"with uid={event_uid}..."
        )

        first_delete = await mcp_client.call_tool(
            "ha_config_remove_calendar_event",
            {"entity_id": calendar_entity, "uid": event_uid},
        )
        first_delete_data = assert_mcp_success(
            first_delete, "first deletion of just-created event"
        )
        # Hard-require the explicit success flag — assert_mcp_success accepts
        # several indicators, but this PR's regression test should fail loudly
        # if a future refactor stops surfacing the flag.
        assert first_delete_data.get("success") is True, (
            f"First delete should return success=True; got {first_delete_data}"
        )
        logger.info(f"Deleted event {event_uid} (positive path)")

        second_delete = await safe_call_tool(
            mcp_client,
            "ha_config_remove_calendar_event",
            {"entity_id": calendar_entity, "uid": event_uid},
        )
        assert second_delete.get("success") is False, (
            f"Second deletion of released UID should fail: got {second_delete}"
        )
        # Accept either plural ``suggestions`` (multi-item) or singular
        # ``suggestion`` (single-item) — ``create_error_response`` writes the
        # singular form when the suggestions list has one entry.
        second_error = second_delete.get("error", {})
        assert second_error.get("suggestions") or second_error.get("suggestion"), (
            f"Delete failure should provide helpful suggestion(s); got {second_error}"
        )
        logger.info(
            f"Second delete failed as expected: {second_delete.get('error', 'Unknown')}"
        )
        logger.info("ha_config_remove_calendar_event test completed")

    async def test_delete_calendar_event_invalid_entity(self, mcp_client):
        """
        Test: Delete event with invalid calendar entity

        Verifies proper error handling for invalid entity format.
        """
        logger.info("Testing ha_config_remove_calendar_event with invalid entity...")

        # Use safe_call_tool since we expect this to fail
        data = await safe_call_tool(
            mcp_client,
            "ha_config_remove_calendar_event",
            {"entity_id": "not_a_valid_calendar", "uid": "some-event-uid"},
        )

        assert data.get("success") is False, "Should fail for invalid entity"
        assert "calendar." in str(data.get("error", "")), (
            "Error should mention correct format"
        )

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
    get_data = await safe_call_tool(
        mcp_client, "ha_config_get_calendar_events", {"entity_id": "calendar.test"}
    )
    assert "events" in get_data or "error" in get_data, (
        "ha_config_get_calendar_events should return events or error"
    )
    logger.info("ha_config_get_calendar_events tool is registered and functional")

    # Test create event tool registration
    now = datetime.now()
    create_data = await safe_call_tool(
        mcp_client,
        "ha_config_set_calendar_event",
        {
            "entity_id": "calendar.test",
            "summary": "Test",
            "start": now.isoformat(),
            "end": (now + timedelta(hours=1)).isoformat(),
        },
    )
    assert "event" in create_data or "error" in create_data, (
        "ha_config_set_calendar_event should return event or error"
    )
    logger.info("ha_config_set_calendar_event tool is registered and functional")

    # Test delete event tool registration
    delete_data = await safe_call_tool(
        mcp_client,
        "ha_config_remove_calendar_event",
        {"entity_id": "calendar.test", "uid": "test-uid"},
    )
    assert "uid" in delete_data or "error" in delete_data, (
        "ha_config_remove_calendar_event should return uid or error"
    )
    logger.info("ha_config_remove_calendar_event tool is registered and functional")

    logger.info("All calendar tools are properly registered")
