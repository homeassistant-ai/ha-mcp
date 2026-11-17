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

from ..client.rest_client import HomeAssistantAPIError
from ..errors import ErrorCode, create_error_response
from .auto_backup import with_auto_backup
from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    raise_core_refusal,
    raise_tool_error,
    register_tool_methods,
    validate_identifier_not_empty,
)
from .response_helpers import fetch_ha_timezone, resolve_local_timezone
from .tool_hints import read_only_hints, write_hints

logger = logging.getLogger(__name__)

# The only recurrence range Home Assistant recognises; it compares the string
# verbatim against ical's Range.THIS_AND_FUTURE.
_THIS_AND_FUTURE = "THISANDFUTURE"

_FIND_CALENDARS_HINT = (
    "Use ha_search(query='calendar', domain_filter='calendar') to find "
    "calendar entities"
)

# What to do about a calendar write Core refuses, by Core's error code.
_REFUSAL_HINTS = {
    "not_found": _FIND_CALENDARS_HINT,
    "not_supported": "This calendar's integration does not offer this write; "
    "use a calendar that does",
    "unauthorized": "The Home Assistant user behind this connection may not "
    "change this calendar",
}


def _calendar_event_backup_id(kw: dict[str, Any]) -> str:
    """Auto-backup key for a calendar write: ``<entity>::<uid>[::<recurrence_id>]``.

    A recurring series expands into occurrences that all share the ``uid`` and
    differ only by ``recurrence_id``, so the uid alone cannot identify which
    occurrence a write targets. Creates carry no uid and are skipped with a
    falsy key (the truthy ``"::"`` shape would hit the fetch with no record to
    find), as are the two write shapes no snapshot could undo.
    """
    entity_id = kw.get("entity_id")
    uid = kw.get("uid")
    if not entity_id or not uid:
        return ""
    if kw.get("recurrence_range"):
        # A ranged write splits the series: Home Assistant truncates the
        # original at this occurrence and starts a second one under the same
        # uid. Restoring the captured occurrence would land on the NEW series
        # and fork that single date out of it, leaving every later date
        # edited while reporting success.
        # Debug, not warning: the decorator runs this key builder twice per
        # call, and once even when auto-backup is switched off.
        logger.debug(
            "Auto-backup: no snapshot for the ranged write on %s (event %s); "
            "a recurrence range rewrites the series, which a per-occurrence "
            "snapshot cannot undo",
            entity_id,
            uid,
        )
        return ""
    if kw.get("rrule"):
        # Turning an event into a series cannot be undone through the update
        # command: Home Assistant validates ``rrule`` with a rule parser that
        # rejects both null and empty, so the restore has no way to say
        # "no recurrence" and the stored rule survives the merge.
        logger.debug(
            "Auto-backup: no snapshot for the recurrence-rule write on %s "
            "(event %s); Home Assistant offers no way to clear a rule again",
            entity_id,
            uid,
        )
        return ""
    recurrence_id = kw.get("recurrence_id")
    if recurrence_id:
        return f"{entity_id}::{uid}::{recurrence_id}"
    return f"{entity_id}::{uid}"


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
    if recurrence_range is not None and recurrence_range != _THIS_AND_FUTURE:
        # The ``Literal`` annotation only binds MCP calls, which the tool's
        # TypeAdapter validates; a direct Python call reaches here unchecked
        # and Home Assistant compares the value verbatim, so a near-miss
        # spelling silently degrades to a single-occurrence write.
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"recurrence_range must be {_THIS_AND_FUTURE!r}: Home Assistant "
                "compares the value verbatim",
                context={
                    "entity_id": entity_id,
                    "recurrence_range": recurrence_range,
                },
                suggestions=[
                    f"Pass {_THIS_AND_FUTURE!r} to target this and later occurrences",
                    "Omit recurrence_range to target a single occurrence",
                ],
            )
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


def _set_fields(**fields: Any) -> dict[str, Any]:
    """The given fields that are not None."""
    return {key: value for key, value in fields.items() if value is not None}


