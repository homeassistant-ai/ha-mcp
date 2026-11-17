"""Reading events with ``ha_config_get_calendar_events``.

The tool reads Core's calendar view (``GET /api/calendars/<entity_id>``), which
keeps the uid and recurrence_id the edit and remove tools need. The view reads
a time without an offset in Home Assistant's time zone, and answers an
unknown calendar entity or a bad time range with a bare 400. The tool runs on
the server's own client against a real ``hass`` whose ``calendar.test`` is
Core's Local Calendar.
"""

import datetime as dt
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import tools_calendar
from ha_mcp.tools.tools_calendar import CalendarTools

from ._calendars import calendar_config_dir, local_calendar, write_event
from ._module_clock import set_module_clock
from ._tool_errors import error_of

# Core's config directory is this test's ``tmp_path``.
hass_config_dir = calendar_config_dir


async def test_the_default_window_is_the_next_seven_days_on_home_assistants_clock(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window must not move by the offset between this server's time zone
    and Home Assistant's: an event an hour away and one an hour before the
    window closes are both in it."""
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
    the default must sit there too, not on this server's or UTC's."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    local_now = dt_util.as_local(now).replace(tzinfo=None)
    await write_event(ws, "calendar.test", "Soon", local_now + dt.timedelta(hours=1))
    await write_event(
        ws, "calendar.test", "Beyond", local_now + dt.timedelta(days=7, hours=1)
    )
    bound = {
        "start": local_now,
        "end": local_now + dt.timedelta(hours=2),
    }[given]

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test", **{given: bound.isoformat()}
    )

    assert [event["summary"] for event in listed["events"]] == ["Soon"]


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


async def test_a_default_beside_a_bound_without_an_offset_needs_the_time_zone(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without Home Assistant's time zone the default would sit on UTC's
    clock, a window shifted by Home Assistant's offset from UTC."""
    await local_calendar(hass, "Test")
    monkeypatch.setattr(
        core_client,
        "get_config",
        AsyncMock(side_effect=HomeAssistantConnectionError("network down")),
    )

    with pytest.raises(ToolError) as exc_info:
        await CalendarTools(core_client).ha_config_get_calendar_events(
            "calendar.test", start="2026-06-12T08:00:00"
        )

    assert error_of(exc_info)["code"] == "CONNECTION_FAILED"


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
