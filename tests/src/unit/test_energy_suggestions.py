"""The meters Settings > Energy suggests come from the installed frontend's code.

Core's recorder lists the sum statistics of the sensors set up here, Core's
energy integration holds the preferences, and the installed frontend's own
``computeEnergySuggestions`` decides; nothing of the frontend is written here.
"""

from __future__ import annotations

from typing import Any

import hass_frontend
import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_energy import EnergyTools

from ._card_definitions_core import fresh_card_state as fresh_card_state

_ENERGY = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}


def _frontend_suggests_meters() -> bool:
    """Settings > Energy suggests meters from Home Assistant 2026.12."""
    return any(
        'tab:"electricity"' in path.read_text(encoding="utf-8")
        for path in (hass_frontend.where() / "frontend_latest").glob("*.js")
    )


async def _meters(hass: HomeAssistant, hass_storage: dict[str, Any]) -> None:
    """A configured fridge meter, an unconfigured oven meter, saved prefs."""
    hass_storage["energy"] = {
        "version": 1,
        "minor_version": 3,
        "key": "energy",
        "data": {
            "energy_sources": [],
            "device_consumption": [{"stat_consumption": "sensor.fridge_energy"}],
            "device_consumption_water": [],
        },
    }
    assert await async_setup_component(hass, "sensor", {})
    assert await async_setup_component(hass, "energy", {})
    hass.states.async_set("sensor.fridge_energy", "1", _ENERGY)
    hass.states.async_set("sensor.oven_energy", "1", _ENERGY)
    await hass.async_block_till_done()


@pytest.mark.usefixtures("fresh_card_state")
async def test_suggestions_are_the_installed_frontends_own(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    component_core_client: HomeAssistantClient,
) -> None:
    """A meter not yet in the dashboard is suggested with the slots the
    frontend offers; a frontend without suggestions (2026.11) says so."""
    await _meters(hass, hass_storage)

    result = await EnergyTools(component_core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )

    suggestions = result["energy_suggestions"]
    if not _frontend_suggests_meters():
        assert suggestions["status"] == "unavailable"
        assert suggestions["reason"] in result["warnings"]
        return
    assert suggestions["status"] == "available"
    by_id = {s["statisticId"]: s for s in suggestions["suggestions"]}
    assert "sensor.fridge_energy" not in by_id
    assert by_id["sensor.oven_energy"]["tab"] == "electricity"
    assert "device" in by_id["sensor.oven_energy"]["slots"]
    assert suggestions["editors"]["device"]["stat"] == "stat_consumption"
    assert suggestions["dismissed"] == []


async def test_suggestions_without_the_component_say_it_is_needed(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """The read itself still answers; the warning names the component."""
    await _meters(hass, hass_storage)

    result = await EnergyTools(core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )

    assert result["config"]["device_consumption"]
    assert result["energy_suggestions"]["status"] == "unavailable"
    assert any("HA-MCP component" in w for w in result["warnings"])


@pytest.mark.usefixtures("fresh_card_state")
async def test_a_dismissed_meter_stays_hidden_until_restored(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    component_core_client: HomeAssistantClient,
) -> None:
    """Dismiss and restore go through the frontend's own steps and are saved
    where Settings > Energy reads them; a frontend without suggestions refuses
    and saves nothing."""
    await _meters(hass, hass_storage)
    tool = EnergyTools(component_core_client)

    if not _frontend_suggests_meters():
        with pytest.raises(ToolError, match="suggests no energy meters"):
            await tool.ha_manage_energy_prefs(
                mode="dismiss_suggestions", statistic_ids=["sensor.oven_energy"]
            )
        return
    dismissed = await tool.ha_manage_energy_prefs(
        mode="dismiss_suggestions", statistic_ids=["sensor.oven_energy"]
    )
    read = await tool.ha_manage_energy_prefs(mode="get", include_suggestions=True)
    restored = await tool.ha_manage_energy_prefs(
        mode="restore_suggestions", statistic_ids=["sensor.oven_energy"]
    )

    assert dismissed["energy_suggestions"]["dismissed"] == ["sensor.oven_energy"]
    ids = [s["statisticId"] for s in read["energy_suggestions"]["suggestions"]]
    assert "sensor.oven_energy" not in ids
    assert read["energy_suggestions"]["dismissed"] == ["sensor.oven_energy"]
    assert restored["energy_suggestions"]["dismissed"] == []
    assert "sensor.oven_energy" in [
        s["statisticId"] for s in restored["energy_suggestions"]["suggestions"]
    ]
