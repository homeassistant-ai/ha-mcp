"""Calendar restore: which failed in-place updates may fall back to re-creating.

``calendar.fam`` is Core's Local Calendar, set up from its config entry with
its ``.ics`` file in the test's config directory; events are written through
Core's calendar WebSocket commands. A snapshot is placed in the backup
directory with the event as Core's calendar view lists it, the way a capture
stores it; the restore runs through the manager.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from homeassistant.components.calendar import CalendarEntity
from homeassistant.components.local_calendar.calendar import LocalCalendarEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError as CoreHomeAssistantError
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantError,
)

from ._calendars import (
    calendar_config_dir,
    listed_events,
    local_calendar,
    remote_calendar,
    write_event,
)
from ._component_core import ws_result

STANDUP = dt.datetime(2026, 6, 15, 9)
# A published calendar Remote Calendar reads; it has no update feature.
BOARD_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//ha-mcp tests//EN
BEGIN:VEVENT
UID:evt-1
DTSTAMP:20260101T000000Z
SUMMARY:Standup
DTSTART:20260615T090000
DTEND:20260615T091500
END:VEVENT
END:VCALENDAR
"""


# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


@pytest.fixture(autouse=True)
async def new_york(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("America/New_York")


@pytest.fixture
async def manager(core_client: HomeAssistantClient, tmp_path: Path) -> bm.BackupManager:
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    return bm.get_backup_manager(core_client, settings)


@pytest.fixture
async def fam(hass: HomeAssistant, core_client: HomeAssistantClient) -> CalendarEntity:
    """``calendar.fam``: Core's Local Calendar."""
    return await local_calendar(hass, "Fam")


@pytest.fixture
async def ws(
    hass: HomeAssistant, fam: CalendarEntity, hass_ws_client: WebSocketGenerator
) -> MockHAClientWebSocket:
    return await hass_ws_client(hass)


async def _put(
    ws: MockHAClientWebSocket,
    summary: str,
    start: dt.datetime = STANDUP,
    uid: str | None = None,
) -> None:
    await write_event(ws, "calendar.fam", summary, start, uid)


async def _delete(ws: MockHAClientWebSocket, uid: str) -> None:
    await ws_result(ws, "calendar/event/delete", entity_id="calendar.fam", uid=uid)


async def _events(
    client: HomeAssistantClient, cal: str = "calendar.fam"
) -> list[dict[str, Any]]:
    return await listed_events(client, cal)


async def _snapshot(
    manager: bm.BackupManager,
    client: HomeAssistantClient,
    cal: str = "calendar.fam",
    into: str | None = None,
    **extra: Any,
) -> tuple[str, str]:
    """Place a snapshot of the one event on ``cal``; its name and the uid.

    ``into`` names another calendar for the snapshot to belong to.
    """
    (event,) = await _events(client, cal)
    target, uid = into or cal, event["uid"]
    name = f"calendar_event.{target}_{uid}.20200101_000000.yaml"
    manager.backup_dir.mkdir(parents=True, exist_ok=True)
    (manager.backup_dir / name).write_text(
        yaml.safe_dump(
            {
                "schema_version": bm.SCHEMA_VERSION,
                "domain": "calendar_event",
                "entity_id": f"{target}::{uid}",
                "config": {"calendar_entity_id": target, **event, **extra},
            }
        ),
        encoding="utf-8",
    )
    return name, uid


async def test_a_failure_that_cannot_mean_a_deleted_event_is_raised_unchanged(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    # A missing calendar entity says nothing about the event, so it is neither
    # looked up nor re-created, though the calendar view would not list the uid.
    await _put(ws, "Standup")
    name, uid = await _snapshot(manager, core_client, into="calendar.gone")

    with pytest.raises(HomeAssistantCommandError) as exc_info:
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert exc_info.value.code == "not_found"
    assert [event["uid"] for event in await _events(core_client)] == [uid]


async def test_a_failed_lookup_is_raised_instead_of_reading_as_a_deleted_event(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    # Whether the event still exists is unknown, so re-creating it could leave
    # a duplicate.
    await _put(ws, "Standup")
    name, uid = await _snapshot(manager, core_client)
    await _delete(ws, uid)

    with (
        patch.object(
            LocalCalendarEntity,
            "async_get_events",
            side_effect=CoreHomeAssistantError("backend unavailable"),
        ) as reads,
        pytest.raises(HomeAssistantAPIError) as exc_info,
    ):
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert exc_info.value.status_code == 500
    # The first failed read ends the restore: no wider lookup, no copy search.
    assert reads.call_count == 1
    assert await _events(core_client) == []


async def test_an_occurrence_on_a_calendar_without_updates_names_the_real_cause(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # The occurrence was never looked up, so the error must not claim it is gone.
    board = await remote_calendar(hass, "Board", BOARD_ICS)
    name, _ = await _snapshot(
        manager, core_client, "calendar.board", recurrence_id="20260615T090000"
    )

    with (
        patch.object(board, "async_get_events", wraps=board.async_get_events) as reads,
        pytest.raises(HomeAssistantError, match="cannot be updated") as exc_info,
    ):
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert "no longer exists" not in str(exc_info.value)
    reads.assert_not_called()
    assert [e["summary"] for e in await _events(core_client, "calendar.board")] == [
        "Standup"
    ]


async def test_restoring_a_deleted_event_twice_keeps_one_copy(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    # The first restore re-created the event under a new uid, so the second
    # finds the snapshotted uid gone again; the copy it made is on the calendar.
    await _put(ws, "Standup")
    name, uid = await _snapshot(manager, core_client)
    await _delete(ws, uid)

    await manager.restore_snapshot(name, take_safety_backup=False)
    result = await manager.restore_snapshot(name, take_safety_backup=False)

    (copy,) = await _events(core_client)
    assert copy["summary"] == "Standup"
    assert copy["uid"] != uid
    assert result["result"]["already_restored"] is True
    assert result["result"]["event"]["uid"] == copy["uid"]


async def test_a_deleted_event_with_no_copy_is_re_created(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    # An event at the same times with other text is not the restored copy.
    await _put(ws, "Standup")
    name, uid = await _snapshot(manager, core_client)
    await _delete(ws, uid)
    await _put(ws, "Retro")

    await manager.restore_snapshot(name, take_safety_backup=False)

    events = await _events(core_client)
    assert sorted(event["summary"] for event in events) == ["Retro", "Standup"]
    assert uid not in {event["uid"] for event in events}


async def test_the_restored_copy_is_found_after_a_timezone_change(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    fam: CalendarEntity,
    ws: MockHAClientWebSocket,
) -> None:
    # Local Calendar keeps floating times: after a timezone change the copy
    # shows the same wall clock under the new offset.
    await _put(ws, "Standup")
    name, uid = await _snapshot(manager, core_client)
    await _delete(ws, uid)
    await manager.restore_snapshot(name, take_safety_backup=False)
    await hass.config.async_set_time_zone("Europe/Berlin")

    with patch.object(fam, "async_get_events", wraps=fam.async_get_events) as reads:
        result = await manager.restore_snapshot(name, take_safety_backup=False)

    assert result["result"]["already_restored"] is True
    assert result["result"]["event"]["start"] == {
        "dateTime": "2026-06-15T09:00:00+02:00"
    }
    # The copy search asked for a day either side of the snapshot's times.
    _, start, end = reads.call_args.args
    assert start == dt.datetime.fromisoformat("2026-06-14T09:00:00-04:00")
    assert end == dt.datetime.fromisoformat("2026-06-16T09:15:00-04:00")
    assert [event["summary"] for event in await _events(core_client)] == ["Standup"]


async def test_an_existing_event_far_from_today_is_not_duplicated(
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
    ws: MockHAClientWebSocket,
) -> None:
    # A calendar can answer ``failed`` for an event that still exists, such as
    # on a backend error. Edited since the snapshot, it is no copy of it, so
    # only a lookup around the snapshot's own date keeps it from being
    # duplicated.
    far = dt.datetime(2000, 1, 10, 9)
    await _put(ws, "Standup", far)
    name, uid = await _snapshot(manager, core_client)
    await _put(ws, "Standup (moved room)", far, uid)

    with (
        patch.object(
            LocalCalendarEntity,
            "async_update_event",
            side_effect=CoreHomeAssistantError("An error occurred"),
        ),
        pytest.raises(HomeAssistantCommandError) as exc_info,
    ):
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert exc_info.value.code == "failed"
    assert [event["summary"] for event in await _events(core_client)] == [
        "Standup (moved room)"
    ]
