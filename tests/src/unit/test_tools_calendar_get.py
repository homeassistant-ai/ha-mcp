"""Reading events with ``ha_config_get_calendar_events``.

The tool reads Core's calendar view (``GET /api/calendars/<entity_id>``), which
keeps the uid and recurrence_id the edit and remove tools need. The view reads
a time without an offset in Home Assistant's time zone, and answers an
unknown calendar entity or a bad time range with a bare 400. The tool runs on
the server's own client against a real ``hass`` whose ``calendar.test`` is
Core's Local Calendar.
"""

import datetime as dt
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import tools_calendar
from ha_mcp.tools.tools_calendar import CalendarTools

from ._calendars import calendar_config_dir, local_calendar, write_event
from ._ha_timezone import unresolvable_zone
from ._module_clock import set_module_clock
from ._tool_errors import error_of

# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


async def test_the_default_window_is_the_next_seven_days(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window runs from now to seven days later: an event an hour away
    and one an hour before the window closes are both in it."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    local_now = dt_util.as_local(now).replace(tzinfo=None)
    soon = local_now + dt.timedelta(hours=1)
    late = local_now + dt.timedelta(days=7, hours=-1)
    await write_event(ws, "calendar.test", "Soon", soon)
    await write_event(ws, "calendar.test", "Late", late)

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test"
    )

    assert [event["summary"] for event in listed["events"]] == ["Soon", "Late"]


@pytest.mark.parametrize("given", ["start", "end"])
async def test_a_bound_without_an_offset_pairs_with_a_default_on_home_assistants_clock(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    given: str,
) -> None:
    """A bound without an offset, as the tool's example gives it, must not
    fail against the default of the other: Core cannot compare it with a
    time that has an offset. Core reads both on Home Assistant's clock, so
    the default must sit there too, not on this server's or UTC's. The
    given bound itself is kept."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    local_now = dt_util.as_local(now).replace(tzinfo=None)
    for summary, delta in [
        ("Past", dt.timedelta(hours=-1)),
        ("Now", dt.timedelta()),
        ("Soon", dt.timedelta(hours=1)),
        ("Later", dt.timedelta(hours=3)),
        ("Beyond", dt.timedelta(days=7, hours=1)),
    ]:
        await write_event(ws, "calendar.test", summary, local_now + delta)
    bound, expected = {
        "start": (local_now + dt.timedelta(minutes=30), ["Soon", "Later"]),
        "end": (local_now + dt.timedelta(hours=2), ["Now", "Soon"]),
    }[given]

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test", **{given: bound.isoformat()}
    )

    assert [event["summary"] for event in listed["events"]] == expected


async def test_a_start_python_cannot_read_pairs_like_one_without_an_offset(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core also reads a single-digit month, which Python's parser refuses;
    without an offset, the default end beside it must have none either."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    local_now = dt_util.as_local(now).replace(tzinfo=None)
    await write_event(ws, "calendar.test", "Soon", local_now + dt.timedelta(hours=1))
    start = f"{local_now:%Y}-{local_now.month}-{local_now:%dT%H:%M:%S}"

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test", start=start
    )

    assert [event["summary"] for event in listed["events"]] == ["Soon"]


@pytest.mark.parametrize("given", ["start", "end"])
async def test_a_bound_with_an_offset_pairs_with_the_default_of_the_other(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    given: str,
) -> None:
    """Core cannot compare a time with an offset against one without, so
    beside an ISO 8601 bound with an offset the omitted bound must carry
    one too."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    local_now = dt_util.as_local(now).replace(tzinfo=None)
    await write_event(ws, "calendar.test", "Soon", local_now + dt.timedelta(hours=1))
    bound = {
        "start": now - dt.timedelta(hours=1),
        "end": now + dt.timedelta(days=1),
    }[given]

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test", **{given: bound.isoformat()}
    )

    assert [event["summary"] for event in listed["events"]] == ["Soon"]


