"""``ha_reload_core`` on real Home Assistant Core.

The subsystem reloads are Core service calls. Each reload service is the
test's own (Core's ``async_mock_service``), so a test sees which ones Core
ran, and a reload can fail or wait. A single config entry reloads through
Core's own config-entries endpoint, on an integration the test mocks with
Core's ``mock_integration``.
"""

import asyncio
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    async_mock_service,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_system import RELOAD_TARGETS, SystemTools

# (target, domain, service) of every subsystem target="all" reloads.
RELOADABLE = [
    (target, *service) for target, service in RELOAD_TARGETS.items() if service
]
# Reloads Home Assistant runs at once for target="all".
RELOADS_AT_ONCE = 4
PROBE = "reload_probe"


def _reload_services(
    hass: HomeAssistant, *, failing: str | None = None, missing: str | None = None
) -> dict[str, list[ServiceCall]]:
    """Every reload service, keyed by target; ``failing`` raises, ``missing``
    is not registered (its integration is not loaded)."""
    return {
        target: async_mock_service(
            hass,
            domain,
            service,
            raise_exception=(
                HomeAssistantError(f"{target} reload blew up")
                if target == failing
                else None
            ),
        )
        for target, domain, service in RELOADABLE
        if target != missing
    }


def _service_calls(hass: HomeAssistant) -> list[Event[Any]]:
    calls: list[Event[Any]] = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, calls.append)
    return calls


def _error(excinfo: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(excinfo.value))["error"]
    return error


async def test_reload_all_runs_a_bounded_number_of_reloads_at_once(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Every reload is held until the bound's worth are running at Core, which
    a serial sweep never reaches; by then an unbounded sweep has already sent
    every call."""
    running = started = 0
    sent_when_full: list[int] = []
    full = asyncio.Event()

    async def reload(call: ServiceCall) -> None:
        nonlocal running, started
        running += 1
        started += 1
        if running == RELOADS_AT_ONCE or started == len(RELOADABLE):
            sent_when_full.append(sent.call_count)
            full.set()
        async with asyncio.timeout(5):
            await full.wait()
        running -= 1

    for _, domain, service in RELOADABLE:
        hass.services.async_register(domain, service, reload)
    # Until the client knows its token is an admin's, it sends one call at a
    # time; Core's auth answers that, as on every install.
    assert await async_setup_component(hass, "auth", {})

    with patch.object(
        core_client, "call_service", AsyncMock(wraps=core_client.call_service)
    ) as sent:
        result = await SystemTools(core_client).ha_reload_core(target="all")

    assert sent_when_full[0] == RELOADS_AT_ONCE
    assert result["reloaded"] == [target for target, _, _ in RELOADABLE]
    assert "warnings" not in result


async def test_reload_all_reports_a_failed_reload_and_still_runs_the_others(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    calls = _reload_services(hass, failing="templates")

    result = await SystemTools(core_client).ha_reload_core(target="all")

    assert all(len(made) == 1 for made in calls.values())
    assert result["success"] is True
    assert result["reloaded"] == [t for t, _, _ in RELOADABLE if t != "templates"]
    assert any(
        w.startswith("templates:") and "templates reload blew up" in w
        for w in result["warnings"]
    )


async def test_reload_all_skips_a_subsystem_that_is_not_loaded_without_a_warning(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Not every install loads every reloadable integration."""
    _reload_services(hass, missing="templates")

    result = await SystemTools(core_client).ha_reload_core(target="all")

    assert result["success"] is True
    assert "templates" not in result["reloaded"]
    assert "warnings" not in result


async def test_reload_one_target_calls_only_its_service(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    calls = _reload_services(hass)

    result = await SystemTools(core_client).ha_reload_core(target="automations")

    assert {target: len(made) for target, made in calls.items() if made} == {
        "automations": 1
    }
    assert result["success"] is True
    assert result["service"] == "automation.reload"


async def test_an_unknown_target_is_refused_before_anything_reloads(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    calls = _service_calls(hass)

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(target="bogus")

    assert _error(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert calls == []


@pytest.fixture
def probe_flow() -> Iterator[None]:
    """The config flow Core requires of an integration with config entries."""
    with mock_config_flow(PROBE, ConfigFlow):
        yield


async def _reloadable_entry(hass: HomeAssistant, *, unloads: bool = True) -> list[str]:
    """A loaded config entry; returns the entry id once per setup Core ran."""
    setups: list[str] = []

    async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
        setups.append(entry.entry_id)

    async def unload_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
        if not unloads:
            raise HomeAssistantError("the probe's device is busy")

    assert await async_setup_component(hass, "config", {})
    mock_integration(
        hass,
        MockModule(
            PROBE, async_setup_entry=setup_entry, async_unload_entry=unload_entry
        ),
    )
    mock_platform(hass, f"{PROBE}.config_flow", None)
    entry = MockConfigEntry(domain=PROBE)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    return setups


@pytest.mark.usefixtures("probe_flow")
async def test_entry_id_reloads_that_entry_and_no_subsystem(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    setups = await _reloadable_entry(hass)
    (entry_id,) = setups
    calls = _service_calls(hass)

    result = await SystemTools(core_client).ha_reload_core(entry_id=entry_id)

    assert setups == [entry_id, entry_id]
    assert calls == []
    assert result["reloaded"] is True
    assert result["require_restart"] is False


@pytest.mark.usefixtures("probe_flow")
async def test_an_entry_that_failed_to_unload_says_a_restart_is_needed(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    (entry_id,) = await _reloadable_entry(hass, unloads=False)

    result = await SystemTools(core_client).ha_reload_core(entry_id=entry_id)

    assert result["reloaded"] is False
    assert result["require_restart"] is True
    assert "restart" in result["message"].lower()


@pytest.mark.usefixtures("probe_flow")
async def test_an_entry_core_will_not_reload_is_not_reported_as_an_auth_failure(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core answers 403 for an entry it cannot reload (one stuck failing to
    unload); that is not the token being refused."""
    (entry_id,) = await _reloadable_entry(hass, unloads=False)
    assert not await hass.config_entries.async_unload(entry_id)
    entry = hass.config_entries.async_get_entry(entry_id)
    assert entry is not None
    assert entry.state is ConfigEntryState.FAILED_UNLOAD

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(entry_id=entry_id)

    error = _error(excinfo)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "cannot be reloaded" in error["message"]
    assert entry_id in error["message"]


async def test_an_unknown_entry_id_is_reported_as_not_found(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    assert await async_setup_component(hass, "config", {})

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(entry_id="missing_entry")

    error = _error(excinfo)
    assert error["code"] == "RESOURCE_NOT_FOUND"
    assert "missing_entry" in error["message"]


@pytest.mark.usefixtures("probe_flow")
async def test_an_entry_reload_core_refuses_the_user_fails(
    hass: HomeAssistant, read_only_client: HomeAssistantClient
) -> None:
    """Core reloads an entry only for an administrator."""
    setups = await _reloadable_entry(hass)

    with pytest.raises(ToolError):
        await SystemTools(read_only_client).ha_reload_core(entry_id=setups[0])

    assert len(setups) == 1


@pytest.mark.usefixtures("probe_flow")
async def test_entry_id_with_a_target_is_refused_without_reloading(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    setups = await _reloadable_entry(hass)

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(
            target="scripts", entry_id=setups[0]
        )

    assert _error(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert len(setups) == 1


async def test_a_blank_entry_id_is_refused(core_client: HomeAssistantClient) -> None:
    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(entry_id="   ")

    assert _error(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
