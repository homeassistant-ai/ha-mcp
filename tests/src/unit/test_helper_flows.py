"""Helper types come from Core's helper flow list, read at call time (#2632).

Also holds Core's helper flow list reads and a custom helper integration."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.components.config import config_entries as config_entries_view
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_clear_custom_components_cache
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from ha_mcp import backup_manager as bm
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.tools import flow_helper_lookup, helper_flows
from ha_mcp.tools.config_entry_backup import resolve_config_entry_backup_domain
from ha_mcp.tools.tools_config_helpers import HelperConfigTools
from ha_mcp.tools.tools_integrations import IntegrationTools

from ._backup_core import backup_settings as backup_settings
from ._backup_core import manage_backup
from ._backup_core import snapshot_manager as snapshot_manager
from ._core_setup import install_custom_helper
from ._template_helper_core import create_template_sensor
from ._tool_errors import error_of

CUSTOM = "my_custom_helper"


@contextmanager
def flow_list_reads() -> Iterator[AsyncMock]:
    """Count Core's reads of the helper flow list; Core still answers each one."""
    with patch.object(
        config_entries_view,
        "async_get_config_flows",
        wraps=config_entries_view.async_get_config_flows,
    ) as spy:
        yield spy


class MonotonicClock:
    """The reading of ``time.monotonic`` the helper flow cache sees."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now


class _CustomHelperFlow(ConfigFlow):
    """A helper integration's config flow: a name and a source entity."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=probatio.Schema(
                    {probatio.Required("name"): str, probatio.Required("source"): str}
                ),
            )
        return self.async_create_entry(title=user_input["name"], data=user_input)


@pytest.fixture
def custom_helper_flow(
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    enable_custom_integrations: None,
) -> Iterator[None]:
    """``my_custom_helper``: a custom helper integration Core's loader finds on
    disk and whose config flow runs."""
    install_custom_helper(tmp_path, monkeypatch, CUSTOM)
    mock_integration(hass, MockModule(CUSTOM))
    mock_platform(hass, f"{CUSTOM}.config_flow")
    with mock_config_flow(CUSTOM, _CustomHelperFlow):
        yield


@pytest.fixture
def _server_helper_flows() -> None:
    """The server reads Core's helper flow list itself."""


@pytest.fixture
def _core_helper_flows() -> None:
    """The component's loader read is Core's own."""


@pytest.fixture
async def config_up(hass: HomeAssistant) -> None:
    """``config`` serves the flow list and the registry reads."""
    assert await async_setup_component(hass, "config", {})


async def _call_with_unknown_type(kind: str, client: HomeAssistantClient) -> Any:
    if kind == "list":
        return await HelperConfigTools(client).ha_config_list_helpers(
            helper_type="no_such_helper"
        )
    if kind == "set":
        return await HelperConfigTools(client).ha_config_set_helper(
            helper_type="no_such_helper", name="X"
        )
    return await IntegrationTools(client).ha_remove_helpers_integrations(
        target="no_such_helper.x", helper_type="no_such_helper", confirm=True
    )


@pytest.mark.usefixtures("config_up")
async def test_helper_types_are_read_from_core_once_per_client(
    core_client: HomeAssistantClient,
) -> None:
    with flow_list_reads() as reads:
        types = [await helper_flows.helper_flow_types(core_client) for _ in range(2)]
    assert types[0] == types[1]
    assert {"template", "group"} <= types[0]
    assert "automation" not in types[0]
    reads.assert_awaited_once()


