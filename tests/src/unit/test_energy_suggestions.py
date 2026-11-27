"""The meters Settings > Energy suggests come from the installed frontend's code.

Core's recorder lists the sum statistics of the sensors set up here, Core's
energy integration holds the preferences, and the installed frontend's own
``computeEnergySuggestions`` decides; nothing of the frontend is written here.
Settings > Energy suggests meters from Home Assistant 2026.12; the stable lane
runs Core 2026.11, whose frontend has no suggestions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from homeassistant.components.frontend.storage import async_user_store
from homeassistant.components.recorder import Recorder
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockUser

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_energy import EnergyTools

from ._card_definitions_core import fresh_card_state as fresh_card_state

_SUGGESTS = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 12)
_NEEDS_SUGGESTIONS = pytest.mark.skipif(
    not _SUGGESTS, reason="Settings > Energy suggests meters from Core 2026.12"
)
_ENERGY = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}
_METERS = ("sensor.fridge_energy", "sensor.oven_energy", "sensor.boiler_energy")


async def _meters(
    hass: HomeAssistant, hass_storage: dict[str, Any], *, configured: bool = True
) -> None:
    """Three meters; with ``configured``, saved prefs that hold the fridge."""
    if configured:
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
    for meter in _METERS:
        hass.states.async_set(meter, "1", _ENERGY)
    await hass.async_block_till_done()


def _suggested(result: dict[str, Any]) -> list[str]:
    return [s["statisticId"] for s in result["energy_suggestions"]["suggestions"]]


async def _stored(hass: HomeAssistant, user: MockUser) -> Any:
    """The user's energy frontend data, which Settings > Energy reads."""
    return (await async_user_store(hass, user.id)).data.get("energy")


@pytest.mark.usefixtures("fresh_card_state")
@pytest.mark.parametrize("configured", [True, False], ids=["saved", "never_saved"])
async def test_suggestions_are_the_installed_frontends_own(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    component_core_client: HomeAssistantClient,
    configured: bool,
) -> None:
    """A meter not yet in the dashboard is suggested with the slots the
    frontend offers, also before anything was saved; a frontend without
    suggestions (2026.11) says so."""
    await _meters(hass, hass_storage, configured=configured)

    result = await EnergyTools(component_core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )

    suggestions = result["energy_suggestions"]
    if not _SUGGESTS:
        assert suggestions["status"] == "unavailable"
        assert suggestions["reason"] in result["warnings"]
        return
    by_id = {s["statisticId"]: s for s in suggestions["suggestions"]}
    assert ("sensor.fridge_energy" in by_id) is not configured
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
    """The read still answers with a warning naming the component; a dismiss
    is refused."""
    await _meters(hass, hass_storage)
    tool = EnergyTools(core_client)

    result = await tool.ha_manage_energy_prefs(mode="get", include_suggestions=True)
    with pytest.raises(ToolError, match="COMPONENT_NOT_INSTALLED"):
        await tool.ha_manage_energy_prefs(
            mode="dismiss_suggestions", statistic_ids=["sensor.oven_energy"]
        )

    assert result["config"]["device_consumption"]
    assert result["energy_suggestions"]["status"] == "unavailable"
    assert any("HA-MCP component" in w for w in result["warnings"])


@pytest.mark.usefixtures("fresh_card_state")
async def test_dismissed_meters_stay_hidden_in_settings_until_restored(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
    component_core_client: HomeAssistantClient,
) -> None:
    """Several meters dismissed at once are saved, once each, where Settings >
    Energy reads them, and come back when restored; a frontend without
    suggestions refuses and saves nothing."""
    await _meters(hass, hass_storage)
    tool = EnergyTools(component_core_client)
    both = ["sensor.oven_energy", "sensor.boiler_energy"]

    if not _SUGGESTS:
        with pytest.raises(ToolError):
            await tool.ha_manage_energy_prefs(
                mode="dismiss_suggestions", statistic_ids=both
            )
        assert await _stored(hass, hass_admin_user) is None
        return
    await tool.ha_manage_energy_prefs(mode="dismiss_suggestions", statistic_ids=both)
    stored = await _stored(hass, hass_admin_user)
    hidden = await tool.ha_manage_energy_prefs(mode="get", include_suggestions=True)
    await tool.ha_manage_energy_prefs(mode="restore_suggestions", statistic_ids=both)

    assert sorted(stored["dismissed_suggestions"]) == sorted(both)
    assert not set(both) & set(_suggested(hidden))
    assert await _stored(hass, hass_admin_user) == {"dismissed_suggestions": []}


@_NEEDS_SUGGESTIONS
@pytest.mark.usefixtures("fresh_card_state")
@pytest.mark.parametrize(
    ("mode", "statistic_id"),
    [
        ("dismiss_suggestions", "sensor.oven_enrgy"),
        ("restore_suggestions", "sensor.oven_energy"),
    ],
    ids=["dismiss_unknown", "restore_not_dismissed"],
)
async def test_a_meter_settings_does_not_list_changes_nothing(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
    component_core_client: HomeAssistantClient,
    mode: str,
    statistic_id: str,
) -> None:
    """A dismiss of a meter that is not suggested, or a restore of one that is
    not dismissed, is refused with the id named and nothing saved."""
    await _meters(hass, hass_storage)

    with pytest.raises(ToolError, match="VALIDATION_INVALID_PARAMETER") as refused:
        await EnergyTools(component_core_client).ha_manage_energy_prefs(
            mode=mode, statistic_ids=[statistic_id]
        )

    assert statistic_id in str(refused.value)
    assert await _stored(hass, hass_admin_user) is None


@_NEEDS_SUGGESTIONS
@pytest.mark.usefixtures("fresh_card_state")
async def test_parallel_dismisses_all_stay_dismissed(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
    component_core_client: HomeAssistantClient,
) -> None:
    """Two dismisses sent at once both land; neither overwrites the other."""
    await _meters(hass, hass_storage)
    tool = EnergyTools(component_core_client)

    await asyncio.gather(
        *(
            tool.ha_manage_energy_prefs(
                mode="dismiss_suggestions", statistic_ids=[meter]
            )
            for meter in ("sensor.oven_energy", "sensor.boiler_energy")
        )
    )

    stored = await _stored(hass, hass_admin_user)
    assert sorted(stored["dismissed_suggestions"]) == [
        "sensor.boiler_energy",
        "sensor.oven_energy",
    ]