@pytest.mark.parametrize(
    ("bounds", "failure", "code"),
    [
        ({}, "unreachable", "CONNECTION_FAILED"),
        ({"start": "2026-06-12T08:00:00"}, "unreachable", "CONNECTION_FAILED"),
        ({"start": "2026-06-12T08:00:00+00:00"}, "unreachable", "CONNECTION_FAILED"),
        ({}, "unresolvable", "VALIDATION_INVALID_PARAMETER"),
    ],
    ids=["none", "start_without_offset", "start_with_offset", "zone_unresolvable"],
)
async def test_a_default_bound_needs_the_time_zone(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    bounds: dict[str, str],
    failure: str,
    code: str,
) -> None:
    """A default end counts seven days on Home Assistant's calendar, and a
    default beside a bound without an offset sits on its clock; without its
    time zone, unreachable or unknown to this server, they would be counted
    in UTC, so the read is refused."""
    set_module_clock(
        monkeypatch, tools_calendar, dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    )
    await local_calendar(hass, "Test")
    if failure == "unreachable":
        monkeypatch.setattr(
            core_client,
            "get_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("network down")),
        )
    else:
        await unresolvable_zone(hass, monkeypatch)

    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_get_calendar_events(
            "calendar.test", **bounds
        )

    assert error_of(exc_info)["code"] == code


@pytest.mark.parametrize(
    "start_given", [False, True], ids=["no_bound", "start_with_offset"]
)
async def test_the_default_end_is_seven_days_on_home_assistants_calendar(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
    start_given: bool,
) -> None:
    """Across a DST change seven days on Home Assistant's calendar are an
    hour more or less than 168 hours; the default end must keep the local
    hour, so an event just before it stays in the window and one just after
    it stays out."""
    now = dt.datetime(2026, 10, 30, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    local_now = dt_util.as_local(now)
    local_end = (local_now.replace(tzinfo=None) + dt.timedelta(days=7)).replace(
        tzinfo=local_now.tzinfo
    )
    hours = local_end.astimezone(dt.UTC) - local_now.astimezone(dt.UTC)
    assert hours > dt.timedelta(days=7), "no fall-back DST change"
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    wall_end = local_end.replace(tzinfo=None)
    for summary, minutes in [("Edge", -30), ("Late", 30)]:
        await write_event(
            ws, "calendar.test", summary, wall_end + dt.timedelta(minutes=minutes)
        )
    bounds = {"start": (now - dt.timedelta(hours=1)).isoformat()} if start_given else {}

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test", **bounds
    )

    assert [event["summary"] for event in listed["events"]] == ["Edge"]


async def test_the_default_start_is_now_also_in_a_repeated_hour(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In the second pass of the hour a fall-back DST change repeats, a time
    without an offset names the first pass; Core's view must still hand the
    calendar now as the start.

    Local Calendar compares the events it creates by wall time in Home
    Assistant's zone, where both passes look alike, so the start is read where
    the view hands it on.
    """
    now = dt.datetime(2026, 11, 1, 9, 30, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    first_pass = dt_util.as_local(now - dt.timedelta(hours=1))
    assert first_pass.replace(tzinfo=None) == dt_util.as_local(now).replace(
        tzinfo=None
    ), "no repeated hour"
    entity = await local_calendar(hass, "Test")

    with patch.object(
        entity, "async_get_events", wraps=entity.async_get_events
    ) as reads:
        await CalendarTools(core_client).ha_config_get_calendar_events("calendar.test")

    assert [dt_util.as_utc(call.args[1]) for call in reads.await_args_list] == [now]


async def test_a_read_core_refuses_names_what_its_bare_400_can_mean(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core's 400 carries no reason; the leading hint lists the ones it has."""
    await local_calendar(hass, "Test")

    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_get_calendar_events(
            "calendar.missing"
        )

    hint = error_of(exc_info)["suggestions"][0]
    assert "'calendar.missing' is not a calendar entity" in hint
