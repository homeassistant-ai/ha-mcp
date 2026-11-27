"""The default logbook view preserves native diagnostic fields."""

from __future__ import annotations

from typing import Any

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp.client.rest_client import HomeAssistantClient
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


async def test_compact_logbook_rows_preserve_core_fields(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "logbook", {})
    attributes = {"friendly_name": "Living Room Light", "icon": "mdi:lightbulb"}
    hass.states.async_set("light.living_room", "off", attributes)
    await hass.async_block_till_done()
    hass.states.async_set("light.living_room", "on", attributes)
    await async_wait_recording_done(hass)

    full = (await _logbook(core_client, compact=False))["entries"]
    compact = (await _logbook(core_client, compact=True))["entries"]

    assert full, "Core recorded no logbook rows"
    assert compact == full
    turned_on = [row for row in compact if row.get("entity_id") == "light.living_room"]
    assert turned_on
    assert turned_on[-1]["state"] == "on"
    assert turned_on[-1]["when"]


async def test_an_empty_logbook_compacts_to_no_entries(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "logbook", {})
    await async_wait_recording_done(hass)

    assert (await _logbook(core_client, compact=True))["entries"] == []
