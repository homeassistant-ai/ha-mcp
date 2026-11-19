"""Recurring and one-off event creation in ``ha_config_set_calendar_event``.

Both go through the WebSocket command ``calendar/event/create`` on the shared
pooled client (``client.send_websocket_message``): the ``calendar.create_event``
REST service schema has no ``rrule`` field and answers a refusal with a bare
status, while the command returns Core's own reason.

The tool runs on the server's own client against a real ``hass`` with two
calendars: ``calendar.writable`` is Core's Local Calendar, whose create method
is spied on to see each event Core hands it; ``calendar.read_only`` is Core's
Remote Calendar, which supports no event creation, so Core refuses every create
on it. A wrong rule, a too-short event or a mix of a date and a time is
refused by Core's own schema, and the refusal carries Core's text; one such
refusal stands for all of them.
"""

import datetime as dt
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.tools.tools_calendar import CalendarTools

from ._calendars import (
    NO_EVENTS,
    calendar_config_dir,
    listed_events,
    local_calendar,
    remote_calendar,
)
from ._tool_errors import error_body, error_of

# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


@pytest.fixture
async def writable(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> AsyncIterator[MagicMock | AsyncMock]:
    """A spy on ``calendar.writable``'s create; ``calendar.read_only`` beside it."""
    entity = await local_calendar(hass, "Writable")
    await remote_calendar(hass, "Read only", NO_EVENTS)
    with patch.object(
        entity, "async_create_event", wraps=entity.async_create_event
    ) as create:
        yield create


def _created(create: MagicMock | AsyncMock) -> list[dict[str, Any]]:
    """The event fields of each create Core handed the calendar."""
    return [dict(call.kwargs) for call in create.await_args_list]


async def test_a_rule_reaches_the_calendar_as_a_series(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """The calendar gets the rule and Core lists every occurrence."""
    result = await CalendarTools(core_client).ha_config_set_calendar_event(
        entity_id="calendar.writable",
        summary="Weekly sync",
        start="2026-06-15T10:00:00",
        end="2026-06-15T10:30:00",
        description="desc",
        location="loc",
        rrule="FREQ=WEEKLY;BYDAY=MO;COUNT=10",
    )

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


async def test_no_rrule_creates_a_timed_event(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    result = await CalendarTools(core_client).ha_config_set_calendar_event(
        entity_id="calendar.writable",
        summary="One-off",
        start="2026-06-15T10:00:00",
        end="2026-06-15T11:00:00",
    )

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


async def _refused_create(
    core_client: HomeAssistantClient, **fields: Any
) -> dict[str, Any]:
    """The body of a create the tool reports as refused."""
    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_set_calendar_event(
            **{"entity_id": "calendar.writable", "summary": "Refused", **fields}
        )
    return error_body(exc_info)


async def test_a_calendar_without_creates_says_so(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """A one-off create on a read-only calendar gets Core's reason, not a
    bare status the agent has to guess at."""
    body = await _refused_create(
        core_client,
        entity_id="calendar.read_only",
        start="2026-06-15T10:00:00",
        end="2026-06-15T11:00:00",
    )

    assert "Calendar does not support event creation" in body["error"]["message"]
    assert "integration does not offer" in body["error"]["suggestion"]


async def test_a_single_day_all_day_event_ending_on_its_start_gets_cores_reason(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """The all-day end date is exclusive, so end == start is an empty event."""
    body = await _refused_create(core_client, start="2026-07-04", end="2026-07-04")

    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "Expected minimum event duration" in body["error"]["message"]
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
    assert not any("calendar" in s.lower() for s in err.get("suggestions", []))


async def test_an_empty_rule_is_refused_not_dropped(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """An empty rule reaches Core, which refuses it; dropping it would create a
    one-off event the caller did not ask for."""
    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="No rule",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
            rrule="",
        )

    assert "rrule" in error_of(exc_info)["message"]
    assert _created(writable) == []


async def test_a_create_the_calendar_fails_is_not_blamed_on_a_uid(
    writable: MagicMock | AsyncMock, core_client: HomeAssistantClient
) -> None:
    """Core reports the integration's failure as ``failed``; a create has no
    uid to check, so the event-lookup hint stays out."""
    writable.side_effect = HomeAssistantError("the calendar store is read-only")

    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_set_calendar_event(
            entity_id="calendar.writable",
            summary="Blocked",
            start="2026-06-15T10:00:00",
            end="2026-06-15T10:30:00",
        )

    body = error_body(exc_info)
    assert body["ha_error_code"] == "failed"
    assert "the calendar store is read-only" in body["error"]["message"]
    assert "ha_config_get_calendar_events" not in str(body)
