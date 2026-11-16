"""``ha_reload_core`` on real Home Assistant Core.

``target="all"`` is Core's own ``homeassistant.reload_all``, and every other
target is a reload service Core has registered, so the tests set up Core's
``homeassistant`` integration and watch which services Core runs. A reload
service of another integration is the test's own (Core's
``async_mock_service``), so it can fail. A single config entry reloads through
Core's own config-entries endpoint, on an integration the test mocks with
Core's ``mock_integration``.
"""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigFlow
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant
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
from ha_mcp.tools.tools_system import SystemTools

PROBE = "reload_probe"


def _service_calls(hass: HomeAssistant) -> list[Event[Any]]:
    calls: list[Event[Any]] = []
    hass.bus.async_listen(EVENT_CALL_SERVICE, calls.append)
    return calls


def _called(calls: list[Event[Any]]) -> list[str]:
    return [f"{call.data['domain']}.{call.data['service']}" for call in calls]


async def _write_configuration(hass: HomeAssistant, text: str) -> None:
    path = Path(hass.config.config_dir, "configuration.yaml")
    await hass.async_add_executor_job(path.write_text, text)


def _error(excinfo: pytest.ExceptionInfo[ToolError]) -> dict[str, Any]:
    error: dict[str, Any] = json.loads(str(excinfo.value))["error"]
    return error


@pytest.fixture
async def core_reloads(hass: HomeAssistant) -> None:
    """Core's ``homeassistant`` services over a configuration that checks out,
    and the themes reload Core's reload-all also calls."""
    await _write_configuration(hass, "")
    assert await async_setup_component(hass, "homeassistant", {})
    async_mock_service(hass, "frontend", "reload_themes")


@pytest.mark.usefixtures("core_reloads")
async def test_all_is_cores_own_reload_all(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Core reloads every integration that has a reload service, whichever
    they are, plus the core config, themes and custom templates."""
    async_mock_service(hass, "automation", "reload")
    async_mock_service(hass, "an_integration_of_tomorrow", "reload")
    calls = _service_calls(hass)

    result = await SystemTools(core_client).ha_reload_core(target="all")

    assert result["success"] is True
    assert result["service"] == "homeassistant.reload_all"
    called = _called(calls)
    assert called[0] == "homeassistant.reload_all"
    assert sorted(called[1:]) == [
        "an_integration_of_tomorrow.reload",
        "automation.reload",
        "frontend.reload_themes",
        "homeassistant.reload_core_config",
        "homeassistant.reload_custom_templates",
    ]


@pytest.mark.usefixtures("core_reloads")
async def test_all_is_refused_with_cores_reason_when_the_configuration_is_invalid(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _write_configuration(hass, "homeassistant:\n  latitude: north\n")
    reloads = async_mock_service(hass, "automation", "reload")

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(target="all")

    error = _error(excinfo)
    assert "configuration is not valid" in error["message"]
    assert "latitude" in error["message"]
    assert error["suggestion"] == "Fix what Home Assistant reported, then reload again"
    assert reloads == []


@pytest.mark.usefixtures("core_reloads")
@pytest.mark.parametrize(
    ("target", "service"),
    [
        ("automation", "automation.reload"),
        ("core", "homeassistant.reload_core_config"),
        ("themes", "frontend.reload_themes"),
        ("custom_templates", "homeassistant.reload_custom_templates"),
    ],
)
async def test_one_target_calls_only_its_service(
    hass: HomeAssistant, core_client: HomeAssistantClient, target: str, service: str
) -> None:
    async_mock_service(hass, "automation", "reload")
    async_mock_service(hass, "script", "reload")
    calls = _service_calls(hass)

    result = await SystemTools(core_client).ha_reload_core(target=target)

    assert _called(calls) == [service]
    assert result["success"] is True
    assert result["service"] == service


@pytest.mark.usefixtures("core_reloads")
async def test_a_domain_core_cannot_reload_is_refused_with_the_ones_it_can(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """An integration that is not loaded has no reload service; Core's list of
    services says which domains do."""
    async_mock_service(hass, "automation", "reload")
    calls = _service_calls(hass)

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(target="group")

    response = json.loads(str(excinfo.value))
    assert response["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert response["valid_targets"] == [
        "all",
        "core",
        "themes",
        "custom_templates",
        "automation",
    ]
    assert calls == []


@pytest.mark.usefixtures("core_reloads")
async def test_a_reload_core_fails_is_reported_with_cores_text(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    async_mock_service(
        hass,
        "template",
        "reload",
        raise_exception=HomeAssistantError("the template file is unreadable"),
    )

    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(target="template")

    error = _error(excinfo)
    assert error["code"] == "SERVICE_CALL_FAILED"
    assert "template.reload failed" in error["message"]
    assert "the template file is unreadable" in error["message"]


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
            target="script", entry_id=setups[0]
        )

    assert _error(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
    assert len(setups) == 1


async def test_a_blank_entry_id_is_refused(core_client: HomeAssistantClient) -> None:
    with pytest.raises(ToolError) as excinfo:
        await SystemTools(core_client).ha_reload_core(entry_id="   ")

    assert _error(excinfo)["code"] == "VALIDATION_INVALID_PARAMETER"
