"""The component's ``ha_mcp_tools/logbook_events`` command on a real ``hass``.

The command runs Core's own ``EventProcessor`` with the REST view's flags
(ISO ``when``, entity names) on Core's recorder and mirrors Core's
``logbook/get_events`` validation. The rows are Core's, recorded from state
changes by the recorder and logbook integrations.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done
from pytest_homeassistant.typing import MockHAClientWebSocket

from ._component_core import ws_call

_WS = "ha_mcp_tools/logbook_events"


@pytest.fixture
async def logbook(recorder_mock: Recorder, hass: HomeAssistant) -> None:
    """Core's logbook on its recorder, with a lamp that turned on."""
    # The logbook registers a sidebar panel; the real frontend needs the
    # frontend build, which is not installed with Core.
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    attributes = {"friendly_name": "Kitchen Lamp"}
    hass.states.async_set("light.kitchen", "off", attributes)
    await hass.async_block_till_done()
    hass.states.async_set("light.kitchen", "on", attributes)
    hass.states.async_set("light.hall", "on", {"friendly_name": "Hall"})
    await async_wait_recording_done(hass)


def _ago(**delta: float) -> str:
    return (dt_util.utcnow() - timedelta(**delta)).isoformat()


async def _events(ws: MockHAClientWebSocket, **params: Any) -> dict[str, Any]:
    params.setdefault("start_time", _ago(hours=1))
    return await ws_call(ws, _WS, **params)


@pytest.mark.usefixtures("logbook")
async def test_rows_carry_iso_times_and_entity_names_for_the_asked_entity(
    component_ws: MockHAClientWebSocket,
) -> None:
    """The REST view's shape, not the WebSocket one's float timestamps; the end
    defaults to now."""
    reply = await _events(component_ws, entity_ids=["light.kitchen"])

    rows = reply["result"]
    assert rows
    assert {row["entity_id"] for row in rows} == {"light.kitchen"}
    assert all(dt_util.parse_datetime(row["when"]) is not None for row in rows)
    assert rows[-1]["name"] == "Kitchen Lamp"
    assert rows[-1]["state"] == "on"


@pytest.mark.usefixtures("logbook")
@pytest.mark.parametrize("field", ["start_time", "end_time"])
async def test_an_unreadable_time_is_refused_by_name(
    component_ws: MockHAClientWebSocket, field: str
) -> None:
    reply = await _events(component_ws, **{field: "not-a-time"})

    assert reply["success"] is False
    assert f"Invalid {field}" in reply["error"]["message"]


@pytest.mark.usefixtures("logbook")
async def test_a_start_in_the_future_has_no_rows(
    component_ws: MockHAClientWebSocket,
) -> None:
    reply = await _events(component_ws, start_time=_ago(hours=-1))

    assert reply["result"] == []


@pytest.mark.usefixtures("logbook")
async def test_entities_the_logbook_never_shows_have_no_rows(
    hass: HomeAssistant, component_ws: MockHAClientWebSocket
) -> None:
    """A sensor with a unit is continuous: Core's logbook filters it away."""
    hass.states.async_set("sensor.temp", "21", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.temp", "22", {"unit_of_measurement": "°C"})
    await async_wait_recording_done(hass)

    reply = await _events(component_ws, entity_ids=["sensor.temp"])

    assert reply["result"] == []


@pytest.mark.usefixtures("logbook")
async def test_a_context_cannot_be_combined_with_entities(
    component_ws: MockHAClientWebSocket,
) -> None:
    reply = await _events(component_ws, entity_ids=["light.kitchen"], context_id="ctx1")

    assert reply["success"] is False
    assert "context_id cannot be combined" in reply["error"]["message"]


async def test_without_the_logbook_integration_the_read_is_refused(
    component_ws: MockHAClientWebSocket,
) -> None:
    reply = await _events(component_ws)

    assert reply["success"] is False
    assert "logbook integration is not set up" in reply["error"]["message"]
