"""Recurring and one-off event creation in ``ha_config_set_calendar_event``.

The ``calendar.create_event`` REST service schema has no ``rrule`` field;
recurrence is only accepted by the WebSocket command ``calendar/event/create``,
which since issue #1813 goes through the shared pooled client
(``client.send_websocket_message``).

The tool runs on the server's own client against a real ``hass`` with two
calendars: ``calendar.writable`` is Core's Local Calendar, whose create method
is spied on to see each event Core hands it; ``calendar.read_only`` is Core's
Remote Calendar, which supports no event creation, so Core refuses every create
on it. A wrong rule or a too-short event is refused by Core's own schemas.
"""

import datetime as dt
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_calendar import CalendarTools

from ._calendars import (
    calendar_config_dir,
    listed_events,
    local_calendar,
    remote_calendar,
)
from ._tool_errors import error_of

_RRULE_HINT = "Check RRULE syntax"
_EXCLUSIVE_END_HINT = "For an all-day event the end date is exclusive"
_NO_EVENTS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//ha-mcp tests//EN
END:VCALENDAR
"""


# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


@pytest.fixture
async def writable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[MagicMock | AsyncMock]:
    """A spy on ``calendar.writable``'s create; ``calendar.read_only`` beside it."""
    entity = await local_calendar(hass, "Writable")
    await remote_calendar(hass, "Read only", _NO_EVENTS)
    with patch.object(
        entity, "async_create_event", wraps=entity.async_create_event
    ) as create:
        yield create


def _created(create: MagicMock | AsyncMock) -> list[dict[str, Any]]:
    """The event fields of each create Core handed the calendar."""
    return [dict(call.kwargs) for call in create.await_args_list]


def _spy(
    client: HomeAssistantClient, method: str
) -> AbstractContextManager[MagicMock | AsyncMock]:
    """A spy on one client method; Core still answers."""
    return patch.object(client, method, wraps=getattr(client, method))


async def test_rrule_creates_series_over_websocket_not_rest(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """A rule reaches the calendar: the REST service would refuse it."""
    tools = CalendarTools(core_client)
    with (
        _spy(core_client, "send_websocket_message") as ws,
        _spy(core_client, "call_service") as rest,
    ):
        result = await tools.ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="Weekly sync",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            description="desc",
            location="loc",
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        )

    assert ws.await_args_list[0].args[0]["type"] == "calendar/event/create"
    rest.assert_not_awaited()
    [created] = _created(writable)
    assert created["summary"] == "Weekly sync"
    assert created["rrule"] == "FREQ=WEEKLY;BYDAY=MO;COUNT=10"
    assert created["description"] == "desc"
    assert created["location"] == "loc"
    assert created["dtstart"].replace(tzinfo=None) == dt.datetime(2026, 6, 15, 10)
    assert created["dtend"].replace(tzinfo=None) == dt.datetime(2026, 6, 15, 10, 30)
    assert result["success"] is True
    assert result["event"]["rrule"] == "FREQ=WEEKLY;BYDAY=MO;COUNT=10"
    series = await listed_events(core_client, "calendar.writable")
    assert [event["summary"] for event in series] == ["Weekly sync"] * 10


