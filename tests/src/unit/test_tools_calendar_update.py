"""Update mode in ``ha_config_set_calendar_event`` (#2482), and its auto-backup.

Home Assistant registers only ``create_event`` and ``get_events`` as REST
calendar services: updating an event lives on the WebSocket command
``calendar/event/update``, which the tool sends through the shared pooled
client like the create and delete paths.

The tool runs on the server's own client against a real ``hass`` with two
calendars: ``calendar.test`` is Core's Local Calendar, which supports updates;
``calendar.read_only`` is Core's Remote Calendar, which does not. What a write
changed is read back from Core's calendar view.
"""

import datetime as dt
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.typing import MockHAClientWebSocket, WebSocketGenerator

from ha_mcp import backup_calendar
from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_calendar import CalendarTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_manager as snapshot_manager
from ._calendars import (
    NO_EVENTS,
    calendar_config_dir,
    calendar_entity,
    listed_events,
    local_calendar,
    remote_calendar,
    write_event,
)
from ._command_faults import dropped_on, pooled_socket
from ._module_clock import set_module_clock
from ._tool_errors import error_of, rejected_call_body

START = dt.datetime(2026, 6, 15, 10)

# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


@pytest.fixture
def backups(
    snapshot_manager: bm.BackupManager, monkeypatch: pytest.MonkeyPatch
) -> bm.BackupManager:
    """Auto-backup on, its calendar search run from days before ``START``."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, backup_calendar, now)
    return snapshot_manager


@pytest.fixture
async def ws(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
) -> MockHAClientWebSocket:
    """A WebSocket client on ``hass``, with ``calendar.test`` and
    ``calendar.read_only``."""
    await local_calendar(hass, "Test")
    await remote_calendar(hass, "Read only", NO_EVENTS)
    return await hass_ws_client(hass)


async def _one_off(
    ws: MockHAClientWebSocket, client: HomeAssistantClient, **fields: Any
) -> dict[str, Any]:
    """A single event ``Meeting`` at ``START``, as Core lists it."""
    await write_event(ws, "calendar.test", "Meeting", START, **fields)
    [event] = await listed_events(client, "calendar.test")
    return event


async def _daily(
    ws: MockHAClientWebSocket, client: HomeAssistantClient
) -> list[dict[str, Any]]:
    """A three-day series ``Standup`` from ``START``, as Core lists it."""
    await write_event(ws, "calendar.test", "Standup", START, rrule="FREQ=DAILY;COUNT=3")
    return await listed_events(client, "calendar.test")


async def _update(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    """``Renamed`` from 11:00 to 11:30 on ``START``'s day: an update of event
    ``uid`` when one is given, else a new event."""
    fields: dict[str, Any] = {
        "entity_id": "calendar.test",
        "summary": "Renamed",
        "start": "2026-06-15T11:00:00",
        "end": "2026-06-15T11:30:00",
        **kwargs,
    }
    result: dict[str, Any] = await CalendarTools(client).ha_config_set_calendar_event(
        **fields
    )
    return result


async def _refused(client: HomeAssistantClient, **kwargs: Any) -> dict[str, Any]:
    """The error object of an update the tool refuses."""
    with pytest.raises(ToolError) as exc_info:
        await _update(client, **kwargs)
    return error_of(exc_info)


async def test_an_update_rewrites_the_event_in_place(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """A uid makes the call an update of that event, not a second event, and a
    description or location left out is cleared: the update is a replacement."""
    event = await _one_off(ws, core_client, description="Agenda", location="Room 1")

    result = await _update(core_client, uid=event["uid"])

    [updated] = await listed_events(core_client, "calendar.test")
    assert updated["uid"] == event["uid"]
    assert updated["summary"] == "Renamed"
    assert updated["start"]["dateTime"].startswith("2026-06-15T11:00:00")
    assert updated["end"]["dateTime"].startswith("2026-06-15T11:30:00")
    assert (updated["description"], updated["location"]) == ("", "")
    assert result["success"] is True
    assert result["uid"] == event["uid"]
    assert "updated" in result["message"]


async def test_an_update_from_one_occurrence_onwards_leaves_earlier_ones(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """The occurrence id and the range reach the calendar with the event's
    description and location."""
    first, second, _ = await _daily(ws, core_client)

    result = await _update(
        core_client,
        start="2026-06-16T10:00:00",
        end="2026-06-16T10:15:00",
        description="desc",
        location="loc",
        uid=second["uid"],
        recurrence_id=second["recurrence_id"],
        recurrence_range="THISANDFUTURE",
    )

    events = await listed_events(core_client, "calendar.test")
    assert [e["summary"] for e in events] == ["Standup", "Renamed", "Renamed"]
    assert events[0]["recurrence_id"] == first["recurrence_id"]
    assert [(e["description"], e["location"]) for e in events[1:]] == [
        ("desc", "loc")
    ] * 2
    assert result["recurrence_id"] == second["recurrence_id"]
    assert result["recurrence_range"] == "THISANDFUTURE"


async def test_an_update_with_a_rule_turns_the_event_into_a_series(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    event = await _one_off(ws, core_client)

    await _update(core_client, uid=event["uid"], rrule="FREQ=DAILY;COUNT=3")

    events = await listed_events(core_client, "calendar.test")
    assert [e["summary"] for e in events] == ["Renamed"] * 3


async def test_a_calendar_without_updates_says_so(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """Core refuses an update on Remote Calendar and names the update."""
    error = await _refused(core_client, entity_id="calendar.read_only", uid="evt-123")

    assert "Calendar does not support event update" in error["message"]
    assert "integration does not offer" in error["suggestion"]


async def test_an_update_of_an_unknown_calendar_points_at_the_calendar_search(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """Core's not_found names the calendar entity, never the event."""
    error = await _refused(core_client, entity_id="calendar.missing", uid="evt-123")

    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "ha_search" in error["suggestion"]


