"""
Microsoft 365 integration tools for Home Assistant MCP server.

Provides tools for interacting with Microsoft 365 services (Calendar, To Do)
via the Microsoft Graph API. Requires MS365 credentials configured via
environment variables or the add-on configuration.

Supports multi-user setups (e.g. family members with separate calendars).

Environment variables required:
    MS365_CLIENT_ID          - Azure app client ID
    MS365_CLIENT_SECRET      - Azure app client secret
    MS365_TENANT_ID          - Azure tenant ID (use 'common' for personal accounts)
    MS365_REFRESH_TOKEN      - OAuth2 refresh token for the primary user

Optional per-user overrides (NAME uppercased):
    MS365_REFRESH_TOKEN_<NAME>  - Per-user refresh token  (e.g. MS365_REFRESH_TOKEN_SONIA)
    MS365_CALENDAR_ID_<NAME>    - Shared/secondary calendar ID for a user
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any

import httpx
from fastmcp.exceptions import ToolError
from fastmcp.tools import tool
from pydantic import Field

from .helpers import (
    exception_to_structured_error,
    log_tool_usage,
    register_tool_methods,
)

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
SCOPES = "Calendars.ReadWrite Tasks.ReadWrite offline_access"

# ---------------------------------------------------------------------------
# Token management
# ---------------------------------------------------------------------------

_token_cache: dict[str, tuple[str, datetime]] = {}


async def _get_access_token(user: str | None = None) -> str:
    """Obtain a valid access token for the given user (or default user)."""
    cache_key = user or "default"

    # Return cached token if still valid (with 60s buffer)
    if cache_key in _token_cache:
        token, expires_at = _token_cache[cache_key]
        if datetime.now(timezone.utc) < expires_at - timedelta(seconds=60):
            return token

    client_id = os.environ.get("MS365_CLIENT_ID", "")
    client_secret = os.environ.get("MS365_CLIENT_SECRET", "")
    tenant = os.environ.get("MS365_TENANT_ID", "common")

    # Resolve refresh token: per-user first, then default
    refresh_token = ""
    if user:
        refresh_token = os.environ.get(f"MS365_REFRESH_TOKEN_{user.upper()}", "")
    if not refresh_token:
        refresh_token = os.environ.get("MS365_REFRESH_TOKEN", "")

    if not all([client_id, client_secret, refresh_token]):
        raise ToolError(
            "Microsoft 365 credentials not configured. "
            "Set MS365_CLIENT_ID, MS365_CLIENT_SECRET and MS365_REFRESH_TOKEN "
            "in the add-on configuration. See docs/ms365-integration.md."
        )

    url = TOKEN_URL.format(tenant=tenant)
    data = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": SCOPES,
    }

    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(url, data=data)

    if resp.status_code != 200:
        raise ToolError(
            f"Failed to obtain MS365 access token: {resp.status_code} {resp.text}"
        )

    token_data = resp.json()
    access_token: str = token_data["access_token"]
    expires_in: int = token_data.get("expires_in", 3600)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    _token_cache[cache_key] = (access_token, expires_at)

    # Persist updated refresh token if provided
    new_refresh = token_data.get("refresh_token")
    if new_refresh:
        env_key = (
            f"MS365_REFRESH_TOKEN_{user.upper()}" if user else "MS365_REFRESH_TOKEN"
        )
        os.environ[env_key] = new_refresh

    return access_token


async def _graph_request(
    method: str,
    path: str,
    user: str | None = None,
    **kwargs: Any,
) -> Any:
    """Make an authenticated request to the Microsoft Graph API."""
    token = await _get_access_token(user)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    url = f"{GRAPH_BASE}{path}"

    async with httpx.AsyncClient(timeout=20) as http:
        resp = await http.request(method, url, headers=headers, **kwargs)

    if resp.status_code == 204:
        return None
    if resp.status_code >= 400:
        raise ToolError(f"Graph API error {resp.status_code}: {resp.text}")

    return resp.json() if resp.content else None


def _resolve_calendar_base(user: str | None) -> str:
    """Return the Graph API base path for the user's calendar."""
    if user:
        cal_id = os.environ.get(f"MS365_CALENDAR_ID_{user.upper()}", "")
        if cal_id:
            return f"me/calendars/{cal_id}"
    return "me"


# ---------------------------------------------------------------------------
# Calendar tools
# ---------------------------------------------------------------------------


