"""Compact logbook entries keep only the essential fields (issue #683).

``ha_get_logs(source="logbook")`` reads Core's logbook through the server's own
client on a real ``hass``; compact mode must cut each row Core returns down to
``COMPACT_LOGBOOK_FIELDS`` without inventing or changing a value.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.log_common import COMPACT_LOGBOOK_FIELDS
from ha_mcp.tools.tools_logs import register_logs_tools


async def _logbook(client: HomeAssistantClient, *, compact: bool) -> dict[str, Any]:
    mcp = FastMCP("logbook-compact")
    register_logs_tools(mcp, client)
    async with Client(mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "ha_get_logs", {"source": "logbook", "compact": compact}
        )
    assert isinstance(result.structured_content, dict)
    data: dict[str, Any] = result.structured_content["data"]
    return data


async def test_compact_logbook_rows_keep_only_the_essential_fields_unchanged(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    # The logbook registers a sidebar panel; the real frontend needs the
    # frontend build, which is not installed with Core.
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    attributes = {"friendly_name": "Living Room Light", "icon": "mdi:lightbulb"}
    hass.states.async_set("light.living_room", "off", attributes)
    await hass.async_block_till_done()
    hass.states.async_set("light.living_room", "on", attributes)
    await async_wait_recording_done(hass)

    full = (await _logbook(core_client, compact=False))["entries"]
    compact = (await _logbook(core_client, compact=True))["entries"]

    assert full, "Core recorded no logbook rows"
    # Core's rows carry more than the compact set, so the cut is exercised.
    assert any(set(row) - COMPACT_LOGBOOK_FIELDS for row in full)
    assert compact == [
        {key: value for key, value in row.items() if key in COMPACT_LOGBOOK_FIELDS}
        for row in full
    ]
    # What a debugging agent needs from each row survives the cut.
    turned_on = [row for row in compact if row.get("entity_id") == "light.living_room"]
    assert turned_on
    assert turned_on[-1]["state"] == "on"
    assert turned_on[-1]["when"]


async def test_an_empty_logbook_compacts_to_no_entries(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    mock_integration(hass, MockModule("frontend"))
    assert await async_setup_component(hass, "logbook", {})
    await async_wait_recording_done(hass)

    assert (await _logbook(core_client, compact=True))["entries"] == []
