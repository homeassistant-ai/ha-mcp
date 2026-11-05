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


# Bounds of the second sweep below: wide enough for a past event or one booked
# well ahead, without asking a busy calendar to expand a decade of recurrences.
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
    """Return the event with ``uid`` on ``cal`` between ``start`` and ``end``.

    Reads the REST calendar view rather than the ``calendar.get_events``
    service: the service response is built by HA's
    ``_list_events_dict_factory``, which keeps only ``LIST_EVENT_FIELDS``
    (start/end/summary/description/location/status) and therefore carries no
    ``uid`` to match on. ``/api/calendars/{entity_id}`` serialises the whole
    CalendarEvent, so uid, recurrence_id and rrule survive — the same endpoint
    ``ha_config_get_calendar_events`` reads.

    A recurring series is expanded into occurrences that all share the ``uid``,
    so ``recurrence_id`` selects which one; without it the first match wins.
    """
    try:
        events = await client._request(
            "GET",
            f"/calendars/{cal}",
            params={"start": start.isoformat(), "end": end.isoformat()},
        )
    except HomeAssistantError as err:
        # Only treat 404 (calendar integration not loaded) as "skip silently".
        # Auth/transport/server errors deserve a WARNING so an operator
        # can spot a misconfigured calendar integration; matches the
        # ``status_code == 404`` narrowing the automation/script/scene
        # fetchers use.
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


def _recurrence_id_window(
    recurrence_id: str | None,
) -> tuple[datetime, datetime] | None:
    """A day-wide window around an iCalendar recurrence id, if it parses."""
    if not recurrence_id:
        return None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M%SZ", "%Y%m%d"):
        try:
            at = datetime.strptime(recurrence_id, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
        return at - timedelta(days=1), at + timedelta(days=1)
    return None


async def _locate_calendar_event(
    client: Any, cal: str, uid: str, wanted: str | None
) -> Any:
    """The ``uid`` event (occurrence ``wanted``) on ``cal``; None if no window has it.

    Out-of-window events and cancelled ones Local Calendar hides also read as None.
    """
    # Configurable lookahead window. Default 7 days catches typical edits;
    # set HAMCP_AUTO_BACKUP_CALENDAR_LOOKAHEAD_DAYS to widen for far-future
    # events or narrow to skip noise. Bounded so a typo can't query
    # decades of history.
    try:
        from .config import get_global_settings

        days = int(
            getattr(get_global_settings(), "auto_backup_calendar_lookahead_days", 7)
        )
    except (AttributeError, ImportError, ValueError, TypeError):
        days = 7
    days = max(1, min(365, days))
    now = datetime.now(UTC)
    # A recurrence_id names the occurrence's own date, so an occurrence far
    # outside the windows below is still found in one request.
    around = _recurrence_id_window(wanted)
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
        # The configured window is the cheap common case, not the contract: a
        # write targets an event by uid, and an event being edited or deleted
        # can sit in the past or well beyond the lookahead. Missing it would
        # silently skip the snapshot, or make a restore re-create an event that
        # still exists, so sweep once more over a wide window.
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
    # entity_id is "<calendar.entity>::<event_uid>[::<recurrence_id>]"
    cal, _, rest = entity_id.partition("::")
    uid, _, recurrence_id = rest.partition("::")
    if not cal or not uid:
        return None
    wanted = recurrence_id or None
    found = await _locate_calendar_event(client, cal, uid, wanted)
    if found is None:
        return None
    if wanted is None and found.get("rrule"):
        # The write targets a whole series, but the calendar view only exposes
        # expanded OCCURRENCES — never the master event's own start. Restoring
        # one occurrence's values would revert a single date and leave the rest
        # of the series edited, or re-create a series starting at the wrong
        # date, both reported as a successful restore. No snapshot is the
        # honest answer; a single-occurrence write (which carries a
        # recurrence_id) is captured normally.
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
    """Return ``(iso_value, is_date_only)`` for a snapshotted event boundary.

    The REST view wraps each boundary as ``{"dateTime": ...}`` (timed) or
    ``{"date": ...}`` (all-day); snapshots taken before that switch hold a
    flat ISO string, which a 10-character length identifies as date-only.
    """
    if isinstance(value, dict):
        if value.get("dateTime"):
            return str(value["dateTime"]), False
        if value.get("date"):
            return str(value["date"]), True
        return None
    if isinstance(value, str) and value:
        return value, len(value) == 10
    return None


async def _restored_copy(
    client: Any,
    cal: str,
    event: dict[str, Any],
    start: tuple[str, bool],
    end: tuple[str, bool],
) -> dict[str, Any] | None:
    """An event on ``cal`` with the snapshot's summary, times and text.

    A re-created event gets a new uid, so restoring the same snapshot again
    finds the snapshotted uid gone again; this is the copy the earlier restore
    made. None when the calendar has none (a 404 included: the re-create then
    fails on the missing calendar itself).
    """
    bounds = [
        f"{value}T00:00:00" if date_only else value for value, date_only in (start, end)
    ]
    try:
        events = await client._request(
            "GET", f"/calendars/{cal}", params={"start": bounds[0], "end": bounds[1]}
        )
    except HomeAssistantError as err:
        if getattr(err, "status_code", None) == 404:
            return None
        raise
    for found in _manager()._require_list(events, f"/calendars/{cal}"):
        if (
            isinstance(found, dict)
            and _calendar_bound(found.get("start")) == start
            and _calendar_bound(found.get("end")) == end
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
    """Create an event that no longer exists under its snapshotted uid.

    Always a single event: a series-wide write is never snapshotted (the
    calendar view cannot describe the series), and a snapshot of one
    occurrence must not be re-created as a series of its own.
    """
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

    # Home Assistant merges the update into the stored event (ical dumps it
    # with ``exclude_unset``), so a field left out keeps whatever the write
    # put there. Send the text fields unconditionally — empty clears a
    # description or location the write added.
    event: dict[str, Any] = {
        "summary": config.get("summary") or "",
        "dtstart": start[0],
        "dtend": end[0],
        "description": config.get("description") or "",
        "location": config.get("location") or "",
    }
    # ``rrule`` is deliberately never replayed. Every expanded occurrence
    # carries the series' rule, and sending it back onto one occurrence —
    # which ical has already forked into a plain event — would make that
    # occurrence a second series under the same uid, duplicating every later
    # date.

    if not uid:
        return await _recreate_calendar_event(client, cal, event, start, end)

    # An edited event still exists under its uid, so put the captured values
    # back in place instead of creating a second copy.
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
        # Re-create only when the event is gone or the calendar has no
        # UPDATE_EVENT. Local Calendar reports a missing event as a plain
        # ``failed`` with generic text, so look the uid up; other codes cannot
        # mean a deleted event. A failing lookup propagates, except a 404 (the
        # calendar is gone), on which the re-create fails in turn.
        if not unsupported and (
            err.code != "failed"
            or await _locate_calendar_event(client, cal, uid, recurrence_id) is not None
        ):
            raise
        if recurrence_id:
            # The occurrence is gone (deleted from the series, or the series
            # moved and this recurrence_id no longer names one of its dates),
            # or the calendar cannot update it. ``create_event`` could only add
            # a detached event next to the series, which is not the occurrence
            # being restored, so say so instead of inventing one.
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