class CalendarTools:
    """Calendar event management tools for Home Assistant."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @tool(
        name="ha_config_get_calendar_events",
        tags={"Calendar"},
        annotations=read_only_hints("Get Calendar Events", open_world=False),
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
                description="Start datetime in ISO format; omit it to start at the current time",
                default=None,
            ),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description="End datetime in ISO format (default: 7 days from now, "
                "not from start; pass end whenever you pass start)",
                default=None,
            ),
        ] = None,
        max_results: Annotated[
            int,
            Field(description="Maximum number of events to return", default=20),
        ] = 20,
    ) -> dict[str, Any]:
        """Get calendar events from a calendar entity within a time range.

        Returns each event's summary, start, end, description and location,
        plus the uid, recurrence_id and rrule when the calendar provides them
        (the edit and remove tools use uid and recurrence_id). To find calendar
        entities, use ha_search(domain_filter='calendar').

        EXAMPLES:
        - Next week (defaults): ha_config_get_calendar_events("calendar.family")
        - Date range: ha_config_get_calendar_events("calendar.work", start="2024-01-01T00:00:00", end="2024-01-31T23:59:59")
        """
        try:
            if start is None or end is None:
                # Core reads a time without an offset in Home Assistant's time
                # zone, which need not be this server's. A default without an
                # offset pairs with a bound given the same way; Core cannot
                # compare one with an offset against one without.
                ha_timezone, _ = await fetch_ha_timezone(self._client)
                local_tz, _ = resolve_local_timezone(ha_timezone)
                now = datetime.now(local_tz).replace(tzinfo=None)
                if start is None:
                    start = now.isoformat()
                if end is None:
                    end = (now + timedelta(days=7)).isoformat()

            # The client adds the /api prefix to Core's calendar view.
            response = await self._client._request(
                "GET", f"/calendars/{entity_id}", params={"start": start, "end": end}
            )
            events = response if isinstance(response, list) else []
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
        except Exception as error:  # noqa: BLE001
            logger.error(f"Failed to get calendar events for {entity_id}: {error}")

            suggestions = [_FIND_CALENDARS_HINT]
            if isinstance(error, HomeAssistantAPIError) and error.status_code == 400:
                suggestions.insert(
                    0,
                    f"Home Assistant answers 400 when '{entity_id}' is not a "
                    "calendar entity it has loaded, when start or end is not an "
                    "ISO 8601 datetime, or when start is after end",
                )

            exception_to_structured_error(
                error, context={"entity_id": entity_id}, suggestions=suggestions
            )
            return None  # unreachable: exception_to_structured_error always raises

    async def _send_event_command(
        self,
        action: Literal["create", "update", "delete"],
        entity_id: str,
        uid: str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Send ``calendar/event/<action>`` for ``entity_id`` and event ``uid``.

        Create, update and delete are WebSocket commands; Home Assistant
        registers no REST service for update or delete, and its
        ``create_event`` service schema has no ``rrule``. A refusal is raised
        with Core's own text and error code: ``not_found`` for a calendar
        entity Core does not have, ``not_supported`` for a write the calendar
        does not offer, ``unauthorized`` for a user who may not change the
        calendar, ``invalid_format`` for a command Core's schema refuses
        (a malformed entity_id or event), and ``failed`` with the integration's
        text when the write itself fails. Unset ``fields`` stay out of the
        command.
        """
        target = {"entity_id": entity_id, **_set_fields(uid=uid)}
        result: dict[str, Any] = await self._client.send_websocket_message(
            {"type": f"calendar/event/{action}", **target, **_set_fields(**fields)}
        )
        if not result.get("success"):
            code = result.get("error_code")
            hint = _REFUSAL_HINTS.get(code or "")
            if code == "failed" and uid is not None:
                # Local Calendar answers a uid it does not hold with a generic
                # "An error occurred while updating/deleting the event".
                hint = (
                    "Use ha_config_get_calendar_events() to check the event's "
                    "uid and recurrence_id"
                )
            raise_core_refusal(
                f"Failed to {action} the calendar event",
                result,
                target,
                [hint] if hint else None,
            )
        return result

    @tool(
        name="ha_config_set_calendar_event",
        tags={"Calendar"},
        annotations=write_hints(
            "Create or Update Calendar Event",
            destructive=True,
            idempotent=False,
            open_world=False,
        ),
    )
    @with_auto_backup(
        domain="calendar_event",
        id_fn=_calendar_event_backup_id,
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
            Field(description="Event description", default=None),
        ] = None,
        location: Annotated[
            str | None, Field(description="Event location", default=None)
        ] = None,
        rrule: Annotated[
            str | None,
            Field(
                description=(
                    "RFC 5545 recurrence rule, without 'RRULE:' prefix (e.g., "
                    "'FREQ=WEEKLY;BYDAY=MO' or 'FREQ=MONTHLY;BYDAY=3SA'). Creates a "
                    "recurring event series."
                ),
                default=None,
            ),
        ] = None,
        uid: Annotated[
            str | None,
            Field(
                description="UID of an existing event to update.",
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
        """Create a new event in a calendar, or update an existing one.

        Passing ``uid`` switches to update mode.

        **When NOT to use:**
        - To retrieve calendar events, use ``ha_config_get_calendar_events``.
        - To delete an event, use ``ha_config_remove_calendar_event``.
        - To find the ``uid`` of an event to update, use
          ``ha_config_get_calendar_events``; this tool does not search.

        Passing date-only values (``YYYY-MM-DD``) for both ``start`` and
        ``end`` creates an all-day event; passing full ISO datetimes creates
        a timed event. Home Assistant refuses a mix of the two.

        An update replaces the whole event rather than patching it, so
        ``summary``, ``start`` and ``end`` stay required in update mode, and a
        ``description`` or ``location`` that is not re-supplied is cleared.
        An ``rrule`` is the exception: Home Assistant accepts a new rule but
        has no way to express "no recurrence", so an existing rule survives an
        update that omits it. Delete the event and create it again to drop the
        recurrence.

        Which writes a calendar accepts depends on its integration; Home
        Assistant refuses a create or update the calendar does not support,
        and its error says so.

        EXAMPLES:
        - Create: ha_config_set_calendar_event("calendar.family", summary="Doctor appointment", start="2024-01-15T14:00:00", end="2024-01-15T15:00:00")
        - Recurring (every Monday, 10 occurrences): ha_config_set_calendar_event("calendar.work", summary="Team meeting", start="2024-01-15T10:00:00", end="2024-01-15T11:00:00", rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10")
        - Update this and all later occurrences: ha_config_set_calendar_event("calendar.work", summary="Team meeting (new time)", start="2024-02-05T11:00:00", end="2024-02-05T12:00:00", uid="recurring-event-67890", recurrence_id="20240205T100000", recurrence_range="THISANDFUTURE")
        """
        try:
            if uid is not None:
                # An empty/whitespace uid would reach the WS command, and HA
                # answers it with no more than a failed update.
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

            event_details: dict[str, Any] = {
                "summary": summary,
                "start": start,
                "end": end,
                "description": description,
                "location": location,
                "rrule": rrule,
            }

            if uid is not None:
                # HA merges rather than replaces, so an omitted key keeps its
                # old value; the empty strings are what make this a replacement.
                # An rrule has no empty counterpart: HA's rule parser rejects
                # null and empty, so an unset one stays out.
                event = {
                    "summary": summary,
                    "dtstart": start,
                    "dtend": end,
                    "description": description or "",
                    "location": location or "",
                    **_set_fields(rrule=rrule),
                }
                result = await self._send_event_command(
                    "update",
                    entity_id,
                    uid,
                    event=event,
                    recurrence_id=recurrence_id,
                    recurrence_range=recurrence_range,
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

            # Core reads a date or a datetime from dtstart/dtend itself.
            event = {
                "summary": summary,
                "dtstart": start,
                "dtend": end,
                **_set_fields(description=description, location=location, rrule=rrule),
            }
            result = await self._send_event_command("create", entity_id, event=event)

            return {
                "success": True,
                "entity_id": entity_id,
                "event": event_details,
                "result": result,
                "message": f"Successfully created event '{summary}' in {entity_id}",
            }

        except ToolError:
            raise
        except Exception as error:  # noqa: BLE001
            action = "update" if uid is not None else "create"
            logger.error(f"Failed to {action} calendar event in {entity_id}: {error}")
            exception_to_structured_error(
                error, context={"entity_id": entity_id, **_set_fields(uid=uid)}
            )
            return None  # unreachable: exception_to_structured_error always raises

    @tool(
        name="ha_config_remove_calendar_event",
        tags={"Calendar"},
        annotations=write_hints(
            "Remove Calendar Event", destructive=True, idempotent=True, open_world=False
        ),
    )
    @with_auto_backup(
        domain="calendar_event",
        id_fn=_calendar_event_backup_id,
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
            Field(description="Recurrence ID for recurring events", default=None),
        ] = None,
        recurrence_range: Annotated[
            Literal["THISANDFUTURE"] | None,
            Field(
                description=(
                    "Recurrence range: 'THISANDFUTURE' to delete this and future "
                    "occurrences. Home Assistant compares this value verbatim, so no other "
                    "spelling (including 'THIS_AND_FUTURE') selects the range."
                ),
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Delete an event from a calendar.

        Get the event UID from ha_config_get_calendar_events().

        EXAMPLES:
        - Delete a single event: ha_config_remove_calendar_event("calendar.family", uid="event-12345")
        - Delete one occurrence and all later ones: ha_config_remove_calendar_event("calendar.work", uid="recurring-event-67890", recurrence_id="20240115T100000", recurrence_range="THISANDFUTURE")
        """
        try:
            # An empty/whitespace uid would reach the WS command, and HA
            # answers it with no more than a failed delete.
            validate_identifier_not_empty(
                uid,
                "uid",
                suggestions=[
                    "Use ha_config_get_calendar_events() to list events and obtain valid UIDs",
                ],
                context={"entity_id": entity_id},
            )

            _validate_recurrence_target(entity_id, recurrence_id, recurrence_range)

            result = await self._send_event_command(
                "delete",
                entity_id,
                uid,
                recurrence_id=recurrence_id,
                recurrence_range=recurrence_range,
            )

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
        except Exception as error:  # noqa: BLE001
            logger.error(f"Failed to delete calendar event from {entity_id}: {error}")
            exception_to_structured_error(
                error, context={"entity_id": entity_id, "uid": uid}
            )
            return None  # unreachable: exception_to_structured_error always raises


def register_calendar_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register calendar management tools with the MCP server."""
    register_tool_methods(mcp, CalendarTools(client))
