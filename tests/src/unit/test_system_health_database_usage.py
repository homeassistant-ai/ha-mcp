"""``ha_get_system_health(include="database_usage")`` reads Core's
``recorder/database_usage``, the report behind the Storage card (2026.12).

Core's own recorder writes the rows and analyzes its database.
"""

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.components.recorder.common import (
    async_wait_recording_done,
)

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.system_health_sections import DATABASE_USAGE_LIMIT
from ha_mcp.tools.tools_system import SystemTools

HAS_DATABASE_USAGE = (MAJOR_VERSION, MINOR_VERSION) >= (2026, 12)


async def test_database_usage_is_absent_on_a_core_without_it(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    if HAS_DATABASE_USAGE:
        pytest.skip("Core added recorder/database_usage in 2026.12")
    assert await async_setup_component(hass, "system_health", {})

    result = await SystemTools(core_client).ha_get_system_health(
        include="database_usage"
    )

    assert "database_usage" not in result
    assert any("no database usage report" in w for w in result["warnings"])


async def test_database_usage_says_when_core_has_not_analyzed_yet(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    if not HAS_DATABASE_USAGE:
        pytest.skip("Core added recorder/database_usage in 2026.12")
    assert await async_setup_component(hass, "system_health", {})

    result = await SystemTools(core_client).ha_get_system_health(
        include="database_usage"
    )

    assert result["database_usage"] is None
    assert any("refresh_database_usage=True" in w for w in result["warnings"])


async def test_refreshed_database_usage_lists_the_largest_entities_first(
    recorder_mock: Recorder, hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """More entities than the cap: the busiest one must survive the cut, and
    the full count must say how many Core reported."""
    if not HAS_DATABASE_USAGE:
        pytest.skip("Core added recorder/database_usage in 2026.12")
    assert await async_setup_component(hass, "system_health", {})
    for index in range(DATABASE_USAGE_LIMIT + 5):
        hass.states.async_set(f"sensor.quiet_{index}", "1")
    for value in range(5):
        hass.states.async_set("sensor.busy", str(value))
    await async_wait_recording_done(hass)

    result = await SystemTools(core_client).ha_get_system_health(
        include="database_usage", refresh_database_usage=True
    )

    usage = result["database_usage"]
    assert usage["total_bytes"] > 0
    assert usage["entities"][0]["entity_id"] == "sensor.busy"
    assert len(usage["entities"]) <= DATABASE_USAGE_LIMIT < usage["entity_count"]
    assert usage["truncated"] is True
    states = [row["states"] for row in usage["entities"]]
    assert states == sorted(states, reverse=True)