async def test_an_update_of_an_unknown_uid_points_at_the_event_list(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """Local Calendar answers an unknown uid with a generic failure, so the
    hint is where to read the uids."""
    await _one_off(ws, core_client)

    error = await _refused(core_client, uid="missing-uid")

    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "ha_config_get_calendar_events" in error["suggestion"]


async def test_an_update_with_no_answer_gets_connectivity_guidance_only(
    hass: HomeAssistant, ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """A dropped connection is not a calendar problem: no uid hints."""
    event = await _one_off(ws, core_client)
    socket = await pooled_socket(core_client)

    with dropped_on(hass, "calendar/event/update", socket):
        error = await _refused(core_client, uid=event["uid"])

    assert error["code"] in ("CONNECTION_FAILED", "CONNECTION_TIMEOUT")
    assert all("uid" not in s.lower() for s in error["suggestions"])


@pytest.mark.parametrize("recurrence", ["recurrence_id", "recurrence_range"])
async def test_recurrence_targeting_without_uid_creates_nothing(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient, recurrence: str
) -> None:
    """Recurrence targeting is meaningless when creating a new event."""
    value = {"recurrence_id": "20260615T100000", "recurrence_range": "THISANDFUTURE"}

    error = await _refused(core_client, **{recurrence: value[recurrence]})

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert await listed_events(core_client, "calendar.test") == []


async def test_a_blank_uid_is_refused_by_name(
    core_client: HomeAssistantClient,
) -> None:
    """A blank uid would reach HA, which answers it with no more than a
    failed update."""
    body = await rejected_call_body(_update(core_client, uid="   "))

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["parameter"] == "uid"


async def test_a_blank_recurrence_id_does_not_rewrite_the_series(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """Core reads a blank occurrence id as none and would edit every date."""
    series = await _daily(ws, core_client)

    body = await rejected_call_body(
        _update(core_client, uid=series[0]["uid"], recurrence_id="")
    )

    assert body["parameter"] == "recurrence_id"
    assert await listed_events(core_client, "calendar.test") == series


async def test_a_range_without_an_occurrence_does_not_rewrite_the_series(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """The range starts AT the occurrence; without one Core edits the master."""
    series = await _daily(ws, core_client)

    error = await _refused(
        core_client, uid=series[0]["uid"], recurrence_range="THISANDFUTURE"
    )

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "recurrence_id" in error["message"]
    assert await listed_events(core_client, "calendar.test") == series


async def test_a_mixed_date_and_time_update_leaves_the_event(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    event = await _one_off(ws, core_client)

    error = await _refused(
        core_client, uid=event["uid"], start="2026-07-04", end="2026-07-04T12:00:00"
    )

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert await listed_events(core_client, "calendar.test") == [event]


async def test_a_misspelled_range_does_not_shrink_to_one_occurrence(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    """The Literal binds MCP calls only; Core compares the range verbatim, so a
    direct call's "THIS_AND_FUTURE" would edit only the named date."""
    series = await _daily(ws, core_client)

    error = await _refused(
        core_client,
        uid=series[1]["uid"],
        recurrence_id=series[1]["recurrence_id"],
        recurrence_range="THIS_AND_FUTURE",
    )

    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "THISANDFUTURE" in error["message"]
    assert await listed_events(core_client, "calendar.test") == series


async def test_a_misspelled_range_does_not_delete_one_occurrence(
    ws: MockHAClientWebSocket, core_client: HomeAssistantClient
) -> None:
    series = await _daily(ws, core_client)

    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_remove_calendar_event(
            entity_id="calendar.test",
            uid=series[1]["uid"],
            recurrence_id=series[1]["recurrence_id"],
            recurrence_range="THIS_AND_FUTURE",
        )

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert await listed_events(core_client, "calendar.test") == series


def _captured(manager: bm.BackupManager) -> dict[str, Any]:
    """Each calendar snapshot's id and the summary it holds."""
    return {
        row["entity_id"]: manager.read_snapshot(row["name"])["config"]["summary"]
        for row in manager.list_snapshots(domain="calendar_event")
    }


async def test_an_updated_event_is_snapshotted_first(
    backups: bm.BackupManager,
    ws: MockHAClientWebSocket,
    core_client: HomeAssistantClient,
) -> None:
    event = await _one_off(ws, core_client)

    await _update(core_client, uid=event["uid"])

    assert _captured(backups) == {f"calendar.test::{event['uid']}": "Meeting"}


async def test_a_create_does_not_search_the_calendar_for_a_snapshot(
    hass: HomeAssistant,
    backups: bm.BackupManager,
    ws: MockHAClientWebSocket,
    core_client: HomeAssistantClient,
) -> None:
    """A new event has no uid to look up; the search would cost two sweeps."""
    entity = calendar_entity(hass, "calendar.test")

    with patch.object(
        entity, "async_get_events", wraps=entity.async_get_events
    ) as reads:
        await _update(core_client)

    reads.assert_not_called()
    assert [
        e["summary"] for e in await listed_events(core_client, "calendar.test")
    ] == ["Renamed"]


async def test_an_updated_occurrence_is_snapshotted_by_its_recurrence_id(
    backups: bm.BackupManager,
    ws: MockHAClientWebSocket,
    core_client: HomeAssistantClient,
) -> None:
    """Every occurrence of a series shares the uid, so the key names the one
    the write targets."""
    _, second, _ = await _daily(ws, core_client)

    await _update(
        core_client,
        start="2026-06-16T10:00:00",
        end="2026-06-16T10:15:00",
        uid=second["uid"],
        recurrence_id=second["recurrence_id"],
    )

    key = f"calendar.test::{second['uid']}::{second['recurrence_id']}"
    assert _captured(backups) == {key: "Standup"}


async def test_a_ranged_update_is_not_snapshotted(
    backups: bm.BackupManager,
    ws: MockHAClientWebSocket,
    core_client: HomeAssistantClient,
) -> None:
    """A range splits the series; a snapshot of one occurrence cannot undo it."""
    _, second, _ = await _daily(ws, core_client)

    await _update(
        core_client,
        start="2026-06-16T10:00:00",
        end="2026-06-16T10:15:00",
        uid=second["uid"],
        recurrence_id=second["recurrence_id"],
        recurrence_range="THISANDFUTURE",
    )

    assert _captured(backups) == {}


async def test_an_update_adding_a_rule_is_not_snapshotted(
    backups: bm.BackupManager,
    ws: MockHAClientWebSocket,
    core_client: HomeAssistantClient,
) -> None:
    """Core has no way to clear a rule again, so no snapshot could undo it."""
    event = await _one_off(ws, core_client)

    await _update(core_client, uid=event["uid"], rrule="FREQ=DAILY;COUNT=3")

    assert _captured(backups) == {}
