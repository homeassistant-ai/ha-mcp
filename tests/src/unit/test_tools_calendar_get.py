"""Reading events with ``ha_config_get_calendar_events``.

The tool reads Core's calendar view (``GET /api/calendars/<entity_id>``), which
keeps the uid and recurrence_id the edit and remove tools need. The view reads
a time without an offset in Home Assistant's time zone, and answers an
unknown calendar entity or a bad time range with a bare 400. The tool runs on the server's own client against a
real ``hass`` whose ``calendar.test`` is Core's Local Calendar.
"""

import datetime as dt

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant.typing import WebSocketGenerator

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
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


async def test_a_start_without_an_offset_pairs_with_the_default_end(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    hass_ws_client: WebSocketGenerator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core cannot compare a time without an offset against one with an
    offset, so the default end must take the form the documented examples
    give start in."""
    now = dt.datetime(2026, 6, 12, 12, tzinfo=dt.UTC)
    set_module_clock(monkeypatch, tools_calendar, now)
    await local_calendar(hass, "Test")
    ws = await hass_ws_client(hass)
    start = dt_util.as_local(now).replace(tzinfo=None)
    await write_event(ws, "calendar.test", "Soon", start + dt.timedelta(hours=1))

    listed = await CalendarTools(core_client).ha_config_get_calendar_events(
        "calendar.test", start=start.isoformat()
    )

    assert [event["summary"] for event in listed["events"]] == ["Soon"]


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
