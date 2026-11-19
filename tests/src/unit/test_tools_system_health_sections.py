"""Which sections ``ha_get_system_health`` returns, on real Home Assistant Core.

Every section reads Core: the health baseline is Core's ``system_health``,
the WebSocket-backed sections share one health connection, and
``config_check`` is Core's REST check. A section's command is held, dropped
or never answered by re-registering Core's own handler (``_command_faults``,
``_component_frames``, ``_tools_system_core``); Core still answers it.
Every test here also checks that the health connection is closed: one left
open fails the plugin's leftover-task check.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from ha_mcp import update_check
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools.tools_system import SystemTools

from ._command_faults import arrival
from ._component_frames import held
from ._tools_system_core import answered_after, health_socket_dropped_on
from ._zha_core import IEEE, set_up_zha, zigbee_network
from ._update_check_sources import PyPI
from ._update_check_sources import pypi as pypi

OPTIONAL_SECTIONS = (
    "repairs",
    "zha_network",
    "zwave_network",
    "thread_network",
    "matter_network",
    "integration_startup",
    "supervisor",
    "config_check",
    "dead_entities",
)


async def _health(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "system_health", {})


async def _set_up_repairs(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "repairs", {})


async def _set_up_config(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "config", {})


async def _set_up_thread(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "thread", {})


async def _set_up_zha(hass: HomeAssistant) -> None:
    await _set_up_config(hass)
    await set_up_zha(hass, zigbee_network())


# Each WebSocket section, the integration that answers it and the command it
# sends first on the health connection (Z-Wave reads the config entries).
WS_SECTIONS: dict[str, tuple[Callable[[HomeAssistant], Awaitable[None]], str]] = {
    "repairs": (_set_up_repairs, "repairs/list_issues"),
    "zha_network": (_set_up_zha, "zha/devices"),
    "zwave_network": (_set_up_config, "config_entries/get"),
    "thread_network": (_set_up_thread, "thread/list_datasets"),
}


def _code(excinfo: pytest.ExceptionInfo[ToolError]) -> str:
    code: str = json.loads(str(excinfo.value))["error"]["code"]
    return code


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_servers_own_update_status_is_relayed_when_there_is_one(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    pypi: PyPI,
    enabled: bool,
) -> None:
    await _health(hass)
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.setattr(update_check, "get_version", lambda: "7.8.0")
    if enabled:
        monkeypatch.delenv(update_check.DISABLE_ENV, raising=False)
    pypi.publish("ha-mcp", "7.9.0")

    result = await SystemTools(core_client).ha_get_system_health()

    if enabled:
        notice = result["ha_mcp_update"]
        assert (notice["current"], notice["latest"], notice["update_available"]) == (
            "7.8.0", "7.9.0", True
        )
        assert pypi.asked == ["ha-mcp"]
    else:
        assert "ha_mcp_update" not in result
        assert not pypi.asked


async def test_a_bare_call_returns_the_baseline_and_no_section(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _health(hass)

    with patch.object(
        core_client, "check_config", AsyncMock(wraps=core_client.check_config)
    ) as check:
        result = await SystemTools(core_client).ha_get_system_health()

    assert result["success"] is True
    assert "health_info" in result
    assert not set(OPTIONAL_SECTIONS) & set(result)
    check.assert_not_awaited()


async def test_every_requested_section_is_returned(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Sections Core cannot serve here still come back, each with Core's
    answer: no Z-Wave or Matter entry, no Thread integration."""
    await _health(hass)
    await _set_up_repairs(hass)
    await _set_up_config(hass)
    requested = (
        "repairs",
        "zwave_network",
        "thread_network",
        "matter_network",
        "integration_startup",
        "config_check",
    )

    with patch.object(
        core_client, "check_config", AsyncMock(wraps=core_client.check_config)
    ) as check:
        result = await SystemTools(core_client).ha_get_system_health(
            include=",".join(requested)
        )

    assert {s for s in OPTIONAL_SECTIONS if s in result} == set(requested)
    assert result["repairs"] == {"issues": [], "count": 0}
    assert result["zwave_network"]["error"] == "No enabled Z-Wave JS config entry found"
    assert result["thread_network"]["dataset_count"] == 0
    assert result["matter_network"] == {"error": "No enabled Matter config entry found"}
    assert "integrations" in result["integration_startup"]
    assert "result" in result["config_check"]
    check.assert_awaited_once()


