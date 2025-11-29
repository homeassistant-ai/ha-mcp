"""
Calendar event management tools for Home Assistant MCP server.

This module provides tools for managing calendar events in Home Assistant,
including retrieving events, creating events, and deleting events.

Use ha_search_entities(query='calendar', domain_filter='calendar') to find calendar entities.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import Field

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_calendar_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register calendar management tools with the MCP server."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Calendar Events"})
    @log_tool_usage
    async def ha_config_get_calendar_events(
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        start: Annotated[
            str | None,
            Field(
                description="Start datetime in ISO format (default: now)", default=None
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description="End datetime in ISO format (default: 7 days from start)",
                default=None,
            ),
        ] = None,
        max_results: Annotated[
            int,
            Field(description="Maximum number of events to return", default=20),
        ] = 20,
    ) -> dict[str, Any]:
        """
        Retrieve calendar events from a calendar entity.

        Retrieves calendar events within a specified time range.

        **Parameters:**
        - entity_id: Calendar entity ID (e.g., 'calendar.family')
        - start: Start datetime in ISO format (default: now)
        - end: End datetime in ISO format (default: 7 days from start)
        - max_results: Maximum number of events to return (default: 20)

        **Example Usage:**
        ```python
        # Get events for the next week
        events = ha_config_get_calendar_events("calendar.family")

        # Get events for a specific date range
        events = ha_config_get_calendar_events(
            "calendar.work",
            start="2024-01-01T00:00:00",
            end="2024-01-31T23:59:59"
        )
        ```

        **Note:** To find calendar entities, use ha_search_entities(query='calendar', domain_filter='calendar')

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
                        "Use ha_search_entities(query='calendar', domain_filter='calendar') to find calendar entities",
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
                f"Verify calendar entity '{entity_id}' exists using ha_search_entities(query='calendar', domain_filter='calendar')",
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

    @mcp.tool(annotations={"idempotentHint": True, "title": "Create or Update Calendar Event"})
    @log_tool_usage
    async def ha_config_set_calendar_event(
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        summary: Annotated[str, Field(description="Event title/summary")],
        start: Annotated[
            str, Field(description="Event start datetime in ISO format")
        ],
        end: Annotated[str, Field(description="Event end datetime in ISO format")],
        description: Annotated[
            str | None,
            Field(description="Optional event description", default=None),
        ] = None,
        location: Annotated[
            str | None, Field(description="Optional event location", default=None)
        ] = None,
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
        result = ha_config_set_calendar_event(
            "calendar.family",
            summary="Doctor appointment",
            start="2024-01-15T14:00:00",
            end="2024-01-15T15:00:00"
        )

        # Create an event with details
        result = ha_config_set_calendar_event(
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
                        "Use ha_search_entities(query='calendar', domain_filter='calendar') to find calendar entities",
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

    @mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "title": "Remove Calendar Event"})
    @log_tool_usage
    async def ha_config_remove_calendar_event(
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        uid: Annotated[str, Field(description="Unique identifier of the event to delete")],
        recurrence_id: Annotated[
            str | None,
            Field(description="Optional recurrence ID for recurring events", default=None),
        ] = None,
        recurrence_range: Annotated[
            str | None,
            Field(
                description="Optional recurrence range ('THIS_AND_FUTURE' to delete this and future occurrences)",
                default=None,
            ),
        ] = None,
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
        result = ha_config_remove_calendar_event(
            "calendar.family",
            uid="event-12345"
        )

        # Delete a recurring event instance and future occurrences
        result = ha_config_remove_calendar_event(
            "calendar.work",
            uid="recurring-event-67890",
            recurrence_id="20240115T100000",
            recurrence_range="THIS_AND_FUTURE"
        )
        ```

        **Note:**
        To get the event UID, first use ha_config_get_calendar_events() to list events.
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
                        "Use ha_search_entities(query='calendar', domain_filter='calendar') to find calendar entities",
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
                "Use ha_config_get_calendar_events() to find the correct event UID",
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
