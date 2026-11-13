"""``ha_get_system_health`` reads Core's whole ``system_health/info`` stream.

Core answers with ``initial``, one ``update`` per pending check, then
``finish``. A test integration registers a real system_health platform, so the
stream is Core's own.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockModule, mock_integration, mock_platform

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import SystemTools

_DOMAIN = "health_probe"


async def _set_up_probe(
    hass: HomeAssistant, info: dict[str, Any], tmp_path: Path
) -> None:
    """Core's ``system_health`` with one integration whose platform reports
    ``info``, and the frontend that serves the labels. The development-repo
    path keeps the frontend off its package files."""

    async def info_callback(hass: HomeAssistant) -> dict[str, Any]:
        return info

    def async_register(hass: HomeAssistant, register: Any) -> None:
        register.async_register_info(info_callback)

    mock_integration(hass, MockModule(_DOMAIN))
    mock_platform(
        hass,
        f"{_DOMAIN}.system_health",
        SimpleNamespace(async_register=async_register),
    )
    assert await async_setup_component(hass, "system_health", {})
    assert await async_setup_component(hass, _DOMAIN, {})
    frontend = {"frontend": {"development_repo": str(tmp_path)}}
    assert await async_setup_component(hass, "frontend", frontend)


async def test_component_count_matches_the_components_core_reports(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    await _set_up_probe(hass, {"version": "1"}, tmp_path)

    result = await SystemTools(core_client).ha_get_system_health()

    assert set(result["health_info"]) == {_DOMAIN, "lovelace"}
    assert result["component_count"] == 2


async def test_a_slow_check_is_awaited_instead_of_left_pending(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    async def slow() -> str:
        await asyncio.sleep(0)
        return "ok"

    await _set_up_probe(hass, {"check": slow()}, tmp_path)

    result = await SystemTools(core_client).ha_get_system_health()

    assert result["health_info"][_DOMAIN]["info"]["check"] == "ok"
    assert "warnings" not in result


async def test_a_check_that_raises_shows_cores_failure(
    hass: HomeAssistant, core_client: HomeAssistantClient, tmp_path: Path
) -> None:
    async def broken() -> str:
        raise RuntimeError("probe failure")

    await _set_up_probe(hass, {"check": broken()}, tmp_path)

    result = await SystemTools(core_client).ha_get_system_health()

    assert result["health_info"][_DOMAIN]["info"]["check"]["type"] == "failed"


async def test_a_check_still_running_at_the_deadline_stays_pending_and_is_cancelled(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cancelled = asyncio.Event()

    async def hanging() -> str:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return "never"

    monkeypatch.setattr(
        "ha_mcp.tools.system_health_sections.HEALTH_DEADLINE_SECONDS", 2.0
    )
    await _set_up_probe(hass, {"check": hanging()}, tmp_path)

    result = await SystemTools(core_client).ha_get_system_health()

    assert result["health_info"][_DOMAIN]["info"]["check"] == {"type": "pending"}
    assert f"{_DOMAIN}.check" in result["warnings"][0]
    await asyncio.wait_for(cancelled.wait(), timeout=5)
