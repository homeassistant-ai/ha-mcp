"""Core's Local Calendar and Remote Calendar on a real ``hass``.

Local Calendar keeps its ``.ics`` file in Core's config directory, so a test
module using ``local_calendar`` sets ``hass_config_dir = calendar_config_dir``.
Remote Calendar reads its ``.ics`` from a URL that respx serves, as Core's own
tests serve it.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import respx
from homeassistant.components.calendar import CalendarEntity
from homeassistant.components.calendar.const import DATA_COMPONENT
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from pytest_homeassistant.common import MockConfigEntry, get_test_config_dir
from pytest_homeassistant.typing import MockHAClientWebSocket

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_calendar import CalendarTools

from ._component_core import ws_result
from ._server_entry import set_up


@pytest.fixture
def calendar_config_dir(hass_tmp_config_dir: str) -> str:
    """This test's ``tmp_path`` as Core's config directory, with ``.storage``."""
    Path(hass_tmp_config_dir, ".storage").mkdir(exist_ok=True)
    return hass_tmp_config_dir


def calendar_entity(hass: HomeAssistant, entity_id: str) -> CalendarEntity:
    """The calendar entity Core serves as ``entity_id``."""
    entity = hass.data[DATA_COMPONENT].get_entity(entity_id)
    assert entity is not None
    return entity


async def _set_up_calendar(
    hass: HomeAssistant, domain: str, name: str, **data: Any
) -> CalendarEntity:
    entry = MockConfigEntry(domain=domain, data={"calendar_name": name, **data})
    entry.add_to_hass(hass)
    assert await set_up(hass, entry)
    return calendar_entity(hass, f"calendar.{slugify(name)}")


async def local_calendar(hass: HomeAssistant, name: str) -> CalendarEntity:
    """Core's Local Calendar ``name``, set up from its config entry."""
    # Its file must land in the test's own directory, not the shared one.
    assert Path(hass.config.config_dir) != Path(get_test_config_dir())
    return await _set_up_calendar(hass, "local_calendar", name)


async def remote_calendar(hass: HomeAssistant, name: str, ics: str) -> CalendarEntity:
    """Core's Remote Calendar ``name``, read once from a URL serving ``ics``."""
    url = f"https://calendar.example/{slugify(name)}.ics"
    with respx.mock:
        respx.get(url).respond(200, text=ics)
        return await _set_up_calendar(
            hass, "remote_calendar", name, url=url, verify_ssl=True
        )


async def write_event(
    ws: MockHAClientWebSocket,
    entity_id: str,
    summary: str,
    start: dt.datetime,
    uid: str | None = None,
    **fields: Any,
) -> None:
    """Create a 15-minute ``summary`` at local ``start`` through Core's
    calendar commands, or rewrite event ``uid`` so."""
    begin = start.replace(tzinfo=dt_util.get_default_time_zone())
    event = {
        "summary": summary,
        "dtstart": begin.isoformat(),
        "dtend": (begin + dt.timedelta(minutes=15)).isoformat(),
        **fields,
    }
    if uid is None:
        await ws_result(ws, "calendar/event/create", entity_id=entity_id, event=event)
    else:
        await ws_result(
            ws, "calendar/event/update", entity_id=entity_id, uid=uid, event=event
        )


async def listed_events(
    client: HomeAssistantClient, entity_id: str
) -> list[dict[str, Any]]:
    """Every event on ``entity_id`` as Core's calendar view lists it."""
    listed = await CalendarTools(client).ha_config_get_calendar_events(
        entity_id, start="1999-01-01T00:00:00Z", end="2100-01-01T00:00:00Z"
    )
    assert listed["total_available"] == listed["count"], (
        f"{entity_id} has more events than the tool returns"
    )
    events: list[dict[str, Any]] = listed["events"]
    return events
