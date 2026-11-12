"""Calendar restore: which failed in-place updates may fall back to re-creating.

Core's ``calendar`` integration serves a calendar of a test platform that
keeps floating times, as Local Calendar does: an event shows its wall clock
in Home Assistant's current timezone. A snapshot is placed in the backup
directory with the event as Core's calendar view lists it, the way a capture
stores it; the restore runs through the manager.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from homeassistant.components.calendar import (
    CalendarEntity,
    CalendarEntityFeature,
    CalendarEvent,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError as CoreHomeAssistantError
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import setup_test_component_platform

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantError,
)
from ha_mcp.tools.tools_calendar import CalendarTools

KEY = "calendar.fam::evt-1"


class FloatingCalendar(CalendarEntity):
    """``calendar.fam``: events kept as wall-clock times, like Local Calendar."""

    _attr_name = "Fam"

    def __init__(self, features: CalendarEntityFeature) -> None:
        self._attr_supported_features = features
        # uid -> (summary, naive start, naive end)
        self.events: dict[str, tuple[str, dt.datetime, dt.datetime]] = {}
        self.windows: list[tuple[dt.datetime, dt.datetime]] = []
        self.created: list[str] = []
        self.update_error: str | None = None
        self.read_error: str | None = None

    def add(self, uid: str, summary: str, start: dt.datetime, minutes: int) -> None:
        self.events[uid] = (summary, start, start + dt.timedelta(minutes=minutes))

    @property
    def event(self) -> CalendarEvent | None:
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: dt.datetime, end_date: dt.datetime
    ) -> list[CalendarEvent]:
        self.windows.append((start_date, end_date))
        if self.read_error is not None:
            raise CoreHomeAssistantError(self.read_error)
        zone = dt_util.get_default_time_zone()
        found = []
        for uid, (summary, start, end) in self.events.items():
            local_start, local_end = (
                start.replace(tzinfo=zone),
                end.replace(tzinfo=zone),
            )
            if local_start < end_date and local_end > start_date:
                found.append(
                    CalendarEvent(
                        start=local_start, end=local_end, summary=summary, uid=uid
                    )
                )
        return found

    async def async_update_event(
        self,
        uid: str,
        event: dict[str, Any],
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        if self.update_error is not None:
            raise CoreHomeAssistantError(self.update_error)
        if uid not in self.events:
            raise CoreHomeAssistantError(f"No existing event with uid {uid}")
        start, end = (dt_util.as_local(event[k]) for k in ("dtstart", "dtend"))
        self.events[uid] = (
            event["summary"],
            start.replace(tzinfo=None),
            end.replace(tzinfo=None),
        )

    async def async_create_event(self, **kwargs: Any) -> None:
        start, end = (dt_util.as_local(kwargs[k]) for k in ("dtstart", "dtend"))
        self.events[uuid.uuid4().hex] = (
            kwargs["summary"],
            start.replace(tzinfo=None),
            end.replace(tzinfo=None),
        )
        self.created.append(kwargs["summary"])


async def _calendar(
    hass: HomeAssistant,
    features: CalendarEntityFeature = (
        CalendarEntityFeature.UPDATE_EVENT | CalendarEntityFeature.CREATE_EVENT
    ),
) -> FloatingCalendar:
    entity = FloatingCalendar(features)
    setup_test_component_platform(hass, "calendar", [entity])
    assert await async_setup_component(
        hass, "calendar", {"calendar": {"platform": "test"}}
    )
    await hass.async_block_till_done()
    return entity


@pytest.fixture
async def manager(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> bm.BackupManager:
    await hass.config.async_set_time_zone("America/New_York")
    settings = SimpleNamespace(
        enable_auto_backup=True,
        auto_backup_throttle_minutes=0,
        auto_backup_retain_per_entity=5,
        auto_backup_dir=str(tmp_path / "backups"),
    )
    return bm.get_backup_manager(core_client, settings)


async def _snapshot(
    manager: bm.BackupManager,
    client: HomeAssistantClient,
    cal: str = "calendar.fam",
    **extra: Any,
) -> str:
    """Place a snapshot of ``evt-1`` as Core's calendar view lists it now."""
    listed = await CalendarTools(client).ha_config_get_calendar_events(
        "calendar.fam", start="1999-01-01T00:00:00Z", end="2100-01-01T00:00:00Z"
    )
    (event,) = [e for e in listed["events"] if e["uid"] == "evt-1"]
    name = f"calendar_event.{cal}_evt-1.20200101_000000.yaml"
    manager.backup_dir.mkdir(parents=True, exist_ok=True)
    (manager.backup_dir / name).write_text(
        yaml.safe_dump(
            {
                "schema_version": bm.SCHEMA_VERSION,
                "domain": "calendar_event",
                "entity_id": f"{cal}::evt-1",
                "config": {"calendar_entity_id": cal, **event, **extra},
            }
        ),
        encoding="utf-8",
    )
    return name