async def test_rrule_series_leaves_unset_description_and_location_out(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """An unset field must not reach the calendar as a value."""
    await CalendarTools(core_client).ha_config_set_calendar_event(
        entity_id="calendar.writable",
        summary="Bare recurring",
        start="2026-06-20T09:00:00",
        end="2026-06-20T09:30:00",
        rrule="FREQ=MONTHLY;BYDAY=3SA",
    )

    [created] = _created(writable)
    assert "description" not in created
    assert "location" not in created
    assert created["rrule"] == "FREQ=MONTHLY;BYDAY=3SA"


@pytest.mark.parametrize("rrule", [None, "FREQ=MONTHLY;BYDAY=3SA"])
async def test_explicit_empty_description_and_location_reach_the_calendar(
    writable: MagicMock | AsyncMock,
    core_client: HomeAssistantClient,
    rrule: str | None,
) -> None:
    """An explicit "" clears the field in HA, unlike an unset one."""
    await CalendarTools(core_client).ha_config_set_calendar_event(
        entity_id="calendar.writable",
        summary="Cleared fields",
        start="2026-06-20T09:00:00",
        end="2026-06-20T09:30:00",
        description="",
        location="",
        rrule=rrule,
    )

    [created] = _created(writable)
    assert created["description"] == ""
    assert created["location"] == ""


async def test_no_rrule_creates_timed_event_through_rest_service(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    tools = CalendarTools(core_client)
    with (
        _spy(core_client, "send_websocket_message") as ws,
        _spy(core_client, "call_service") as rest,
    ):
        result = await tools.ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="One-off",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
        )

    rest.assert_awaited_once()
    assert rest.await_args_list[0].args[:2] == ("calendar", "create_event")
    ws.assert_not_awaited()
    [created] = _created(writable)
    assert created["summary"] == "One-off"
    assert "location" not in created
    assert isinstance(created["dtstart"], dt.datetime)
    assert created["dtstart"].replace(tzinfo=None) == dt.datetime(2026, 6, 15, 10)
    assert created["dtend"].replace(tzinfo=None) == dt.datetime(2026, 6, 15, 11)
    assert result["success"] is True
    assert result["event"]["rrule"] is None


async def test_no_rrule_date_only_values_create_all_day_event(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """Date-only values must reach the calendar as dates, not midnight times."""
    await CalendarTools(core_client).ha_config_set_calendar_event(
        entity_id="calendar.writable",
        summary="School holidays",
        start="2026-07-04",
        end="2026-08-10",
    )

    [created] = _created(writable)
    assert type(created["dtstart"]) is dt.date
    assert created["dtstart"] == dt.date(2026, 7, 4)
    assert created["dtend"] == dt.date(2026, 8, 10)


async def test_mixed_date_and_datetime_values_are_rejected_before_any_call(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """Mixed all-day and timed boundaries are ambiguous and invalid."""
    tools = CalendarTools(core_client)
    with (
        _spy(core_client, "call_service") as rest,
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="Mixed event",
            start="2026-07-04",
            end="2026-07-04T12:00:00",
        )

    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"
    rest.assert_not_awaited()
    assert _created(writable) == []


async def test_rejected_rrule_leads_with_rrule_syntax_hint(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """Core refuses an unknown frequency; the hint must point at the rule."""
    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="Weekly sync",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            rrule="FREQ=FORTNIGHTLY",
        )

    assert error_of(exc_info)["suggestions"][0].startswith(_RRULE_HINT)
    assert _created(writable) == []


async def test_failed_create_without_rrule_gives_no_rrule_hint(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """The RRULE-syntax hint must not appear when no rule was supplied."""
    tools = CalendarTools(core_client)
    with (
        _spy(core_client, "send_websocket_message") as ws,
        pytest.raises(ToolError) as exc_info,
    ):
        await tools.ha_config_set_calendar_event(
            entity_id="calendar.read_only",
            summary="One-off",
            start="2026-06-15T10:00:00",
            end="2026-06-15T11:00:00",
        )

    suggestions = error_of(exc_info)["suggestions"]
    assert not any(s.startswith(_RRULE_HINT) for s in suggestions)
    ws.assert_not_awaited()


async def test_single_day_all_day_event_leads_with_exclusive_end_hint(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """Core refuses end == start for a date range; the end date is exclusive.

    The hint is keyed on the supplied boundaries: the REST service answers a
    bare 400, so Core's "Expected minimum event duration" never reaches us.
    """
    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="Single day off",
            start="2026-07-04",
            end="2026-07-04",
        )

    assert error_of(exc_info)["suggestions"][0].startswith(_EXCLUSIVE_END_HINT)
    assert _created(writable) == []


@pytest.mark.parametrize(
    ("calendar", "start", "end"),
    [
        # A valid all-day range that fails for another reason.
        ("calendar.read_only", "2026-07-04", "2026-07-11"),
        # A timed event hits the same minimum duration but needs a later time.
        ("calendar.writable", "2026-06-15T10:00:00", "2026-06-15T10:00:00"),
    ],
    ids=["valid-all-day-range", "zero-length-timed-event"],
)
async def test_exclusive_end_hint_only_for_empty_all_day_range(
    writable: MagicMock | AsyncMock,
    core_client: HomeAssistantClient,
    calendar: str,
    start: str,
    end: str,
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_set_calendar_event(
            entity_id=calendar,
            summary="Not a single day",
            start=start,
            end=end,
        )

    suggestions = error_of(exc_info)["suggestions"]
    assert not any(s.startswith(_EXCLUSIVE_END_HINT) for s in suggestions)
    assert _created(writable) == []


async def test_rrule_create_with_no_answer_reports_connection_failure(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """A dropped connection gets connectivity guidance, not calendar hints (#1832)."""
    with (
        patch.object(
            core_client,
            "send_websocket_message",
            AsyncMock(side_effect=HomeAssistantConnectionError("Failed to connect")),
        ),
        pytest.raises(ToolError) as exc_info,
    ):
        await CalendarTools(core_client).ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="Weekly sync",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
        )

    err = error_of(exc_info)
    assert err["code"] in ("CONNECTION_FAILED", "CONNECTION_TIMEOUT")
    assert not any("RRULE" in s for s in err.get("suggestions", []))
