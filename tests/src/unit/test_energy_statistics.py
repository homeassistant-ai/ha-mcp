"""Energy inspection must expose recorder metadata without changing preferences."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.recorder.common import async_wait_recording_done

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_energy import EnergyTools

from ._client_spies import sent_messages


async def test_energy_inspection_resolves_all_referenced_statistics_without_writes(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    assert await async_setup_component(hass, "energy", {})
    saved = await core_client.send_websocket_message(
        {
            "type": "energy/save_prefs",
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.solar",
                    "config_entry_solar_forecast": None,
                }
            ],
            "device_consumption": [
                {
                    "stat_consumption": "sensor.missing",
                    "included_in_stat": "sensor.solar",
                }
            ],
            "device_consumption_water": [],
        }
    )
    assert saved["success"] is True
    prefs = saved["result"]

    hass.states.async_set(
        "sensor.solar", "1", {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    async_import_statistics(
        hass,
        {
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "name": None,
            "source": "recorder",
            "statistic_id": "sensor.solar",
            "unit_class": "energy",
            "unit_of_measurement": "MWh",
        },
        [
            {
                "start": datetime(2026, 1, 1, tzinfo=UTC),
                "state": 1,
                "sum": 1,
            }
        ],
    )
    await async_wait_recording_done(hass)

    tool = EnergyTools(core_client)
    ordinary = await tool.ha_manage_energy_prefs(mode="get")
    inspected = await tool.ha_manage_energy_prefs(mode="get", include_statistics=True)
    assert inspected["config"] == ordinary["config"] == prefs
    assert inspected["config_hash"] == ordinary["config_hash"]
    assert inspected["config_hash_per_key"] == ordinary["config_hash_per_key"]
    records = {row["statistic_id"]: row for row in inspected["statistics_metadata"]}
    assert sorted(records) == ["sensor.missing", "sensor.solar"]
    assert records["sensor.solar"]["unit_of_measurement"] == "kWh"
    assert records["sensor.solar"]["statistics_unit_of_measurement"] == "MWh"
    assert records["sensor.missing"]["unit_reason"] == "statistics_metadata_missing"
    assert inspected["warnings"]
    # Inspection is read-only: Core still holds exactly the saved preferences.
    after = await core_client.send_websocket_message({"type": "energy/get_prefs"})
    assert after["result"] == prefs


async def test_empty_energy_config_does_not_request_all_recorder_metadata(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
) -> None:
    assert await async_setup_component(hass, "energy", {})

    with sent_messages(core_client) as sent:
        result = await EnergyTools(core_client).ha_manage_energy_prefs(
            mode="get", include_statistics=True
        )

    assert result["statistics_metadata"] == []
    # An empty statistic_ids list would make Core's recorder return every
    # statistic's metadata.
    assert "recorder/get_statistics_metadata" not in [
        call.args[0]["type"] for call in sent.await_args_list
    ]
