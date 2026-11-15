"""``ha_get_system_health`` reads what the System information dialog and the
Integration startup dialog read.

The labels, names and setup times are Core's own: a real ``hass`` with
Core's frontend serves them. Core records setup times only while it starts.
Core's own ``supervisor/api`` answers the Supervisor reads, in front of a
Supervisor that ``aioclient_mock`` answers (``_supervisor_core``).
"""

from pathlib import Path

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import mock_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools

from ._supervisor_core import SUPERVISOR, load_supervisor_api


async def _set_up_frontend(hass: HomeAssistant, tmp_path: Path) -> None:
    """Core's system_health and frontend. Lovelace, which the frontend sets up,
    reports health info that Core labels. The development-repo path keeps the
    frontend off its package files."""
    assert await async_setup_component(hass, "system_health", {})
    config = {"frontend": {"development_repo": str(tmp_path)}}
    assert await async_setup_component(hass, "frontend", config)


async def test_health_keys_carry_the_labels_the_dialog_shows(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_frontend(hass, tmp_path)

    result = await SystemTools(core_client).ha_get_system_health()

    lovelace = result["health_info"]["lovelace"]
    assert lovelace["name"] == "Dashboards"
    assert lovelace["labels"]["mode"] == "Mode"
    assert set(lovelace["labels"]) <= set(lovelace["info"])
    assert "warnings" not in result


async def test_unreadable_labels_leave_raw_keys_and_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, "lovelace", {})

    result = await SystemTools(core_client).ha_get_system_health()

    assert "labels" not in result["health_info"]["lovelace"]
    assert any("labels could not be read" in w for w in result["warnings"])


async def test_integration_startup_lists_setup_times_slowest_first(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    hass.set_state(CoreState.starting)
    await _set_up_frontend(hass, tmp_path)
    hass.set_state(CoreState.running)

    result = await SystemTools(core_client).ha_get_system_health(
        include="integration_startup"
    )

    startup = result["integration_startup"]
    seconds = [setup["seconds"] for setup in startup["integrations"]]
    assert startup["count"] == len(seconds) > 0
    assert seconds == sorted(seconds, reverse=True)
    names = {s["domain"]: s.get("name") for s in startup["integrations"]}
    assert names["frontend"] == "Home Assistant frontend"


async def test_supervisor_is_reported_absent_without_hassio(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_frontend(hass, tmp_path)

    result = await SystemTools(core_client).ha_get_system_health(include="supervisor")

    assert result["supervisor"] == {
        "error": "Supervisor is not available on this installation"
    }


async def test_supervisor_reads_resolution_and_both_container_stats(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    aioclient_mock: AiohttpClientMocker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _set_up_frontend(hass, tmp_path)
    mock_component(hass, "hassio")
    # ``load_supervisor_api`` sets ``SUPERVISOR_TOKEN``, which alone makes the
    # server call the Supervisor directly; embedded mode keeps it on Core.
    monkeypatch.setenv("HA_MCP_EMBEDDED", "1")
    load_supervisor_api(
        hass,
        aioclient_mock,
        monkeypatch,
        {
            ("get", "/resolution/info"): {"unhealthy": ["docker"]},
            ("get", "/core/stats"): {"cpu_percent": 3.5},
        },
    )
    aioclient_mock.get(f"http://{SUPERVISOR}/supervisor/stats", status=500)

    result = await SystemTools(core_client).ha_get_system_health(include="supervisor")

    section = result["supervisor"]
    assert section["resolution"] == {"unhealthy": ["docker"]}
    assert section["core_stats"] == {"cpu_percent": 3.5}
    assert "Not available" in section["supervisor_stats"]["error"]