async def test_only_the_requested_sections_are_read(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _health(hass)
    await _set_up_repairs(hass)

    result = await SystemTools(core_client).ha_get_system_health(include="repairs")

    assert {s for s in OPTIONAL_SECTIONS if s in result} == {"repairs"}


async def test_zha_network_summarises_each_devices_signal(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _health(hass)
    await _set_up_zha(hass)

    zha = (await SystemTools(core_client).ha_get_system_health(include="zha_network"))[
        "zha_network"
    ]

    device = next(d for d in zha["devices"] if d["ieee"] == IEEE)
    assert (device["lqi"], device["rssi"]) == (200, -45)
    assert zha["count"] == zha["total_count"] == len(zha["devices"])


async def test_zha_network_full_returns_zhas_whole_device(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _health(hass)
    await _set_up_zha(hass)
    tools = SystemTools(core_client)

    summary = (await tools.ha_get_system_health(include="zha_network"))["zha_network"]
    full = (await tools.ha_get_system_health(include="zha_network_full"))["zha_network"]

    summary_device = next(d for d in summary["devices"] if d["ieee"] == IEEE)
    full_device = next(d for d in full["devices"] if d["ieee"] == IEEE)
    assert set(full_device) > set(summary_device)


async def test_ws_sections_run_at_the_same_time(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core answers repairs only once the startup read has arrived, which a
    serial fetch sends only after repairs was answered."""
    await _health(hass)
    await _set_up_repairs(hass)

    async with answered_after(
        hass, "repairs/list_issues", "integration/setup_info", monkeypatch
    ):
        result = await SystemTools(core_client).ha_get_system_health(
            include="repairs,integration_startup"
        )

    assert "error" not in result["repairs"]
    assert "integrations" in result["integration_startup"]


@pytest.mark.usefixtures("mock_async_zeroconf")
@pytest.mark.parametrize("section", list(WS_SECTIONS))
async def test_an_unanswered_section_reports_its_error_beside_its_siblings(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
) -> None:
    set_up, command = WS_SECTIONS[section]
    await _health(hass)
    await set_up(hass)

    async with held(hass, command, monkeypatch):
        result = await SystemTools(core_client).ha_get_system_health(
            include=f"{section},integration_startup"
        )

    assert result["success"] is True
    assert result[section]["error"]
    assert "integrations" in result["integration_startup"]


@pytest.mark.usefixtures("mock_async_zeroconf")
@pytest.mark.parametrize("section", list(WS_SECTIONS))
async def test_a_dropped_health_connection_fails_the_call_instead_of_one_section(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
) -> None:
    """Once the connection is gone every section is blind: the call fails
    with the cause rather than answer with sections that saw nothing."""
    set_up, command = WS_SECTIONS[section]
    await _health(hass)
    await set_up(hass)

    with (
        health_socket_dropped_on(hass, command, monkeypatch),
        pytest.raises(ToolError) as excinfo,
    ):
        await SystemTools(core_client).ha_get_system_health(include=section)

    assert _code(excinfo) == "CONNECTION_FAILED"


async def test_a_config_check_with_no_answer_fails_the_call(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _health(hass)

    with (
        patch.object(
            core_client,
            "check_config",
            AsyncMock(side_effect=HomeAssistantConnectionError("rest dead")),
        ),
        pytest.raises(ToolError) as excinfo,
    ):
        await SystemTools(core_client).ha_get_system_health(include="config_check")

    assert _code(excinfo) == "CONNECTION_FAILED"


async def test_a_cancelled_call_unwinds_instead_of_returning(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled request must not come back as a result with the
    cancellation folded into a section."""
    await _health(hass)
    await _set_up_repairs(hass)
    tools = SystemTools(core_client)

    async with held(hass, "repairs/list_issues", monkeypatch):
        with arrival(hass, "repairs/list_issues") as arrived:
            call = asyncio.ensure_future(tools.ha_get_system_health(include="repairs"))
            async with asyncio.timeout(5):
                await arrived.wait()
            call.cancel()
            await asyncio.wait([call])
            assert call.cancelled()


# --- without the health baseline ---------------------------------------------
# Core's ``system_health`` is not set up, so Core refuses the baseline read.


async def test_without_the_health_baseline_config_check_still_returns(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _set_up_config(hass)

    result = await SystemTools(core_client).ha_get_system_health(include="config_check")

    assert result["success"] is True
    assert result["baseline_available"] is False
    assert "result" in result["config_check"]


async def test_without_the_health_baseline_ws_sections_say_they_need_it(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _set_up_config(hass)
    ws_sections = ("repairs", "thread_network", "matter_network")

    result = await SystemTools(core_client).ha_get_system_health(
        include=",".join((*ws_sections, "config_check"))
    )

    assert result["success"] is True
    assert "result" in result["config_check"]
    for section in ws_sections:
        assert "system_health WebSocket" in result[section]["error"]
        assert any(section in w for w in result["warnings"])


async def test_without_the_health_baseline_a_call_needing_it_fails(
    core_client: HomeAssistantClient,
) -> None:
    """Nothing requested can be served without the baseline, so a success
    would be empty."""
    with pytest.raises(ToolError):
        await SystemTools(core_client).ha_get_system_health(include="repairs")