class MS365CalendarTools:
    """Microsoft 365 Calendar tools via the Graph API."""

    @tool(
        name="ms365_get_calendar_events",
        tags={"MS365", "Calendar"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get MS365 Calendar Events",
        },
    )
    @log_tool_usage
    async def ms365_get_calendar_events(
        self,
        start: Annotated[
            str | None,
            Field(description="Start datetime ISO format (default: now)", default=None),
        ] = None,
        end: Annotated[
            str | None,
            Field(
                description="End datetime ISO format (default: 7 days from start)",
                default=None,
            ),
        ] = None,
        user: Annotated[
            str | None,
            Field(
                description=(
                    "User name for multi-user setups (e.g. 'sonia'). "
                    "Omit for primary user."
                ),
                default=None,
            ),
        ] = None,
        max_results: Annotated[
            int,
            Field(description="Maximum events to return (default: 20)", default=20),
        ] = 20,
    ) -> dict[str, Any]:
        """
        Retrieve calendar events from Microsoft 365 / Outlook.

        Fetches events within a date range from the primary calendar or a
        configured shared calendar. Supports multi-user family setups.

        **Example Usage:**
        ```python
        # This week for primary user
        ms365_get_calendar_events()

        # Next week for a family member
        ms365_get_calendar_events(user="sonia", start="2026-05-05T00:00:00")
        ```

        **Returns:** List of events with subject, start, end, location, isAllDay
        """
        try:
            now = datetime.now(timezone.utc)
            start_dt = start or now.isoformat()
            end_dt = end or (now + timedelta(days=7)).isoformat()

            cal_base = _resolve_calendar_base(user)
            params = {
                "$top": max_results,
                "startDateTime": start_dt,
                "endDateTime": end_dt,
                "$select": "id,subject,start,end,location,organizer,isAllDay,bodyPreview",
                "$orderby": "start/dateTime asc",
            }

            data = await _graph_request(
                "GET", f"/{cal_base}/calendarView", user=user, params=params
            )
            events = data.get("value", []) if data else []

            return {
                "success": True,
                "user": user or "primary",
                "events": events,
                "count": len(events),
                "time_range": {"start": start_dt, "end": end_dt},
                "message": f"Retrieved {len(events)} event(s)",
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(error, context={"user": user})

    @tool(
        name="ms365_create_calendar_event",
        tags={"MS365", "Calendar"},
        annotations={
            "destructiveHint": True,
            "title": "Create MS365 Calendar Event",
        },
    )
    @log_tool_usage
    async def ms365_create_calendar_event(
        self,
        subject: Annotated[str, Field(description="Event title")],
        start: Annotated[str, Field(description="Start datetime in ISO format")],
        end: Annotated[str, Field(description="End datetime in ISO format")],
        location: Annotated[
            str | None, Field(description="Location", default=None)
        ] = None,
        description: Annotated[
            str | None, Field(description="Event body / notes", default=None)
        ] = None,
        attendees: Annotated[
            list[str] | None,
            Field(description="List of attendee email addresses", default=None),
        ] = None,
        is_all_day: Annotated[
            bool, Field(description="All-day event", default=False)
        ] = False,
        user: Annotated[
            str | None,
            Field(
                description="User name for multi-user setups. Omit for primary user.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Create a new calendar event in Microsoft 365 / Outlook.

        **Example Usage:**
        ```python
        ms365_create_calendar_event(
            subject="Dentist",
            start="2026-05-10T10:00:00",
            end="2026-05-10T11:00:00",
            location="Tandlæge Hansen, Helsingør"
        )

        # Add to a family member's calendar
        ms365_create_calendar_event(
            subject="Football practice",
            start="2026-05-12T16:00:00",
            end="2026-05-12T17:30:00",
            user="sonia"
        )
        ```
        """
        try:
            body: dict[str, Any] = {
                "subject": subject,
                "start": {"dateTime": start, "timeZone": "Europe/Copenhagen"},
                "end": {"dateTime": end, "timeZone": "Europe/Copenhagen"},
                "isAllDay": is_all_day,
            }
            if location:
                body["location"] = {"displayName": location}
            if description:
                body["body"] = {"contentType": "text", "content": description}
            if attendees:
                body["attendees"] = [
                    {"emailAddress": {"address": e}, "type": "required"}
                    for e in attendees
                ]

            cal_base = _resolve_calendar_base(user)
            data = await _graph_request(
                "POST", f"/{cal_base}/events", user=user, json=body
            )

            return {
                "success": True,
                "user": user or "primary",
                "event_id": data.get("id") if data else None,
                "subject": subject,
                "start": start,
                "end": end,
                "message": f"Created event '{subject}'",
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(
                error, context={"subject": subject, "user": user}
            )

    @tool(
        name="ms365_update_calendar_event",
        tags={"MS365", "Calendar"},
        annotations={
            "destructiveHint": True,
            "title": "Update MS365 Calendar Event",
        },
    )
    @log_tool_usage
    async def ms365_update_calendar_event(
        self,
        event_id: Annotated[
            str,
            Field(description="Event ID from ms365_get_calendar_events"),
        ],
        subject: Annotated[
            str | None, Field(description="New title", default=None)
        ] = None,
        start: Annotated[
            str | None, Field(description="New start datetime ISO", default=None)
        ] = None,
        end: Annotated[
            str | None, Field(description="New end datetime ISO", default=None)
        ] = None,
        location: Annotated[
            str | None, Field(description="New location", default=None)
        ] = None,
        description: Annotated[
            str | None, Field(description="New body / notes", default=None)
        ] = None,
        user: Annotated[
            str | None,
            Field(description="User name for multi-user setups", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """
        Update an existing Microsoft 365 calendar event.

        Only the provided fields are updated; omitted fields remain unchanged.

        **Example Usage:**
        ```python
        ms365_update_calendar_event(
            event_id="AAMkADAwATM...",
            start="2026-05-10T11:00:00",
            end="2026-05-10T12:00:00"
        )
        ```
        """
        try:
            patch: dict[str, Any] = {}
            if subject:
                patch["subject"] = subject
            if start:
                patch["start"] = {
                    "dateTime": start,
                    "timeZone": "Europe/Copenhagen",
                }
            if end:
                patch["end"] = {
                    "dateTime": end,
                    "timeZone": "Europe/Copenhagen",
                }
            if location:
                patch["location"] = {"displayName": location}
            if description:
                patch["body"] = {"contentType": "text", "content": description}

            if not patch:
                return {
                    "success": False,
                    "message": "No fields provided to update",
                }

            await _graph_request(
                "PATCH", f"/me/events/{event_id}", user=user, json=patch
            )

            return {
                "success": True,
                "event_id": event_id,
                "updated_fields": list(patch.keys()),
                "message": f"Updated event {event_id}",
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(error, context={"event_id": event_id})

    @tool(
        name="ms365_delete_calendar_event",
        tags={"MS365", "Calendar"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "title": "Delete MS365 Calendar Event",
        },
    )
    @log_tool_usage
    async def ms365_delete_calendar_event(
        self,
        event_id: Annotated[
            str,
            Field(description="Event ID from ms365_get_calendar_events"),
        ],
        user: Annotated[
            str | None,
            Field(description="User name for multi-user setups", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """
        Delete a Microsoft 365 calendar event.

        **Example Usage:**
        ```python
        ms365_delete_calendar_event(event_id="AAMkADAwATM...")
        ```
        """
        try:
            await _graph_request("DELETE", f"/me/events/{event_id}", user=user)
            return {
                "success": True,
                "event_id": event_id,
                "message": f"Deleted event {event_id}",
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(error, context={"event_id": event_id})


# ---------------------------------------------------------------------------
# To Do tools
# ---------------------------------------------------------------------------


class MS365TodoTools:
    """Microsoft 365 To Do tools via the Graph API."""

    async def _resolve_list(
        self, list_name: str | None, user: str | None
    ) -> tuple[str, str]:
        """Return (list_id, display_name) for the given list name or default list."""
        data = await _graph_request("GET", "/me/todo/lists", user=user)
        lists = data.get("value", []) if data else []

        if list_name:
            match = next(
                (
                    lst
                    for lst in lists
                    if lst["displayName"].lower() == list_name.lower()
                ),
                None,
            )
            if not match:
                available = [lst["displayName"] for lst in lists]
                raise ToolError(
                    f"Task list '{list_name}' not found. "
                    f"Available lists: {available}"
                )
            return match["id"], match["displayName"]

        # Default list
        default = next(
            (
                lst
                for lst in lists
                if lst.get("wellknownListName") == "defaultList"
            ),
            lists[0] if lists else None,
        )
        if not default:
            raise ToolError("No task lists found in Microsoft To Do")
        return default["id"], default["displayName"]

    @tool(
        name="ms365_get_todo_tasks",
        tags={"MS365", "Todo"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get MS365 To Do Tasks",
        },
    )
    @log_tool_usage
    async def ms365_get_todo_tasks(
        self,
        list_name: Annotated[
            str | None,
            Field(
                description="Task list name (default: primary list)", default=None
            ),
        ] = None,
        status: Annotated[
            str | None,
            Field(
                description=(
                    "Filter: 'notStarted', 'inProgress', 'completed'. "
                    "Default: all open tasks."
                ),
                default=None,
            ),
        ] = None,
        user: Annotated[
            str | None,
            Field(description="User name for multi-user setups", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """
        Retrieve tasks from Microsoft To Do.

        **Example Usage:**
        ```python
        # All open tasks
        ms365_get_todo_tasks()

        # Completed tasks from a specific list
        ms365_get_todo_tasks(list_name="Shopping", status="completed")
        ```
        """
        try:
            list_id, list_display = await self._resolve_list(list_name, user)

            params: dict[str, Any] = {
                "$select": (
                    "id,title,status,importance,"
                    "dueDateTime,completedDateTime,body"
                ),
                "$top": 50,
            }
            if status:
                params["$filter"] = f"status eq '{status}'"
            else:
                params["$filter"] = "status ne 'completed'"

            data = await _graph_request(
                "GET",
                f"/me/todo/lists/{list_id}/tasks",
                user=user,
                params=params,
            )
            tasks = data.get("value", []) if data else []

            return {
                "success": True,
                "list_name": list_display,
                "list_id": list_id,
                "tasks": tasks,
                "count": len(tasks),
                "message": (
                    f"Retrieved {len(tasks)} task(s) from '{list_display}'"
                ),
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(error, context={"list_name": list_name})

    @tool(
        name="ms365_add_todo_task",
        tags={"MS365", "Todo"},
        annotations={"destructiveHint": True, "title": "Add MS365 To Do Task"},
    )
    @log_tool_usage
    async def ms365_add_todo_task(
        self,
        title: Annotated[str, Field(description="Task title")],
        list_name: Annotated[
            str | None,
            Field(
                description="Task list name (default: primary list)", default=None
            ),
        ] = None,
        due_date: Annotated[
            str | None,
            Field(
                description="Due date ISO format (e.g. '2026-05-10T12:00:00')",
                default=None,
            ),
        ] = None,
        importance: Annotated[
            str,
            Field(
                description="Importance: 'low', 'normal', 'high'", default="normal"
            ),
        ] = "normal",
        notes: Annotated[
            str | None, Field(description="Task notes / body", default=None)
        ] = None,
        user: Annotated[
            str | None,
            Field(description="User name for multi-user setups", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """
        Add a new task to Microsoft To Do.

        **Example Usage:**
        ```python
        ms365_add_todo_task(title="Buy groceries", list_name="Shopping")
        ms365_add_todo_task(
            title="Call dentist",
            due_date="2026-05-15T09:00:00",
            importance="high"
        )
        ```
        """
        try:
            list_id, list_display = await self._resolve_list(list_name, user)

            body: dict[str, Any] = {
                "title": title,
                "importance": importance,
            }
            if due_date:
                body["dueDateTime"] = {
                    "dateTime": due_date,
                    "timeZone": "Europe/Copenhagen",
                }
            if notes:
                body["body"] = {"contentType": "text", "content": notes}

            data = await _graph_request(
                "POST",
                f"/me/todo/lists/{list_id}/tasks",
                user=user,
                json=body,
            )

            return {
                "success": True,
                "task_id": data.get("id") if data else None,
                "title": title,
                "list_name": list_display,
                "message": f"Added task '{title}' to '{list_display}'",
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(error, context={"title": title})

    @tool(
        name="ms365_complete_todo_task",
        tags={"MS365", "Todo"},
        annotations={
            "destructiveHint": True,
            "title": "Complete MS365 To Do Task",
        },
    )
    @log_tool_usage
    async def ms365_complete_todo_task(
        self,
        task_id: Annotated[
            str, Field(description="Task ID from ms365_get_todo_tasks")
        ],
        list_id: Annotated[
            str, Field(description="List ID from ms365_get_todo_tasks")
        ],
        user: Annotated[
            str | None,
            Field(description="User name for multi-user setups", default=None),
        ] = None,
    ) -> dict[str, Any]:
        """Mark a Microsoft To Do task as completed."""
        try:
            patch = {
                "status": "completed",
                "completedDateTime": {
                    "dateTime": datetime.now(timezone.utc).isoformat(),
                    "timeZone": "UTC",
                },
            }
            await _graph_request(
                "PATCH",
                f"/me/todo/lists/{list_id}/tasks/{task_id}",
                user=user,
                json=patch,
            )
            return {
                "success": True,
                "task_id": task_id,
                "message": "Task marked as completed",
            }
        except ToolError:
            raise
        except Exception as error:
            exception_to_structured_error(error, context={"task_id": task_id})


# ---------------------------------------------------------------------------
# Registration — auto-discovered via tools_* naming convention
# ---------------------------------------------------------------------------


def register_ms365_tools(mcp: Any, **kwargs: Any) -> None:
    """Register all Microsoft 365 tools with the MCP server."""
    register_tool_methods(mcp, MS365CalendarTools())
    register_tool_methods(mcp, MS365TodoTools())
    logger.info("Registered MS365 tools (calendar + todo)")
