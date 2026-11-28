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
from homeassistant.components.frontend.storage import UserStore, async_user_store
from homeassistant.components.recorder import Recorder
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockUser

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.component_api import COMPONENT_VIA_MARKETPLACE
from ha_mcp.tools.tools_energy import EnergyTools

from ._card_definitions_core import cd
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


def _dismissed_in_settings(
    hass_storage: dict[str, Any], user: MockUser, dismissed: Any
) -> None:
    """The user's energy frontend data as Settings > Energy saves a dismissal."""
    hass_storage[f"frontend.user_data_{user.id}"] = {
        "version": 1,
        "key": f"frontend.user_data_{user.id}",
        "data": {"energy": {"dismissed_suggestions": dismissed}},
    }


@pytest.mark.usefixtures("fresh_card_state")
@pytest.mark.parametrize("configured", [True, False], ids=["saved", "never_saved"])
async def test_suggestions_are_the_installed_frontends_own(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
    component_core_client: HomeAssistantClient,
    configured: bool,
) -> None:
    """A meter not yet in the dashboard is suggested with the slots the
    frontend offers, also before anything was saved, and the read names whose
    dismissals it reflects; a frontend without suggestions (2026.11) says so."""
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
    assert suggestions["dismissed_by"] == hass_admin_user.name


async def test_suggestions_without_the_component_say_it_is_needed(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """The read still answers, with a warning naming the component."""
    await _meters(hass, hass_storage)

    result = await EnergyTools(core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )

    assert result["config"]["device_consumption"]
    assert result["energy_suggestions"]["status"] == "unavailable"
    assert any(COMPONENT_VIA_MARKETPLACE in w for w in result["warnings"])


@_NEEDS_SUGGESTIONS
@pytest.mark.usefixtures("fresh_card_state")
@pytest.mark.parametrize(
    ("dismissed", "listed"),
    [
        (["sensor.oven_energy"], ["sensor.oven_energy"]),
        (["sensor.fridge_energy"], []),
        (None, []),
    ],
    ids=["dismissed", "dismissed_then_configured", "null"],
)
async def test_meters_dismissed_in_settings_are_listed_apart_and_kept(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
    component_core_client: HomeAssistantClient,
    dismissed: Any,
    listed: list[str],
) -> None:
    """A meter dismissed in Settings > Energy is listed as dismissed, not
    suggested; one dismissed and since added to the dashboard is listed
    nowhere, and a null list reads as empty, as the panel shows them. The read
    leaves the stored list as it is."""
    _dismissed_in_settings(hass_storage, hass_admin_user, dismissed)
    await _meters(hass, hass_storage)

    result = await EnergyTools(component_core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )

    assert result["energy_suggestions"]["dismissed"] == listed
    assert not set(listed) & set(_suggested(result))
    stored = (await async_user_store(hass, hass_admin_user.id)).data
    assert stored == {"energy": {"dismissed_suggestions": dismissed}}


@_NEEDS_SUGGESTIONS
@pytest.mark.usefixtures("fresh_card_state")
async def test_a_power_sensor_built_from_a_meter_does_not_hide_it(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    component_core_client: HomeAssistantClient,
) -> None:
    """A configured device power sensor whose state names a meter as its
    source (a derivative helper) leaves that meter suggested, as Settings >
    Energy shows it: the panel hands the frontend only its meters' states."""
    hass_storage["energy"] = {
        "version": 1,
        "minor_version": 3,
        "key": "energy",
        "data": {
            "energy_sources": [],
            "device_consumption": [
                {
                    "stat_consumption": "sensor.fridge_energy",
                    "stat_rate": "sensor.oven_power",
                }
            ],
            "device_consumption_water": [],
        },
    }
    await _meters(hass, hass_storage, configured=False)
    hass.states.async_set(
        "sensor.oven_power",
        "1",
        {
            "device_class": "power",
            "unit_of_measurement": "kW",
            "source": "sensor.oven_energy",
        },
    )

    result = await EnergyTools(component_core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )

    assert "sensor.oven_energy" in _suggested(result)


@pytest.mark.usefixtures("fresh_card_state")
async def test_a_timed_out_read_leaves_the_users_frontend_data_loading(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suggestions read that times out while the browser is loading the same
    user's frontend data stops waiting without cancelling that load: the
    browser's load finishes and later reads of the user's data work."""
    await _meters(hass, hass_storage)
    assert await cd.async_get_definitions(hass) is not None
    release = asyncio.Event()
    load = UserStore.async_load

    async def held_load(store: UserStore) -> None:
        await release.wait()
        await load(store)

    monkeypatch.setattr(UserStore, "async_load", held_load)
    monkeypatch.setattr(cd, "CARD_WORK_SECONDS", 0.5)
    browser = hass.async_create_task(async_user_store(hass, hass_admin_user.id))

    result = await EnergyTools(component_core_client).ha_manage_energy_prefs(
        mode="get", include_suggestions=True
    )
    release.set()

    assert result["energy_suggestions"]["status"] == "unavailable"
    assert "Timeout" in result["energy_suggestions"]["reason"]
    assert (await browser).data == {}
    assert (await async_user_store(hass, hass_admin_user.id)).data == {}
