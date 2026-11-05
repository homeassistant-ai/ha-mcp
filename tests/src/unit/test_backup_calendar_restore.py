"""Calendar restore: which failed in-place updates may fall back to re-creating."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantCommandError,
    HomeAssistantError,
)

from .test_backup_manager import _CalendarRestClient

_SNAPSHOT = {
    "calendar_entity_id": "calendar.fam",
    "uid": "evt-1",
    "summary": "Standup",
    "start": {"dateTime": "2026-06-15T09:00:00-04:00"},
    "end": {"dateTime": "2026-06-15T09:15:00-04:00"},
}


def _fail_update(
    err: HomeAssistantCommandError, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_ws(_client: Any, _msg: dict[str, Any]) -> Any:
        raise err

    async def fail_post(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("restore must not re-create the event")

    monkeypatch.setattr(bm, "_ws_send", failing_ws)
    monkeypatch.setattr(bm, "_rest_post", fail_post)


async def test_a_failure_that_cannot_mean_a_deleted_event_is_raised_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A missing calendar entity says nothing about the event, so it is neither
    # looked up nor re-created, though the calendar view would not list the uid.
    err = HomeAssistantCommandError("Command failed: Entity not found", "not_found")
    _fail_update(err, monkeypatch)
    client = _CalendarRestClient([])
    with pytest.raises(HomeAssistantCommandError) as exc_info:
        await bm._restore_calendar_event(client, "calendar.fam::evt-1", _SNAPSHOT)
    assert exc_info.value is err
    assert client.calls == []


async def test_a_failed_lookup_is_raised_instead_of_reading_as_a_deleted_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Whether the event still exists is unknown, so re-creating it could leave
    # a duplicate.
    lookup_error = HomeAssistantAPIError("API error: 500", status_code=500)

    class _BrokenCalendar:
        async def _request(self, *_args: Any, **_kwargs: Any) -> Any:
            raise lookup_error

    _fail_update(HomeAssistantCommandError("Command failed", "failed"), monkeypatch)
    with pytest.raises(HomeAssistantAPIError) as exc_info:
        await bm._restore_calendar_event(
            _BrokenCalendar(), "calendar.fam::evt-1", _SNAPSHOT
        )
    assert exc_info.value is lookup_error


async def test_an_occurrence_on_a_calendar_without_updates_names_the_real_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The occurrence was never looked up, so the error must not claim it is gone.
    _fail_update(
        HomeAssistantCommandError(
            "Command failed: Calendar does not support event update", "not_supported"
        ),
        monkeypatch,
    )
    client = _CalendarRestClient([])
    with pytest.raises(HomeAssistantError, match="cannot be updated") as exc_info:
        await bm._restore_calendar_event(
            client,
            "calendar.fam::evt-1::20260615T090000",
            {**_SNAPSHOT, "recurrence_id": "20260615T090000"},
        )
    assert "no longer exists" not in str(exc_info.value)
    assert client.calls == []


async def test_restoring_a_deleted_event_twice_keeps_one_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first restore re-created the event under a new uid, so the second
    # finds the snapshotted uid gone again; the copy it made is on the calendar.
    _fail_update(HomeAssistantCommandError("Command failed", "failed"), monkeypatch)
    copy = {
        "uid": "evt-new",
        "summary": "Standup",
        "start": {"dateTime": "2026-06-15T09:00:00-04:00"},
        "end": {"dateTime": "2026-06-15T09:15:00-04:00"},
    }
    result = await bm._restore_calendar_event(
        _CalendarRestClient([copy]), "calendar.fam::evt-1", _SNAPSHOT
    )
    assert result == {"already_restored": True, "event": copy}


async def test_a_deleted_event_with_no_copy_is_re_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An event at the same times with other text is not the restored copy.
    _fail_update(HomeAssistantCommandError("Command failed", "failed"), monkeypatch)
    posted: list[dict[str, Any]] = []

    async def record_post(_client: Any, _path: str, data: dict[str, Any]) -> Any:
        posted.append(data)
        return []

    monkeypatch.setattr(bm, "_rest_post", record_post)
    other = {
        "uid": "evt-2",
        "summary": "Retro",
        "start": {"dateTime": "2026-06-15T09:00:00-04:00"},
        "end": {"dateTime": "2026-06-15T09:15:00-04:00"},
    }
    await bm._restore_calendar_event(
        _CalendarRestClient([other]), "calendar.fam::evt-1", _SNAPSHOT
    )
    assert [p["summary"] for p in posted] == ["Standup"]


async def test_the_restored_copy_is_found_after_a_timezone_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Local Calendar keeps floating times: after a timezone change the copy
    # shows the same wall clock under the new offset.
    _fail_update(HomeAssistantCommandError("Command failed", "failed"), monkeypatch)
    copy = {
        "uid": "evt-new",
        "summary": "Standup",
        "start": {"dateTime": "2026-06-15T09:00:00+02:00"},
        "end": {"dateTime": "2026-06-15T09:15:00+02:00"},
    }
    client = _CalendarRestClient([copy])
    result = await bm._restore_calendar_event(client, "calendar.fam::evt-1", _SNAPSHOT)
    assert result == {"already_restored": True, "event": copy}
    _path, params = client.calls[-1]
    assert params["start"] == "2026-06-14T09:00:00-04:00"
    assert params["end"] == "2026-06-16T09:15:00-04:00"


async def test_an_existing_event_far_from_today_is_not_duplicated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A calendar can answer ``failed`` for an event that still exists, such as
    # on a backend error. Edited since the snapshot, it is no copy of it, so
    # only a lookup around the snapshot's own date keeps it from being
    # duplicated. This calendar stub lists the event only in a window holding
    # its start.
    snapshot = {
        **_SNAPSHOT,
        "start": {"dateTime": "2000-01-10T09:00:00+00:00"},
        "end": {"dateTime": "2000-01-10T09:15:00+00:00"},
    }
    edited = {**snapshot, "summary": "Standup (moved room)"}
    at = datetime.fromisoformat(snapshot["start"]["dateTime"])

    class _WindowedCalendar(_CalendarRestClient):
        async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
            await super()._request(method, path, **kwargs)
            start, end = (
                datetime.fromisoformat(kwargs["params"][k]) for k in ("start", "end")
            )
            return [edited] if start <= at <= end else []

    err = HomeAssistantCommandError("Command failed: An error occurred", "failed")
    _fail_update(err, monkeypatch)
    with pytest.raises(HomeAssistantCommandError) as exc_info:
        await bm._restore_calendar_event(
            _WindowedCalendar([]), "calendar.fam::evt-1", snapshot
        )
    assert exc_info.value is err
