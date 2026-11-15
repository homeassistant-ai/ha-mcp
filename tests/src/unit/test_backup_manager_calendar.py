"""The calendar event handler: finding an event to snapshot, putting it back.

``calendar.fam`` is Core's Local Calendar, set up from its config entry with its
``.ics`` file in the test's config directory; events are written through Core's
calendar WebSocket commands, and read back through Core's calendar view as the
handler reads them. The windows the handler asks for are what Core's calendar
is asked to expand, observed with a call-through spy.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun import freeze_time
from homeassistant.components.calendar import CalendarEntity, CalendarEntityFeature
from homeassistant.components.local_calendar.calendar import LocalCalendarEntity
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandTimeout,
    HomeAssistantConnectionError,
    HomeAssistantError,
)
from ha_mcp.tools import helpers

from ._backup_core import backup_settings as backup_settings
from ._backup_core import place_snapshot
from ._backup_core import snapshot_manager as snapshot_manager
from ._calendars import (
    calendar_config_dir,
    listed_events,
    local_calendar,
    write_event,
)
from ._component_core import ws_result

# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


@pytest.fixture(autouse=True)
def fixed_now() -> Iterator[None]:
    """Friday noon in New York, so a weekly series crosses the March DST change.
    The clock keeps ticking from there: Core's timers still have to fire."""
    with freeze_time("2026-03-06T17:00:00Z", tick=True):
        yield