@pytest.mark.usefixtures("config_up")
async def test_a_helper_integration_installed_later_shows_up_after_the_cache_expires(
    core_client: HomeAssistantClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = MonotonicClock()
    monkeypatch.setattr(helper_flows, "time", clock)
    with flow_list_reads() as reads:
        await helper_flows.helper_flow_types(core_client)
        await helper_flows.helper_flow_types(core_client)
        assert reads.await_count == 1
        clock.now += 301
        await helper_flows.helper_flow_types(core_client)
    assert reads.await_count == 2


async def test_an_unreadable_helper_list_is_a_connection_error_not_an_empty_one(
    unreachable_client: HomeAssistantClient,
) -> None:
    with pytest.raises(HomeAssistantConnectionError):
        await helper_flows.helper_flow_types(unreachable_client)


@pytest.mark.parametrize("helper_type,also", [("input_boolean", ()), ("all", ("all",))])
async def test_storage_types_need_no_core_read(
    unreachable_client: HomeAssistantClient, helper_type: str, also: tuple[str, ...]
) -> None:
    """A read would fail here, as it does for a flow type."""
    await helper_flows.require_helper_type(unreachable_client, helper_type, *also)
    with pytest.raises(ToolError):
        await helper_flows.require_helper_type(unreachable_client, "template")


@pytest.mark.usefixtures("config_up")
async def test_otp_is_accepted_like_any_helper_flow_core_lists(
    core_client: HomeAssistantClient,
) -> None:
    await helper_flows.require_helper_type(core_client, "otp")


@pytest.mark.usefixtures("config_up", "enable_custom_integrations")
async def test_a_helper_integration_installed_since_the_last_read_is_accepted(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await helper_flows.helper_flow_types(core_client)  # cached before the install
    install_custom_helper(tmp_path, monkeypatch, CUSTOM)
    async_clear_custom_components_cache(hass)
    with flow_list_reads() as reads:
        await helper_flows.require_helper_type(core_client, CUSTOM)
    reads.assert_awaited_once()


@pytest.mark.usefixtures("config_up")
async def test_a_fresh_read_is_not_repeated_for_an_unknown_type(
    core_client: HomeAssistantClient,
) -> None:
    with flow_list_reads() as reads, pytest.raises(ToolError):
        await helper_flows.require_helper_type(core_client, "no_such_helper")
    reads.assert_awaited_once()


@pytest.mark.usefixtures("config_up", "enable_custom_integrations")
async def test_an_entity_of_a_newly_installed_helper_integration_is_a_helper(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entity = entity_registry.async_get_or_create("sensor", CUSTOM, "gate")
    await helper_flows.helper_flow_types(core_client)  # cached before the install
    install_custom_helper(tmp_path, monkeypatch, CUSTOM)
    async_clear_custom_components_cache(hass)
    assert await flow_helper_lookup.resolve_helper_entity(
        core_client, entity.entity_id
    ) == (CUSTOM, entity.entity_id)


@pytest.mark.usefixtures("config_up")
@pytest.mark.parametrize("kind", ["list", "set", "remove"])
async def test_an_unknown_helper_type_is_refused_naming_the_known_ones(
    core_client: HomeAssistantClient, kind: str
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await _call_with_unknown_type(kind, core_client)
    error = error_of(exc_info)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert "no_such_helper" in error["message"]
    suggestions = " | ".join(error["suggestions"])
    assert "Storage helpers: counter" in suggestions
    assert "Helper flows on this instance: " in suggestions
    assert "template" in suggestions


@pytest.mark.parametrize("kind", ["list", "set", "remove"])
async def test_a_failed_helper_flow_read_is_a_structured_connection_error(
    unreachable_client: HomeAssistantClient, kind: str
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await _call_with_unknown_type(kind, unreachable_client)
    assert error_of(exc_info)["code"] == "CONNECTION_FAILED"


async def _update_unknown_helper_type(
    hass: HomeAssistant, client: HomeAssistantClient
) -> tuple[dict[str, Any], MockConfigEntry]:
    """Update a ``no_such_helper`` the component does not cover and Core does
    not list; return the refusal and the real template entry it targeted."""
    entry = await create_template_sensor(hass, "1")
    with pytest.raises(ToolError) as exc_info:
        await HelperConfigTools(client).ha_config_set_helper(
            helper_type="no_such_helper",
            helper_id=entry.entry_id,
            config={"state": "{{ 2 }}"},
        )
    return error_of(exc_info), entry


@pytest.mark.usefixtures("backup_settings")
async def test_an_update_with_an_unknown_helper_type_is_refused_before_any_write(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """with_auto_backup captures before the tool body checks helper_type; the
    update is still refused before it changes the entry."""
    error, entry = await _update_unknown_helper_type(hass, component_core_client)
    assert error["code"] == "VALIDATION_INVALID_PARAMETER"
    assert entry.options["state"] == "{{ '1' }}"


@pytest.mark.usefixtures("backup_settings")
async def test_the_same_update_with_the_entrys_own_type_is_written(
    hass: HomeAssistant, component_core_client: HomeAssistantClient
) -> None:
    """The twin of the refusal above: it is the type, not the call, that refuses."""
    entry = await create_template_sensor(hass, "1")
    await HelperConfigTools(component_core_client).ha_config_set_helper(
        helper_type="template", helper_id=entry.entry_id, config={"state": "{{ 2 }}"}
    )
    assert entry.options["state"] == "{{ 2 }}"


@pytest.mark.usefixtures("backup_settings")
async def test_the_skipped_capture_of_an_unknown_helper_type_is_not_a_failed_read(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A type Core does not list has nothing to back up: the capture logs the
    skip at INFO, not a failed fetch at WARNING."""
    with caplog.at_level(logging.INFO):
        await _update_unknown_helper_type(hass, component_core_client)
    records = [r for r in caplog.records if r.name == bm.logger.name]
    assert [r for r in records if r.levelno >= logging.WARNING] == []
    assert any(
        r.levelno == logging.INFO and "no_such_helper" in r.getMessage()
        for r in records
    )


@pytest.mark.usefixtures("backup_settings")
async def test_an_entry_delete_proceeds_when_the_helper_flow_read_fails(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deciding whether the deleted entry is a helper needs Core's helper flow
    list; when that read fails the delete still runs, with the skipped backup
    reported."""
    entry = await create_template_sensor(hass, "1")
    monkeypatch.setattr(
        config_entries_view,
        "async_get_config_flows",
        _failing_flow_list,
    )
    result = await IntegrationTools(
        component_core_client
    ).ha_remove_helpers_integrations(target=entry.entry_id, confirm=True)
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    [warning] = result.get("warnings") or [None]
    assert warning is not None
    assert warning.startswith("No pre-write backup was taken")


async def _failing_flow_list(*args: Any, **kwargs: Any) -> set[str]:
    raise HomeAssistantConnectionError("down")


@pytest.mark.usefixtures("custom_helper_flow", "config_up")
async def test_a_custom_helper_flow_is_created_through_its_config_flow(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A helper integration outside Core's built-in list is created through
    its own config flow, like template or group."""
    result = await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type=CUSTOM,
        name="Gate",
        config={"source": "sensor.x"},
        wait=False,
    )
    assert result["success"] is True, result
    [entry] = hass.config_entries.async_entries(CUSTOM)
    assert entry.title == "Gate"
    assert entry.data == {"name": "Gate", "source": "sensor.x"}


@pytest.mark.usefixtures("custom_helper_flow", "config_up")
async def test_a_custom_helper_flow_describes_its_config_flow_fields(
    core_client: HomeAssistantClient,
) -> None:
    result = await HelperConfigTools(core_client).ha_config_list_helpers(
        helper_type=CUSTOM, describe=True
    )
    assert [field["name"] for field in result["fields"]] == ["name", "source"]


@pytest.mark.usefixtures("custom_helper_flow", "config_up")
async def test_a_custom_helper_flow_is_removed_through_the_flow_path(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entity_registry: er.EntityRegistry,
) -> None:
    entry = MockConfigEntry(domain=CUSTOM, title="Gate")
    entry.add_to_hass(hass)
    entity = entity_registry.async_get_or_create(
        "sensor", CUSTOM, "gate", config_entry=entry
    )
    await IntegrationTools(core_client).ha_remove_helpers_integrations(
        target=entity.entity_id, helper_type=CUSTOM, confirm=True
    )
    assert hass.config_entries.async_get_entry(entry.entry_id) is None


@pytest.mark.usefixtures("custom_helper_flow", "config_up")
async def test_a_platform_that_is_a_custom_helper_flow_is_offered_as_the_retry(
    core_client: HomeAssistantClient, entity_registry: er.EntityRegistry
) -> None:
    entity = entity_registry.async_get_or_create("sensor", CUSTOM, "gate")
    with pytest.raises(ToolError) as exc_info:
        await IntegrationTools(core_client).ha_remove_helpers_integrations(
            target=entity.entity_id, helper_type="template", confirm=True
        )
    assert error_of(exc_info)["suggestion"] == (
        f"Pass helper_type='{CUSTOM}', or omit helper_type."
    )


@pytest.mark.usefixtures("custom_helper_flow")
async def test_a_template_helper_is_read_through_its_options_handler(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    entry = await create_template_sensor(hass, "1")
    handler = snapshot_manager.handler_for("helper_template")
    assert handler is not None
    held = await handler.fetch(component_core_client, entry.entry_id)
    assert held["entry_id"] == entry.entry_id
    assert held["options"]["state"] == "{{ '1' }}"


async def test_a_mistyped_flow_domain_does_not_stay_in_the_supported_domains(
    snapshot_manager: bm.BackupManager,
) -> None:
    before = snapshot_manager.supported_domains()
    assert snapshot_manager.handler_for("helper_input_bolean") is not None
    assert snapshot_manager.supported_domains() == before
    assert snapshot_manager.handler_for("helper_") is None


@pytest.mark.usefixtures("config_up")
async def test_an_on_demand_snapshot_of_an_unlisted_helper_type_is_refused(
    component_core_client: HomeAssistantClient, snapshot_manager: bm.BackupManager
) -> None:
    with pytest.raises(ToolError) as exc_info:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="create",
            domain="helper_input_bolean",
            entity_id="x.y",
        )
    assert error_of(exc_info)["code"] == "VALIDATION_INVALID_PARAMETER"


@pytest.mark.usefixtures("config_up")
@pytest.mark.parametrize(
    "entry_domain,backup_domain",
    [("template", "helper_template"), ("hue", "integration")],
)
async def test_an_entry_delete_is_backed_up_as_a_helper_only_for_a_helper_flow(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    entry_domain: str,
    backup_domain: str,
) -> None:
    entry = MockConfigEntry(domain=entry_domain)
    entry.add_to_hass(hass)
    domain = await resolve_config_entry_backup_domain(
        core_client, {"target": entry.entry_id}, "integration", entry.entry_id
    )
    assert domain == backup_domain


@pytest.mark.usefixtures("custom_helper_flow", "backup_settings")
@pytest.mark.parametrize(
    "domain,options,detail",
    [
        (CUSTOM, {"k": "v"}, "custom integration"),
        ("otp", {}, "no stored options"),
    ],
)
async def test_a_helper_without_readable_options_is_not_backed_up_and_says_why(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    domain: str,
    options: dict[str, Any],
    detail: str,
) -> None:
    entry = MockConfigEntry(domain=domain, options=options)
    entry.add_to_hass(hass)
    with pytest.raises(ToolError) as exc_info:
        await manage_backup(
            component_core_client,
            scope="edits",
            action="create",
            domain=f"helper_{domain}",
            entity_id=entry.entry_id,
        )
    assert detail in error_of(exc_info)["message"]
