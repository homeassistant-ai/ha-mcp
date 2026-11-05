"""Auto-backup handlers for calendar events.

A snapshot holds one event (or one occurrence) read from the calendar view; a
restore puts it back in place by uid, or re-creates a deleted event.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from .backup_entity_ids import _manager
from .client.rest_client import HomeAssistantCommandError, HomeAssistantError

logger = logging.getLogger(__name__)


# Wide enough for past and far-booked events without expanding years of recurrences.
_CALENDAR_WIDE_LOOKBACK_DAYS = 366
_CALENDAR_WIDE_LOOKAHEAD_DAYS = 732


async def _find_calendar_event(
    client: Any,
    cal: str,
    uid: str,
    recurrence_id: str | None,
    start: datetime,
    end: datetime,
) -> Any:
    """The ``uid`` event (occurrence ``recurrence_id``) on ``cal`` in the window.

    The REST calendar view, not ``calendar.get_events``: the service drops uid.
    """
    try:
        events = await client._request(
            "GET",
            f"/calendars/{cal}",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
    except HomeAssistantError as err:
        # 404: the calendar integration is not loaded.
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    for event in _manager()._require_list(events, f"/calendars/{cal}"):
        if not isinstance(event, dict) or event.get("uid") != uid:
            continue
        if recurrence_id is not None and event.get("recurrence_id") != recurrence_id:
            continue
        return event
    return None


def _day_window(value: str | None) -> tuple[datetime, datetime] | None:
    """A day either side of a recurrence id or ISO date(-time), if it parses."""
    try:
        at = datetime.fromisoformat(value or "")
    except ValueError:
        return None
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    return at - timedelta(days=1), at + timedelta(days=1)


async def _locate_calendar_event(
    client: Any, cal: str, uid: str, wanted: str | None, near: str | None = None
) -> Any:
    """The ``uid`` event (occurrence ``wanted``) on ``cal``; None if no window has it.

    Out-of-window events and cancelled ones Local Calendar hides also read as None.
    """
    try:
        from .config import get_global_settings

        days = int(
            getattr(get_global_settings(), "auto_backup_calendar_lookahead_days", 7)
        )
    except (AttributeError, ImportError, ValueError, TypeError):
        days = 7
    days = max(1, min(365, days))
    now = datetime.now(UTC)
    # A recurrence_id names the occurrence's date; without one, ``near`` (the
    # snapshot's start) places the request that finds an event the windows
    # below would miss.
    around = _day_window(wanted) or _day_window(near)
    found = (
        await _find_calendar_event(client, cal, uid, wanted, *around)
        if around
        else None
    )
    if found is None:
        found = await _find_calendar_event(
            client, cal, uid, wanted, now, now + timedelta(days=days)
        )
    if found is None:
        # A missed event would skip the snapshot or be re-created on restore.
        found = await _find_calendar_event(
            client,
            cal,
            uid,
            wanted,
            now - timedelta(days=_CALENDAR_WIDE_LOOKBACK_DAYS),
            now + timedelta(days=_CALENDAR_WIDE_LOOKAHEAD_DAYS),
        )
    return found


async def _fetch_calendar_event(client: Any, entity_id: str) -> Any:
    # "<calendar.entity>::<event_uid>[::<recurrence_id>]"
    cal, _, rest = entity_id.partition("::")
    uid, _, recurrence_id = rest.partition("::")
    if not cal or not uid:
        return None
    wanted = recurrence_id or None
    found = await _locate_calendar_event(client, cal, uid, wanted)
    if found is None:
        return None
    if wanted is None and found.get("rrule"):
        # The view shows only expanded occurrences, never the series itself,
        # so a series-wide write cannot be restored faithfully.
        logger.warning(
            "Auto-backup: no snapshot for series-wide write on %s (event %s); "
            "Home Assistant exposes only expanded occurrences, so the series "
            "could not be restored faithfully",
            cal,
            uid,
        )
        return None
    return {"calendar_entity_id": cal, **found}


def _calendar_bound(value: Any) -> tuple[str, bool] | None:
    """``(iso_value, is_date_only)``; older snapshots hold a flat ISO string."""
    if isinstance(value, dict):
        if value.get("dateTime"):
            return str(value["dateTime"]), False
        if value.get("date"):
            return str(value["date"]), True
        return None
    if isinstance(value, str) and value:
        return value, len(value) == 10
    return None


def _same_bound(found: tuple[str, bool] | None, wanted: tuple[str, bool]) -> bool:
    """Equal bounds: timed ones by instant, or by wall clock, which Local
    Calendar's floating times keep across a timezone change."""
    if found is None or found[1] != wanted[1]:
        return False
    if found[1] or found[0] == wanted[0]:
        return found[0] == wanted[0]
    at, want = datetime.fromisoformat(found[0]), datetime.fromisoformat(wanted[0])
    if at.replace(tzinfo=None) == want.replace(tzinfo=None):
        return True
    return at.tzinfo is not None and want.tzinfo is not None and at == want