@pytest.fixture(autouse=True)
async def new_york(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("America/New_York")


@pytest.fixture
async def fam(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> CalendarEntity:
    """``calendar.fam``: Core's Local Calendar."""
    return await local_calendar(hass, "Fam")


@pytest.fixture
async def ws(
    hass: HomeAssistant, fam: CalendarEntity, hass_ws_client: WebSocketGenerator
) -> MockHAClientWebSocket:
    return await hass_ws_client(hass)


def _soon(days: int = 1) -> dt.datetime:
    """09:00 local, ``days`` from today: inside the handler's first window when
    ``days`` is small, outside it when large."""
    return (dt_util.now() + dt.timedelta(days=days)).replace(
        hour=9, minute=0, second=0, microsecond=0, tzinfo=None
    )


async def _events(client: HomeAssistantClient) -> list[dict[str, Any]]:
    return await listed_events(client, "calendar.fam")


async def _event(client: HomeAssistantClient, summary: str) -> dict[str, Any]:
    (found,) = [e for e in await _events(client) if e["summary"] == summary]
    return found


async def _weekly(ws: MockHAClientWebSocket, summary: str, start: dt.datetime) -> None:
    """A three-occurrence weekly series (Core's calendar commands expand it)."""
    await write_event(ws, "calendar.fam", summary, start, rrule="FREQ=WEEKLY;COUNT=3")


async def _fetch(
    manager: bm.BackupManager, client: HomeAssistantClient, key: str
) -> Any:
    handler = manager.handler_for("calendar_event")
    assert handler is not None
    return await handler.fetch(client, key)


async def test_an_event_is_found_by_uid_and_tagged_with_its_calendar(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """Core's calendar view is the only read that carries ``uid``;
    ``calendar.get_events`` strips it, so a service-backed fetch could never
    match and silently captured nothing."""
    await write_event(ws, "calendar.fam", "Lunch", _soon())
    await write_event(ws, "calendar.fam", "Dinner", _soon())
    dinner = await _event(core_client, "Dinner")

    held = await _fetch(snapshot_manager, core_client, f"calendar.fam::{dinner['uid']}")

    assert held == {"calendar_entity_id": "calendar.fam", **dinner}


async def test_an_event_in_the_first_window_does_not_trigger_the_wide_sweep(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    fam: CalendarEntity,
    ws: MockHAClientWebSocket,
) -> None:
    await write_event(ws, "calendar.fam", "Dinner", _soon())
    dinner = await _event(core_client, "Dinner")

    with patch.object(fam, "async_get_events", wraps=fam.async_get_events) as reads:
        await _fetch(snapshot_manager, core_client, f"calendar.fam::{dinner['uid']}")

    assert reads.call_count == 1


async def test_a_missing_uid_sweeps_a_wider_window_before_giving_up(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    fam: CalendarEntity,
    ws: MockHAClientWebSocket,
) -> None:
    await write_event(ws, "calendar.fam", "Lunch", _soon())

    with patch.object(fam, "async_get_events", wraps=fam.async_get_events) as reads:
        held = await _fetch(snapshot_manager, core_client, "calendar.fam::no-such-uid")

    assert held is None
    assert reads.call_count == 2


async def test_an_event_booked_far_ahead_is_found_by_the_wide_sweep(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    fam: CalendarEntity,
    ws: MockHAClientWebSocket,
) -> None:
    """No snapshot means the prior values are unrecoverable after the write."""
    await write_event(ws, "calendar.fam", "Anniversary", _soon(300))
    anniversary = await _event(core_client, "Anniversary")

    with patch.object(fam, "async_get_events", wraps=fam.async_get_events) as reads:
        held = await _fetch(
            snapshot_manager, core_client, f"calendar.fam::{anniversary['uid']}"
        )

    assert held == {"calendar_entity_id": "calendar.fam", **anniversary}
    assert reads.call_count == 2
    (_, narrow_start, narrow_end), (_, wide_start, wide_end) = (
        call.args for call in reads.call_args_list
    )
    assert wide_start < narrow_start
    assert wide_end > narrow_end


async def test_an_occurrence_decades_out_is_found_in_one_request_around_its_date(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    fam: CalendarEntity,
    ws: MockHAClientWebSocket,
) -> None:
    """The recurrence id names the date, so one request finds an occurrence far
    outside every now-relative window."""
    await _weekly(ws, "Decade out", dt.datetime(2036, 6, 15, 9))
    occurrence = (await _events(core_client))[0]

    with patch.object(fam, "async_get_events", wraps=fam.async_get_events) as reads:
        held = await _fetch(
            snapshot_manager,
            core_client,
            f"calendar.fam::{occurrence['uid']}::{occurrence['recurrence_id']}",
        )

    assert held == {"calendar_entity_id": "calendar.fam", **occurrence}
    assert reads.call_count == 1
    _, start, end = reads.call_args.args
    assert start.date() == dt.date(2036, 6, 14)
    assert end.date() == dt.date(2036, 6, 16)


async def test_an_all_day_occurrence_is_found_by_its_date_only_recurrence_id(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """An all-day series gives a bare date as its recurrence id."""
    await ws_result(
        ws,
        "calendar/event/create",
        entity_id="calendar.fam",
        event={
            "summary": "Holiday",
            "dtstart": "2036-06-15",
            "dtend": "2036-06-16",
            "rrule": "FREQ=WEEKLY;COUNT=3",
        },
    )
    occurrence = (await _events(core_client))[0]
    assert len(occurrence["recurrence_id"]) == len("20360615")

    held = await _fetch(
        snapshot_manager,
        core_client,
        f"calendar.fam::{occurrence['uid']}::{occurrence['recurrence_id']}",
    )

    assert held == {"calendar_entity_id": "calendar.fam", **occurrence}


async def test_the_named_occurrence_of_a_series_is_the_one_selected(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """A series expands into occurrences that share a uid, so the recurrence id
    in the key picks the one the write targets."""
    await _weekly(ws, "Standup", _soon())
    first, second, _ = await _events(core_client)

    held = await _fetch(
        snapshot_manager,
        core_client,
        f"calendar.fam::{second['uid']}::{second['recurrence_id']}",
    )

    assert held == {"calendar_entity_id": "calendar.fam", **second}
    assert held["recurrence_id"] != first["recurrence_id"]


async def test_a_series_wide_write_is_not_snapshotted(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """Without a recurrence id the write targets the whole series, whose own
    start the calendar view never exposes. A snapshot of one occurrence would
    restore a single date and report success, so the capture is skipped."""
    await _weekly(ws, "Standup", _soon())
    occurrence = (await _events(core_client))[0]

    assert (
        await _fetch(
            snapshot_manager, core_client, f"calendar.fam::{occurrence['uid']}"
        )
        is None
    )


async def _capture(
    manager: bm.BackupManager, client: HomeAssistantClient, key: str
) -> str:
    """The name of a snapshot of ``key``, captured the way a write captures it."""
    captured = await manager.maybe_snapshot("calendar_event", key, force=True)
    assert captured is not None
    return captured.name


async def test_restoring_an_edited_event_updates_it_in_place(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """An edited event still exists under its uid: restoring puts the captured
    values back instead of creating a duplicate, and a description the write
    added goes, since Core merges an update into the stored event."""
    start = _soon()
    await write_event(ws, "calendar.fam", "Dinner", start, location="Home")
    dinner = await _event(core_client, "Dinner")
    name = await _capture(
        snapshot_manager, core_client, f"calendar.fam::{dinner['uid']}"
    )
    await write_event(
        ws,
        "calendar.fam",
        "Dinner (moved)",
        start,
        dinner["uid"],
        location="Pub",
        description="Bring wine",
    )

    await snapshot_manager.restore_snapshot(name, take_safety_backup=False)

    (restored,) = await _events(core_client)
    assert restored["uid"] == dinner["uid"]
    assert restored["summary"] == "Dinner"
    assert restored["location"] == "Home"
    assert not restored.get("description")


async def test_restoring_a_deleted_all_day_event_recreates_it_as_dates(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """The in-place update fails (the uid is gone) and Local Calendar's message
    does not say the event is gone, so the event is re-created; an all-day
    snapshot goes back as date-only fields."""
    first_day = _soon(2).date()
    await ws_result(
        ws,
        "calendar/event/create",
        entity_id="calendar.fam",
        event={
            "summary": "Vacation",
            "dtstart": first_day.isoformat(),
            "dtend": (first_day + dt.timedelta(days=7)).isoformat(),
        },
    )
    vacation = await _event(core_client, "Vacation")
    name = await _capture(
        snapshot_manager, core_client, f"calendar.fam::{vacation['uid']}"
    )
    await ws_result(
        ws, "calendar/event/delete", entity_id="calendar.fam", uid=vacation["uid"]
    )

    await snapshot_manager.restore_snapshot(name, take_safety_backup=False)

    (recreated,) = await _events(core_client)
    assert recreated["summary"] == "Vacation"
    assert recreated["start"] == {"date": first_day.isoformat()}
    assert recreated["end"] == {"date": (first_day + dt.timedelta(days=7)).isoformat()}


async def test_restoring_one_occurrence_never_forks_a_second_series(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """Every expanded occurrence carries the series' rule; sending it back would
    start a second series under the same uid."""
    await _weekly(ws, "Standup", _soon())
    before = await _events(core_client)
    second = before[1]
    key = f"calendar.fam::{second['uid']}::{second['recurrence_id']}"
    name = await _capture(snapshot_manager, core_client, key)
    await ws_result(
        ws,
        "calendar/event/update",
        entity_id="calendar.fam",
        uid=second["uid"],
        recurrence_id=second["recurrence_id"],
        event={
            "summary": "Standup (moved)",
            "dtstart": second["start"]["dateTime"],
            "dtend": second["end"]["dateTime"],
        },
    )

    await snapshot_manager.restore_snapshot(name, take_safety_backup=False)

    after = await _events(core_client)
    assert [e["summary"] for e in after] == ["Standup"] * 3
    assert {e["uid"] for e in after} == {second["uid"]}


async def test_the_recurrence_id_in_the_key_selects_the_occurrence_to_restore(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """A snapshot whose body names no recurrence id restores the occurrence
    its key names, and no other."""
    await _weekly(ws, "Standup", _soon())
    first, second, third = await _events(core_client)
    key = f"calendar.fam::{second['uid']}::{second['recurrence_id']}"
    await ws_result(
        ws,
        "calendar/event/update",
        entity_id="calendar.fam",
        uid=second["uid"],
        recurrence_id=second["recurrence_id"],
        event={
            "summary": "Standup (moved)",
            "dtstart": second["start"]["dateTime"],
            "dtend": second["end"]["dateTime"],
        },
    )
    held = {
        key_: value
        for key_, value in {"calendar_entity_id": "calendar.fam", **second}.items()
        if key_ not in ("recurrence_id", "rrule")
    }
    named = place_snapshot(
        snapshot_manager.backup_dir,
        "calendar_event.occurrence.20260101_000000.yaml",
        key,
        domain="calendar_event",
        held=held,
        text=False,
    )

    await snapshot_manager.restore_snapshot(named.name, take_safety_backup=False)

    after = await _events(core_client)
    assert [e["summary"] for e in after] == ["Standup"] * 3
    assert [e["recurrence_id"] for e in after] == [
        first["recurrence_id"],
        second["recurrence_id"],
        third["recurrence_id"],
    ]


async def test_an_occurrence_that_is_gone_is_not_re_created_beside_its_series(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """An occurrence cannot be put back into its series; create_event would
    only add a detached event beside it."""
    await _weekly(ws, "Standup", _soon())
    second = (await _events(core_client))[1]
    key = f"calendar.fam::{second['uid']}::{second['recurrence_id']}"
    name = await _capture(snapshot_manager, core_client, key)
    await ws_result(
        ws, "calendar/event/delete", entity_id="calendar.fam", uid=second["uid"]
    )

    with pytest.raises(HomeAssistantError, match="no longer exists"):
        await snapshot_manager.restore_snapshot(name, take_safety_backup=False)

    assert await _events(core_client) == []


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(HomeAssistantConnectionError("WebSocket closed"), id="connection"),
        pytest.param(HomeAssistantCommandTimeout("Command timeout"), id="timeout"),
    ],
)
async def test_an_update_that_gets_no_answer_is_not_answered_with_a_duplicate(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A dropped connection or a timeout could have applied the update, so
    re-creating would duplicate the event instead of restoring it."""
    await write_event(ws, "calendar.fam", "Dinner", _soon())
    dinner = await _event(core_client, "Dinner")
    name = await _capture(
        snapshot_manager, core_client, f"calendar.fam::{dinner['uid']}"
    )
    monkeypatch.setattr(
        helpers, "get_connected_ws_client", AsyncMock(side_effect=error)
    )

    with pytest.raises(type(error)):
        await snapshot_manager.restore_snapshot(name, take_safety_backup=False)

    assert [e["uid"] for e in await _events(core_client)] == [dinner["uid"]]


async def test_a_calendar_that_cannot_update_events_gets_the_event_re_created(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    """Core's ``not_supported`` proves the calendar can never accept the
    update, so re-creating is the only restore it can offer."""
    await write_event(ws, "calendar.fam", "Dinner", _soon())
    dinner = await _event(core_client, "Dinner")
    name = await _capture(
        snapshot_manager, core_client, f"calendar.fam::{dinner['uid']}"
    )
    await write_event(ws, "calendar.fam", "Dinner (moved)", _soon(), dinner["uid"])
    without_updates = property(
        lambda _: (
            CalendarEntityFeature.CREATE_EVENT | CalendarEntityFeature.DELETE_EVENT
        )
    )

    with patch.object(LocalCalendarEntity, "supported_features", without_updates):
        await snapshot_manager.restore_snapshot(name, take_safety_backup=False)

    assert sorted(e["summary"] for e in await _events(core_client)) == [
        "Dinner",
        "Dinner (moved)",
    ]


async def test_a_snapshot_without_times_cannot_be_restored(
    snapshot_manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    broken = place_snapshot(
        snapshot_manager.backup_dir,
        "calendar_event.calendar.fam_evt-1.20260101_000000.yaml",
        "calendar.fam::evt-1",
        domain="calendar_event",
        held={
            "calendar_entity_id": "calendar.fam",
            "uid": "evt-1",
            "summary": "No times",
        },
        text=False,
    )

    with pytest.raises(HomeAssistantError, match="no usable start/end"):
        await snapshot_manager.restore_snapshot(broken.name, take_safety_backup=False)

    assert await _events(core_client) == []