def _standup(calendar: FloatingCalendar, start: dt.datetime | None = None) -> None:
    calendar.add("evt-1", "Standup", start or dt.datetime(2026, 6, 15, 9), 15)


async def test_a_failure_that_cannot_mean_a_deleted_event_is_raised_unchanged(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # A missing calendar entity says nothing about the event, so it is neither
    # looked up nor re-created, though the calendar view would not list the uid.
    calendar = await _calendar(hass)
    _standup(calendar)
    name = await _snapshot(manager, core_client, cal="calendar.gone")

    with pytest.raises(HomeAssistantCommandError) as exc_info:
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert exc_info.value.code == "not_found"
    assert calendar.created == []


async def test_a_failed_lookup_is_raised_instead_of_reading_as_a_deleted_event(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # Whether the event still exists is unknown, so re-creating it could leave
    # a duplicate.
    calendar = await _calendar(hass)
    _standup(calendar)
    name = await _snapshot(manager, core_client)
    calendar.update_error = "backend unavailable"
    calendar.read_error = "backend unavailable"
    calendar.windows.clear()

    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert exc_info.value.status_code == 500
    # The first failed read ends the restore: no wider lookup, no copy search.
    assert len(calendar.windows) == 1
    assert calendar.created == []


async def test_an_occurrence_on_a_calendar_without_updates_names_the_real_cause(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # The occurrence was never looked up, so the error must not claim it is gone.
    calendar = await _calendar(hass, CalendarEntityFeature.CREATE_EVENT)
    _standup(calendar)
    name = await _snapshot(manager, core_client, recurrence_id="20260615T090000")
    calendar.windows.clear()

    with pytest.raises(HomeAssistantError, match="cannot be updated") as exc_info:
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert "no longer exists" not in str(exc_info.value)
    assert calendar.windows == []
    assert calendar.created == []


async def test_restoring_a_deleted_event_twice_keeps_one_copy(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # The first restore re-created the event under a new uid, so the second
    # finds the snapshotted uid gone again; the copy it made is on the calendar.
    calendar = await _calendar(hass)
    _standup(calendar)
    name = await _snapshot(manager, core_client)
    del calendar.events["evt-1"]

    await manager.restore_snapshot(name, take_safety_backup=False)
    result = await manager.restore_snapshot(name, take_safety_backup=False)

    assert calendar.created == ["Standup"]
    (copy_uid,) = calendar.events
    assert result["result"]["already_restored"] is True
    assert result["result"]["event"]["uid"] == copy_uid


async def test_a_deleted_event_with_no_copy_is_re_created(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # An event at the same times with other text is not the restored copy.
    calendar = await _calendar(hass)
    _standup(calendar)
    name = await _snapshot(manager, core_client)
    del calendar.events["evt-1"]
    calendar.add("evt-2", "Retro", dt.datetime(2026, 6, 15, 9), 15)

    await manager.restore_snapshot(name, take_safety_backup=False)

    assert calendar.created == ["Standup"]


async def test_the_restored_copy_is_found_after_a_timezone_change(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # Local Calendar keeps floating times: after a timezone change the copy
    # shows the same wall clock under the new offset.
    calendar = await _calendar(hass)
    _standup(calendar)
    name = await _snapshot(manager, core_client)
    del calendar.events["evt-1"]
    await manager.restore_snapshot(name, take_safety_backup=False)
    await hass.config.async_set_time_zone("Europe/Berlin")

    result = await manager.restore_snapshot(name, take_safety_backup=False)

    assert result["result"]["already_restored"] is True
    assert result["result"]["event"]["start"] == {
        "dateTime": "2026-06-15T09:00:00+02:00"
    }
    assert calendar.created == ["Standup"]
    start, end = calendar.windows[-1]
    assert start == dt.datetime.fromisoformat("2026-06-14T09:00:00-04:00")
    assert end == dt.datetime.fromisoformat("2026-06-16T09:15:00-04:00")


async def test_an_existing_event_far_from_today_is_not_duplicated(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    core_client: HomeAssistantClient,
) -> None:
    # A calendar can answer ``failed`` for an event that still exists, such as
    # on a backend error. Edited since the snapshot, it is no copy of it, so
    # only a lookup around the snapshot's own date keeps it from being
    # duplicated.
    calendar = await _calendar(hass)
    _standup(calendar, dt.datetime(2000, 1, 10, 9))
    name = await _snapshot(manager, core_client)
    calendar.add("evt-1", "Standup (moved room)", dt.datetime(2000, 1, 10, 9), 15)
    calendar.update_error = "An error occurred"

    with pytest.raises(HomeAssistantCommandError) as exc_info:
        await manager.restore_snapshot(name, take_safety_backup=False)

    assert exc_info.value.code == "failed"
    assert calendar.created == []
