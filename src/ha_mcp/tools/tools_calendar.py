"""
Calendar event management tools for Home Assistant MCP server.

This module provides tools for managing calendar events in Home Assistant,
including retrieving events, creating events, updating existing events, and
deleting events.

Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities.
"""

import logging
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import Field

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import tool

from ..client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .util_helpers import is_connection_error_message

logger = logging.getLogger(__name__)


def _validate_recurrence_target(
    entity_id: str,
    recurrence_id: str | None,
    recurrence_range: Literal["THISANDFUTURE"] | None,
) -> None:
    """Reject recurrence targeting that Home Assistant would silently widen.

    A range starts AT the identified occurrence, so Home Assistant ignores
    ``recurrence_range`` unless ``recurrence_id`` is set (ical gates the whole
    fork on it). Without the identifier an update then edits the master event
    and a delete removes the ENTIRE series rather than this occurrence and the
    following ones — both silently, and both reported as success.
    """
    if recurrence_id is not None:
        validate_identifier_not_empty(
            recurrence_id,
            "recurrence_id",
            suggestions=[
                (
                    "Use ha_config_get_calendar_events() to list occurrences "
                    "and obtain a valid recurrence_id"
                ),
                "Omit recurrence_id to target the whole series",
            ],
            context={"entity_id": entity_id},
        )
    if recurrence_range is not None and recurrence_id is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "recurrence_range requires recurrence_id: the range starts at "
                "the identified occurrence",
                context={
                    "entity_id": entity_id,
                    "recurrence_range": recurrence_range,
                },
                suggestions=[
                    "Pass the recurrence_id of the occurrence the range starts at",
                    "Omit recurrence_range to target the whole series",
                    "Use ha_config_get_calendar_events() to list occurrences",
                ],
            )
        )