async def _restored_copy(
    client: Any,
    cal: str,
    event: dict[str, Any],
    start: tuple[str, bool],
    end: tuple[str, bool],
) -> dict[str, Any] | None:
    """The copy an earlier restore re-created: same summary, times and text.

    A re-created event gets a new uid, so its snapshot's uid stays gone. A day
    either side covers a timezone change since the copy was made.
    """
    first, last = (
        datetime.fromisoformat(f"{value}T00:00:00" if date_only else value)
        for value, date_only in (start, end)
    )
    try:
        events = await client._request(
            "GET",
            f"/calendars/{cal}",
            params={
                "start": (first - timedelta(days=1)).isoformat(),
                "end": (last + timedelta(days=1)).isoformat(),
            },
        )
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    for found in _manager()._require_list(events, f"/calendars/{cal}"):
        if (
            isinstance(found, dict)
            and _same_bound(_calendar_bound(found.get("start")), start)
            and _same_bound(_calendar_bound(found.get("end")), end)
            and all(
                (found.get(key) or "") == event[key]
                for key in ("summary", "description", "location")
            )
        ):
            return found
    return None


async def _recreate_calendar_event(
    client: Any,
    cal: str,
    event: dict[str, Any],
    start: tuple[str, bool],
    end: tuple[str, bool],
) -> Any:
    """Re-create a deleted event, always as a single event."""
    if (copy := await _restored_copy(client, cal, event, start, end)) is not None:
        return {"already_restored": True, "event": copy}
    data: dict[str, Any] = {"entity_id": cal, "summary": event["summary"]}
    if start[1]:
        data.update({"start_date": start[0], "end_date": end[0]})
    else:
        data.update({"start_date_time": start[0], "end_date_time": end[0]})
    for key in ("description", "location"):
        if event.get(key):
            data[key] = event[key]
    return await _manager()._rest_post(client, "services/calendar/create_event", data)


async def _restore_calendar_event(client: Any, entity_id: str, config: Any) -> Any:
    cal, _, rest = entity_id.partition("::")
    key_uid, _, key_recurrence_id = rest.partition("::")
    cal = config.get("calendar_entity_id") or cal
    uid = config.get("uid") or key_uid
    recurrence_id = config.get("recurrence_id") or key_recurrence_id or None
    start = _calendar_bound(config.get("start"))
    end = _calendar_bound(config.get("end"))
    if start is None or end is None:
        raise HomeAssistantError(
            f"Calendar snapshot for {entity_id!r} has no usable start/end"
        )

    # Core merges an update into the stored event: empty text clears a field.
    event: dict[str, Any] = {
        "summary": config.get("summary") or "",
        "dtstart": start[0],
        "dtend": end[0],
        "description": config.get("description") or "",
        "location": config.get("location") or "",
    }
    # No rrule: on one occurrence it would start a second series under the uid.

    if not uid:
        return await _recreate_calendar_event(client, cal, event, start, end)

    message: dict[str, Any] = {
        "type": "calendar/event/update",
        "entity_id": cal,
        "uid": uid,
        "event": event,
    }
    if recurrence_id:
        message["recurrence_id"] = recurrence_id
    try:
        return await _manager()._ws_send(client, message)
    except HomeAssistantCommandError as err:
        unsupported = err.code == "not_supported"
        # Local Calendar reports a missing event as a generic ``failed``, so
        # only that code is checked against the calendar before re-creating.
        if not unsupported and (
            err.code != "failed"
            or await _locate_calendar_event(client, cal, uid, recurrence_id, start[0])
            is not None
        ):
            raise
        if recurrence_id:
            # create_event can only add a detached event, not the occurrence.
            cause = "cannot be updated" if unsupported else "no longer exists"
            raise HomeAssistantError(
                f"Occurrence {recurrence_id} of event {uid} {cause} on {cal}, "
                "and an occurrence cannot be re-created into its series"
            ) from err
        logger.info(
            "Restoring calendar event %s on %s in place is not possible (%s); "
            "re-creating it under a new uid instead",
            uid,
            cal,
            err,
        )
    return await _recreate_calendar_event(client, cal, event, start, end)
