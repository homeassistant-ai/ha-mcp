"""
Calendar event management tools for Home Assistant MCP server.

This module provides tools for managing calendar events in Home Assistant,
including listing calendars, retrieving events, creating events, and deleting events.
"""

import logging
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)


def register_calendar_tools(mcp, client, **kwargs):
    """Register calendar management tools with the MCP server."""

    @mcp.tool
    async def ha_list_calendars() -> dict[str, Any]:
        """
        List all calendar entities in Home Assistant.

        Returns a list of calendar entities with their current state and attributes.

        **Example Usage:**
        ```python
        # List all available calendars
        calendars = ha_list_calendars()
        ```

        **Returns:**
        - List of calendar entities with entity_id, state, and friendly_name
        - Each calendar includes attributes like supported features
        """
        try:
            # Get all entity states and filter for calendar domain
            states = await client.get_states()
            calendars = []

            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("calendar."):
                    calendars.append(
                        {
                            "entity_id": entity_id,
                            "state": state.get("state"),
                            "friendly_name": state.get("attributes", {}).get(
                                "friendly_name", entity_id
                            ),
                            "attributes": state.get("attributes", {}),
                        }
                    )

            return {
                "success": True,
                "calendars": calendars,
                "count": len(calendars),
                "message": f"Found {len(calendars)} calendar(s)",
            }

        except Exception as error:
            logger.error(f"Failed to list calendars: {error}")
            return {
                "success": False,
                "error": str(error),
                "calendars": [],
                "suggestions": [
                    "Verify Home Assistant connection is active",
                    "Check if calendar integrations are configured",
                    "Try ha_search_entities(query='calendar') for entity search",
                ],
            }

    @mcp.tool
    async def ha_get_calendar_events(
        entity_id: str,
        start: str | None = None,
        end: str | None = None,
        max_results: int = 20,
    ) -> dict[str, Any]:
        """
        Get events from a calendar entity.

        Retrieves calendar events within a specified time range.

        **Parameters:**
        - entity_id: Calendar entity ID (e.g., 'calendar.family')
        - start: Start datetime in ISO format (default: now)
        - end: End datetime in ISO format (default: 7 days from start)
        - max_results: Maximum number of events to return (default: 20)

        **Example Usage:**
        ```python
        # Get events for the next week
        events = ha_get_calendar_events("calendar.family")

        # Get events for a specific date range
        events = ha_get_calendar_events(
            "calendar.work",
            start="2024-01-01T00:00:00",
            end="2024-01-31T23:59:59"
        )
        ```

        **Returns:**
        - List of calendar events with summary, start, end, description, location
        """
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                return {
                    "success": False,
                    "error": f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Use ha_list_calendars() to find available calendar entities",
                        "Calendar entity IDs start with 'calendar.' prefix",
                    ],
                }

            # Set default time range if not provided
            now = datetime.now()
            if start is None:
                start = now.isoformat()
            if end is None:
                end_date = now + timedelta(days=7)
                end = end_date.isoformat()

            # Build the API endpoint for calendar events
            # Home Assistant uses: GET /api/calendars/{entity_id}?start=...&end=...
            params = {"start": start, "end": end}

            # Use the REST client to fetch calendar events
            # The endpoint is /calendars/{entity_id} (note: without /api prefix as client adds it)
            response = await client._request(
                "GET", f"/calendars/{entity_id}", params=params
            )

            # Response is a list of events
            events = response if isinstance(response, list) else []

            # Limit results
            limited_events = events[:max_results]

            return {
                "success": True,
                "entity_id": entity_id,
                "events": limited_events,
                "count": len(limited_events),
                "total_available": len(events),
                "time_range": {
                    "start": start,
                    "end": end,
                },
                "message": f"Retrieved {len(limited_events)} event(s) from {entity_id}",
            }

        except Exception as error:
            error_str = str(error)
            logger.error(f"Failed to get calendar events for {entity_id}: {error}")

            # Provide helpful error messages
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists using ha_list_calendars()",
                "Check start/end datetime format (ISO 8601)",
                "Ensure calendar integration supports event retrieval",
            ]

            if "404" in error_str or "not found" in error_str.lower():
                suggestions.insert(0, f"Calendar entity '{entity_id}' not found")

            return {
                "success": False,
                "error": error_str,
                "entity_id": entity_id,
                "events": [],
                "suggestions": suggestions,
            }

    @mcp.tool
    async def ha_create_calendar_event(
        entity_id: str,
        summary: str,
        start: str,
        end: str,
        description: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        """
        Create a new event in a calendar.

        Creates a calendar event using the calendar.create_event service.

        **Parameters:**
        - entity_id: Calendar entity ID (e.g., 'calendar.family')
        - summary: Event title/summary
        - start: Event start datetime in ISO format
        - end: Event end datetime in ISO format
        - description: Optional event description
        - location: Optional event location

        **Example Usage:**
        ```python
        # Create a simple event
        result = ha_create_calendar_event(
            "calendar.family",
            summary="Doctor appointment",
            start="2024-01-15T14:00:00",
            end="2024-01-15T15:00:00"
        )

        # Create an event with details
        result = ha_create_calendar_event(
            "calendar.work",
            summary="Team meeting",
            start="2024-01-16T10:00:00",
            end="2024-01-16T11:00:00",
            description="Weekly sync meeting",
            location="Conference Room A"
        )
        ```

        **Returns:**
        - Success status and event details
        """
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                return {
                    "success": False,
                    "error": f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Use ha_list_calendars() to find available calendar entities",
                        "Calendar entity IDs start with 'calendar.' prefix",
                    ],
                }

            # Build service data
            service_data: dict[str, Any] = {
                "entity_id": entity_id,
                "summary": summary,
                "start_date_time": start,
                "end_date_time": end,
            }

            if description:
                service_data["description"] = description
            if location:
                service_data["location"] = location

            # Call the calendar.create_event service
            result = await client.call_service("calendar", "create_event", service_data)

            return {
                "success": True,
                "entity_id": entity_id,
                "event": {
                    "summary": summary,
                    "start": start,
                    "end": end,
                    "description": description,
                    "location": location,
                },
                "result": result,
                "message": f"Successfully created event '{summary}' in {entity_id}",
            }

        except Exception as error:
            error_str = str(error)
            logger.error(f"Failed to create calendar event in {entity_id}: {error}")

            suggestions = [
                f"Verify calendar entity '{entity_id}' exists and supports event creation",
                "Check datetime format (ISO 8601)",
                "Ensure end time is after start time",
                "Some calendar integrations may be read-only",
            ]

            if "404" in error_str or "not found" in error_str.lower():
                suggestions.insert(0, f"Calendar entity '{entity_id}' not found")
            if "not supported" in error_str.lower():
                suggestions.insert(0, "This calendar does not support event creation")

            return {
                "success": False,
                "error": error_str,
                "entity_id": entity_id,
                "event": {
                    "summary": summary,
                    "start": start,
                    "end": end,
                },
                "suggestions": suggestions,
            }

    @mcp.tool
    async def ha_delete_calendar_event(
        entity_id: str,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> dict[str, Any]:
        """
        Delete an event from a calendar.

        Deletes a calendar event using the calendar.delete_event service.

        **Parameters:**
        - entity_id: Calendar entity ID (e.g., 'calendar.family')
        - uid: Unique identifier of the event to delete
        - recurrence_id: Optional recurrence ID for recurring events
        - recurrence_range: Optional recurrence range ('THIS_AND_FUTURE' to delete this and future occurrences)

        **Example Usage:**
        ```python
        # Delete a single event
        result = ha_delete_calendar_event(
            "calendar.family",
            uid="event-12345"
        )

        # Delete a recurring event instance and future occurrences
        result = ha_delete_calendar_event(
            "calendar.work",
            uid="recurring-event-67890",
            recurrence_id="20240115T100000",
            recurrence_range="THIS_AND_FUTURE"
        )
        ```

        **Note:**
        To get the event UID, first use ha_get_calendar_events() to list events.
        The UID is returned in each event's data.

        **Returns:**
        - Success status and deletion confirmation
        """
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                return {
                    "success": False,
                    "error": f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                    "entity_id": entity_id,
                    "suggestions": [
                        "Use ha_list_calendars() to find available calendar entities",
                        "Calendar entity IDs start with 'calendar.' prefix",
                    ],
                }

            # Build service data
            service_data: dict[str, Any] = {
                "entity_id": entity_id,
                "uid": uid,
            }

            if recurrence_id:
                service_data["recurrence_id"] = recurrence_id
            if recurrence_range:
                service_data["recurrence_range"] = recurrence_range

            # Call the calendar.delete_event service
            result = await client.call_service("calendar", "delete_event", service_data)

            return {
                "success": True,
                "entity_id": entity_id,
                "uid": uid,
                "recurrence_id": recurrence_id,
                "recurrence_range": recurrence_range,
                "result": result,
                "message": f"Successfully deleted event '{uid}' from {entity_id}",
            }

        except Exception as error:
            error_str = str(error)
            logger.error(f"Failed to delete calendar event from {entity_id}: {error}")

            suggestions = [
                f"Verify calendar entity '{entity_id}' exists",
                f"Verify event with UID '{uid}' exists in the calendar",
                "Use ha_get_calendar_events() to find the correct event UID",
                "Some calendar integrations may not support event deletion",
            ]

            if "404" in error_str or "not found" in error_str.lower():
                suggestions.insert(
                    0, f"Calendar entity '{entity_id}' or event '{uid}' not found"
                )
            if "not supported" in error_str.lower():
                suggestions.insert(0, "This calendar does not support event deletion")

            return {
                "success": False,
                "error": error_str,
                "entity_id": entity_id,
                "uid": uid,
                "suggestions": suggestions,
            }