class CalendarTools:
    """Calendar event management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_calendar_events",
        tags={"Calendar"},
        annotations={
            "openWorldHint": False,
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Calendar Events",
        },
    )
    @log_tool_usage
    async def ha_config_get_calendar_events(
        self,
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

        **Note:** To find calendar entities, use ha_search(query='calendar', domain_filter='calendar')

        **Returns:**
        - List of calendar events with summary, start, end, description, location
        """
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities",
                            "Calendar entity IDs start with 'calendar.' prefix",
                        ],
                    )
                )

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
            response = await self._client._request(
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

        except ToolError:
            raise
        except Exception as error:
            logger.error(f"Failed to get calendar events for {entity_id}: {error}")

            # Provide helpful error messages
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists using ha_search(query='calendar', domain_filter='calendar')",
                "Check start/end datetime format (ISO 8601)",
                "Ensure calendar integration supports event retrieval",
            ]

            error_str = str(error)
            if "404" in error_str or "not found" in error_str.lower():
                suggestions.insert(0, f"Calendar entity '{entity_id}' not found")

            exception_to_structured_error(
                error, context={"entity_id": entity_id}, suggestions=suggestions
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _create_recurring_calendar_event(
        self,
        entity_id: str,
        summary: str,
        start: str,
        end: str,
        description: str | None,
        location: str | None,
        rrule: str,
    ) -> Any:
        """Create a recurring calendar event series via the WebSocket API.

        The ``calendar.create_event`` service schema has no rrule field --
        recurrence is only accepted by the WebSocket command
        ``calendar/event/create`` (see HA Core
        ``homeassistant/components/calendar/__init__.py``), the same split
        that forces the delete tool below onto the WebSocket API.
        """
        event: dict[str, Any] = {
            "summary": summary,
            "dtstart": start,
            "dtend": end,
            "rrule": rrule,
        }
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location

        # Route through the shared pooled WebSocket (issue #1813) instead of a
        # dedicated connect/auth handshake per call. The pooled path collapses a
        # failed WS command into ``{"success": False, "error": ...}``; a
        # transport-shaped failure re-raises as ``HomeAssistantConnectionError``
        # (the classifier type-matches it to connectivity guidance), anything
        # else as the same ``HomeAssistantCommandError`` the dedicated
        # send_command used to raise so the caller's error handler still
        # attaches the rrule-specific suggestions.
        result = await self._client.send_websocket_message(
            {"type": "calendar/event/create", "entity_id": entity_id, "event": event}
        )
        if not result.get("success"):
            error = str(result.get("error", "calendar/event/create failed"))
            if is_connection_error_message(error):
                raise HomeAssistantConnectionError(error)
            raise HomeAssistantCommandError(error)
        return result

    async def _create_simple_calendar_event(
        self,
        entity_id: str,
        summary: str,
        start: str,
        end: str,
        description: str | None,
        location: str | None,
    ) -> Any:
        """Create a one-off calendar event via the calendar.create_event service."""
        start_is_date = self._is_date_only(start)

        service_data: dict[str, Any] = {
            "entity_id": entity_id,
            "summary": summary,
        }
        if start_is_date:
            service_data.update({"start_date": start, "end_date": end})
        else:
            service_data.update({"start_date_time": start, "end_date_time": end})

        if description is not None:
            service_data["description"] = description
        if location is not None:
            service_data["location"] = location

        return await self._client.call_service("calendar", "create_event", service_data)

    async def _update_calendar_event(
        self,
        entity_id: str,
        uid: str,
        summary: str,
        start: str,
        end: str,
        description: str | None,
        location: str | None,
        rrule: str | None,
        recurrence_id: str | None,
        recurrence_range: str | None,
    ) -> Any:
        """Update an existing calendar event via the WebSocket API.

        HA registers only ``create_event`` and ``get_events`` as REST services
        (see HA Core ``homeassistant/components/calendar/__init__.py``);
        ``calendar/event/update`` is WebSocket-only, the same split that forces
        the delete tool onto the WebSocket API.
        """
        event: dict[str, Any] = {
            "summary": summary,
            "dtstart": start,
            "dtend": end,
        }
        if description is not None:
            event["description"] = description
        if location is not None:
            event["location"] = location
        if rrule is not None:
            event["rrule"] = rrule

        ws_kwargs: dict[str, Any] = {"entity_id": entity_id, "uid": uid}
        if recurrence_id:
            ws_kwargs["recurrence_id"] = recurrence_id
        if recurrence_range:
            ws_kwargs["recurrence_range"] = recurrence_range

        # Same pooled-WebSocket routing and failure mapping as the create and
        # delete paths (issue #1813): a transport-shaped failure becomes
        # ``HomeAssistantConnectionError`` so the classifier attaches
        # connectivity guidance, anything else ``HomeAssistantCommandError`` so
        # the caller's handler attaches the update-specific suggestions.
        result = await self._client.send_websocket_message(
            {"type": "calendar/event/update", **ws_kwargs, "event": event}
        )
        if not result.get("success"):
            error = str(result.get("error", "calendar/event/update failed"))
            if is_connection_error_message(error):
                raise HomeAssistantConnectionError(error)
            raise HomeAssistantCommandError(error)
        return result

    @staticmethod
    def _is_date_only(value: str) -> bool:
        """Return whether value is a valid date in strict YYYY-MM-DD form."""
        try:
            return (
                len(value) == 10
                and datetime.strptime(value, "%Y-%m-%d").date().isoformat() == value
            )
        except ValueError:
            return False

    def _build_set_calendar_event_error_suggestions(
        self,
        entity_id: str,
        rrule: str | None,
        error: Exception,
        start: str,
        end: str,
        uid: str | None = None,
    ) -> list[str]:
        """Build suggestions for a failed ha_config_set_calendar_event call.

        ``uid`` selects update-mode wording; ``None`` keeps the create-mode
        suggestions.
        """
        if isinstance(error, HomeAssistantConnectionError):
            # A transport drop is not a calendar problem — domain hints would
            # send the agent chasing a non-issue during an HA restart.
            return [
                "Home Assistant may be restarting or unreachable — retry shortly",
                "Check the connection to Home Assistant",
            ]
        if uid is not None:
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists and supports event update",
                f"Verify event with UID '{uid}' exists in the calendar",
                "Use ha_config_get_calendar_events() to find the correct event UID",
                "Check datetime format (ISO 8601)",
                "Ensure end time is after start time",
                (
                    "Not every calendar integration supports updating events "
                    "(Local Calendar does; the core Google and CalDAV "
                    "integrations do not)"
                ),
            ]
        else:
            suggestions = [
                f"Verify calendar entity '{entity_id}' exists and supports event creation",
                "Check datetime format (ISO 8601)",
                "Ensure end time is after start time",
                "Some calendar integrations may be read-only",
            ]
        if rrule:
            suggestions.insert(
                0,
                "Check RRULE syntax (RFC 5545, without the 'RRULE:' prefix, "
                "e.g. 'FREQ=WEEKLY;BYDAY=MO') and that this calendar "
                "integration supports recurring events",
            )

        error_str = str(error)
        if "404" in error_str or "not found" in error_str.lower():
            suggestions.insert(
                0,
                f"Calendar entity '{entity_id}' or event '{uid}' not found"
                if uid is not None
                else f"Calendar entity '{entity_id}' not found",
            )
        # Update always travels the WebSocket path, which preserves HA's
        # humanized message verbatim ("Calendar does not support event
        # update"); on the create side that only happens on the WebSocket
        # (rrule) path. Both spellings are matched because HA words the
        # WebSocket message as "does not support" while other layers say "not
        # supported". The REST service path loses the text entirely: a
        # read-only calendar raises ServiceNotSupported, which HA's handler
        # does not map (see homeassistant/helpers/http.py — only vol.Invalid,
        # ServiceNotFound and Unauthorized are), so the client sees a bodyless
        # status line. Do NOT broaden this to match that generic shape: it
        # would promote "does not support event creation" onto every validation
        # failure. The REST path keeps the read-only hint via the unconditional
        # "Some calendar integrations may be read-only" suggestion above.
        lowered = error_str.lower()
        if "not supported" in lowered or "does not support" in lowered:
            suggestions.insert(
                0,
                "This calendar does not support event update"
                if uid is not None
                else "This calendar does not support event creation",
            )
        # HA treats the all-day end date as exclusive and enforces
        # MIN_NEW_EVENT_DURATION (1 second), so an all-day range with
        # end <= start is rejected. Key this on the boundaries we already hold
        # rather than on HA's wording: the REST service path raises a bare
        # HTTPBadRequest, so voluptuous' "Expected minimum event duration"
        # text never reaches the client (only the WebSocket rrule path keeps
        # it). Strict YYYY-MM-DD sorts lexicographically, so comparing the
        # strings is an exact date comparison here.
        if self._is_date_only(start) and self._is_date_only(end) and end <= start:
            suggestions.insert(
                0,
                "For an all-day event the end date is exclusive — set end to "
                "start + 1 day (a single-day all-day event cannot have "
                "start == end)",
            )

        return suggestions

    @tool(
        name="ha_config_set_calendar_event",
        tags={"Calendar"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "title": "Create or Update Calendar Event",
        },
    )
    @with_auto_backup(
        domain="calendar_event",
        # Skip on missing entity_id or uid; falsy "" beats the truthy
        # "::" shape that would hit the fetch with no record to find. Create
        # mode carries no uid, so only an update snapshots the prior event.
        id_fn=lambda kw: (
            f"{kw['entity_id']}::{kw['uid']}"
            if kw.get("entity_id") and kw.get("uid")
            else ""
        ),
    )
    @log_tool_usage
    async def ha_config_set_calendar_event(
        self,
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        summary: Annotated[str, Field(description="Event title/summary")],
        start: Annotated[
            str, Field(description="Event start date or datetime in ISO format")
        ],
        end: Annotated[
            str,
            Field(
                description=(
                    "Event end date or datetime in ISO format. For all-day "
                    "events (date-only) the end date is exclusive; a "
                    "single-day all-day event needs end = start + 1 day."
                )
            ),
        ],
        description: Annotated[
            str | None,
            Field(description="Optional event description", default=None),
        ] = None,
        location: Annotated[
            str | None, Field(description="Optional event location", default=None)
        ] = None,
        rrule: Annotated[
            str | None,
            Field(
                description=(
                    "Optional RFC 5545 recurrence rule, without 'RRULE:' prefix "
                    "(e.g., 'FREQ=WEEKLY;BYDAY=MO' or 'FREQ=MONTHLY;BYDAY=3SA'). "
                    "Creates a recurring event series."
                ),
                default=None,
            ),
        ] = None,
        uid: Annotated[
            str | None,
            Field(
                description=(
                    "UID of an existing event to update. Omit to create a new "
                    "event. Get UIDs from ha_config_get_calendar_events."
                ),
                default=None,
            ),
        ] = None,
        recurrence_id: Annotated[
            str | None,
            Field(
                description=(
                    "Only meaningful with 'uid': identifies one occurrence of a "
                    "recurring series to update."
                ),
                default=None,
            ),
        ] = None,
        recurrence_range: Annotated[
            Literal["THISANDFUTURE"] | None,
            Field(
                description=(
                    "Only meaningful with 'uid': 'THISANDFUTURE' to update "
                    "this and all following occurrences. Home Assistant "
                    "compares this value verbatim, so no other spelling "
                    "(including 'THIS_AND_FUTURE') selects the range."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create a new event in a calendar, or update an existing one.

        Creates a one-off event via the calendar.create_event service, or a
        recurring series via the WebSocket ``calendar/event/create`` command
        when ``rrule`` is provided (the REST service schema does not accept
        recurrence rules). Passing ``uid`` switches to update mode, which uses
        the WebSocket ``calendar/event/update`` command — HA registers no REST
        service for updating an event.

        **When NOT to use:**
        - To retrieve calendar events, use ``ha_config_get_calendar_events``.
        - To delete an event, use ``ha_config_remove_calendar_event``.
        - To find the ``uid`` of an event to update, use
          ``ha_config_get_calendar_events``; this tool does not search.

        **Example Usage:**
        ```python
        # Create a simple event
        result = ha_config_set_calendar_event(
            "calendar.family",
            summary="Doctor appointment",
            start="2024-01-15T14:00:00",
            end="2024-01-15T15:00:00"
        )

        # Update an existing event (uid from ha_config_get_calendar_events).
        # The event is REPLACED, so re-supply every field you want to keep.
        result = ha_config_set_calendar_event(
            "calendar.family",
            summary="Doctor appointment (rescheduled)",
            start="2024-01-15T16:00:00",
            end="2024-01-15T17:00:00",
            location="Clinic",
            uid="event-12345"
        )

        # Update one occurrence of a recurring series and all later ones
        result = ha_config_set_calendar_event(
            "calendar.work",
            summary="Team meeting (new time)",
            start="2024-02-05T11:00:00",
            end="2024-02-05T12:00:00",
            uid="recurring-event-67890",
            recurrence_id="20240205T100000",
            recurrence_range="THISANDFUTURE"
        )

        # Create a recurring event (every Monday, 10 occurrences)
        result = ha_config_set_calendar_event(
            "calendar.work",
            summary="Team meeting",
            start="2024-01-15T10:00:00",
            end="2024-01-15T11:00:00",
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10"
        )

        # Create an all-day event (date-only, no time component). The end
        # date is EXCLUSIVE, so this spans 2026-07-04 through 2026-07-10.
        result = ha_config_set_calendar_event(
            "calendar.family",
            summary="Vacation",
            start="2026-07-04",
            end="2026-07-11"
        )
        ```

        **Note:**
        Passing date-only values (``YYYY-MM-DD``) for both ``start`` and
        ``end`` creates an all-day event; passing full ISO datetimes creates
        a timed event. The two forms cannot be mixed — a date-only ``start``
        with a datetime ``end`` (or vice versa) is rejected. Because the
        all-day ``end`` date is exclusive, a single-day all-day event must
        set ``end`` to ``start + 1 day``.

        An update replaces the whole event rather than patching it, so
        ``summary``, ``start`` and ``end`` stay required in update mode and any
        ``description``, ``location`` or ``rrule`` that is not re-supplied is
        dropped from the event.

        Not every calendar integration supports event creation; recurring
        events additionally require the integration to support recurrence
        (the built-in Local Calendar does). Update support is narrower still:
        Local Calendar implements it, while the core Google Calendar and CalDAV
        integrations do not.

        **Returns:**
        - Success status and event details
        """
        try:
            # Validate entity_id
            if not entity_id.startswith("calendar."):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities",
                            "Calendar entity IDs start with 'calendar.' prefix",
                        ],
                    )
                )

            if uid is not None:
                # An empty/whitespace uid would reach the WS command and HA
                # returns a misleading "event not found".
                validate_identifier_not_empty(
                    uid,
                    "uid",
                    suggestions=[
                        "Use ha_config_get_calendar_events() to list events and obtain valid UIDs",
                        "Omit uid to create a new event instead of updating one",
                    ],
                    context={"entity_id": entity_id},
                )
            elif recurrence_id is not None or recurrence_range is not None:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "recurrence_id and recurrence_range only apply when updating an event (uid is required)",
                        context={
                            "entity_id": entity_id,
                            "recurrence_id": recurrence_id,
                            "recurrence_range": recurrence_range,
                        },
                        suggestions=[
                            "Pass uid to update an existing event's occurrence",
                            "Omit recurrence_id and recurrence_range to create a new event",
                            "Use rrule to create a new recurring series",
                        ],
                    )
                )

            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)

            # Reject mixed date/datetime up front so the simple, recurring
            # (rrule) and update paths give the same clear validation error —
            # only the simple path routes through
            # ``_create_simple_calendar_event`` where this used to live.
            if self._is_date_only(start) != self._is_date_only(end):
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        "Calendar event start and end must both be dates or both be datetimes",
                        context={"start": start, "end": end},
                        suggestions=[
                            "Use YYYY-MM-DD for both values to create an all-day event",
                            "Use ISO 8601 datetimes for both values to create a timed event",
                        ],
                    )
                )

            event_details: dict[str, Any] = {
                "summary": summary,
                "start": start,
                "end": end,
                "description": description,
                "location": location,
                "rrule": rrule,
            }

            if uid is not None:
                result = await self._update_calendar_event(
                    entity_id,
                    uid,
                    summary,
                    start,
                    end,
                    description,
                    location,
                    rrule,
                    recurrence_id,
                    recurrence_range,
                )
                return {
                    "success": True,
                    "entity_id": entity_id,
                    "uid": uid,
                    "recurrence_id": recurrence_id,
                    "recurrence_range": recurrence_range,
                    "event": event_details,
                    "result": result,
                    "message": f"Successfully updated event '{uid}' in {entity_id}",
                }

            if rrule:
                result = await self._create_recurring_calendar_event(
                    entity_id, summary, start, end, description, location, rrule
                )
            else:
                result = await self._create_simple_calendar_event(
                    entity_id, summary, start, end, description, location
                )

            return {
                "success": True,
                "entity_id": entity_id,
                "event": event_details,
                "result": result,
                "message": f"Successfully created event '{summary}' in {entity_id}",
            }

        except ToolError:
            raise
        except Exception as error:
            action = "update" if uid is not None else "create"
            logger.error(f"Failed to {action} calendar event in {entity_id}: {error}")

            suggestions = self._build_set_calendar_event_error_suggestions(
                entity_id, rrule, error, start, end, uid
            )

            context: dict[str, Any] = {"entity_id": entity_id}
            if uid is not None:
                context["uid"] = uid

            exception_to_structured_error(
                error, context=context, suggestions=suggestions
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_config_remove_calendar_event",
        tags={"Calendar"},
        annotations={
            "openWorldHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Remove Calendar Event",
        },
    )
    @with_auto_backup(
        domain="calendar_event",
        # Skip on missing entity_id or uid; falsy "" beats the truthy
        # "::" shape that would hit the fetch with no record to find.
        id_fn=lambda kw: (
            f"{kw['entity_id']}::{kw['uid']}"
            if kw.get("entity_id") and kw.get("uid")
            else ""
        ),
    )
    @log_tool_usage
    async def ha_config_remove_calendar_event(
        self,
        entity_id: Annotated[
            str, Field(description="Calendar entity ID (e.g., 'calendar.family')")
        ],
        uid: Annotated[
            str, Field(description="Unique identifier of the event to delete")
        ],
        recurrence_id: Annotated[
            str | None,
            Field(
                description="Optional recurrence ID for recurring events", default=None
            ),
        ] = None,
        recurrence_range: Annotated[
            Literal["THISANDFUTURE"] | None,
            Field(
                description=(
                    "Optional recurrence range ('THISANDFUTURE' to delete this "
                    "and future occurrences). Home Assistant compares this "
                    "value verbatim, so no other spelling (including "
                    "'THIS_AND_FUTURE') selects the range."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Delete an event from a calendar.

        Deletes a calendar event via the WebSocket ``calendar/event/delete``
        command. HA's calendar component only registers ``create_event`` and
        ``get_events`` as REST services — delete and update live on the
        WebSocket API only.

        **Parameters:**
        - entity_id: Calendar entity ID (e.g., 'calendar.family')
        - uid: Unique identifier of the event to delete
        - recurrence_id: Optional recurrence ID for recurring events
        - recurrence_range: Optional recurrence range ('THISANDFUTURE' to delete this and future occurrences)

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
            recurrence_range="THISANDFUTURE"
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
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_INVALID_PARAMETER,
                        f"Invalid calendar entity ID: {entity_id}. Must start with 'calendar.'",
                        context={"entity_id": entity_id},
                        suggestions=[
                            "Use ha_search(query='calendar', domain_filter='calendar') to find calendar entities",
                            "Calendar entity IDs start with 'calendar.' prefix",
                        ],
                    )
                )

            # entity_id format-check above does not cover the ``uid`` parameter.
            # Empty/whitespace uid would flow through to the WS command and HA
            # returns a misleading "event not found".
            validate_identifier_not_empty(
                uid,
                "uid",
                suggestions=[
                    "Use ha_config_get_calendar_events() to list events and obtain valid UIDs",
                ],
                context={"entity_id": entity_id},
            )

            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)

            # ``calendar.delete_event`` is NOT a REST service — HA only
            # registers ``calendar.create_event`` and ``calendar.get_events``.
            # Delete is exposed exclusively via the WebSocket command
            # ``calendar/event/delete`` (see HA Core
            # ``homeassistant/components/calendar/__init__.py``).
            ws_kwargs: dict[str, Any] = {"entity_id": entity_id, "uid": uid}
            if recurrence_id:
                ws_kwargs["recurrence_id"] = recurrence_id
            if recurrence_range:
                ws_kwargs["recurrence_range"] = recurrence_range

            # Route through the shared pooled WebSocket (issue #1813) instead of a
            # dedicated connect/auth handshake per call. Transport-shaped
            # failures raise ``HomeAssistantConnectionError`` (connectivity
            # guidance); anything else re-raises as the same
            # ``HomeAssistantCommandError`` the dedicated send_command used to
            # raise so the outer handler builds the delete-specific
            # suggestions (404 / not-supported).
            result = await self._client.send_websocket_message(
                {"type": "calendar/event/delete", **ws_kwargs}
            )
            if not result.get("success"):
                error = str(result.get("error", "calendar/event/delete failed"))
                if is_connection_error_message(error):
                    raise HomeAssistantConnectionError(error)
                raise HomeAssistantCommandError(error)

            return {
                "success": True,
                "entity_id": entity_id,
                "uid": uid,
                "recurrence_id": recurrence_id,
                "recurrence_range": recurrence_range,
                "result": result,
                "message": f"Successfully deleted event '{uid}' from {entity_id}",
            }

        except ToolError:
            raise
        except Exception as error:
            logger.error(f"Failed to delete calendar event from {entity_id}: {error}")

            exception_to_structured_error(
                error,
                context={"entity_id": entity_id, "uid": uid},
                suggestions=self._build_remove_calendar_event_error_suggestions(
                    entity_id, uid, error
                ),
            )
            return None  # unreachable: exception_to_structured_error always raises

    def _build_remove_calendar_event_error_suggestions(
        self, entity_id: str, uid: str, error: Exception
    ) -> list[str]:
        """Build suggestions for a failed ha_config_remove_calendar_event call."""
        if isinstance(error, HomeAssistantConnectionError):
            # A transport drop is not a calendar problem — domain hints would
            # send the agent chasing a non-issue during an HA restart.
            return [
                "Home Assistant may be restarting or unreachable — retry shortly",
                "Check the connection to Home Assistant",
            ]
        suggestions = [
            f"Verify calendar entity '{entity_id}' exists",
            f"Verify event with UID '{uid}' exists in the calendar",
            "Use ha_config_get_calendar_events() to find the correct event UID",
            "Some calendar integrations may not support event deletion",
        ]
        error_str = str(error)
        if "404" in error_str or "not found" in error_str.lower():
            suggestions.insert(
                0, f"Calendar entity '{entity_id}' or event '{uid}' not found"
            )
        # HA words the WebSocket refusal "Calendar does not support event
        # deletion"; other layers say "not supported". Match both.
        lowered = error_str.lower()
        if "not supported" in lowered or "does not support" in lowered:
            suggestions.insert(0, "This calendar does not support event deletion")
        return suggestions


def register_calendar_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register calendar management tools with the MCP server."""
    register_tool_methods(mcp, CalendarTools(client))
