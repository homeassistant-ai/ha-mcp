"""Calendar restore: which failed in-place updates may fall back to re-creating."""

from __future__ import annotations

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
