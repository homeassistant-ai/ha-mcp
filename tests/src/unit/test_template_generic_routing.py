"""Generic config-entry mutations retain recoverable flow-helper options.

``ha_set_integration`` (options, enable/disable) and
``ha_remove_helpers_integrations`` change a config entry Core owns. For a flow
helper (Template, Min/Max, ...) the auto-backup captures the helper's own
options before the write, inside the entry's write lock, so the snapshot can
recreate it; any other entry keeps the integration backup. Every entry,
flow and registry row below is Core's own, reached through the server's
client with the ``ha_mcp_tools`` component installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryDisabler,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
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
from ha_mcp.config import get_global_settings, reset_global_settings
from ha_mcp.tools import config_entry_backup
from ha_mcp.tools.tools_integrations import IntegrationTools

_PLAIN = "plain_hub"


@dataclass
class Mutation:
    """What was true when Core first changed the entry."""

    snapshots: list[dict[str, Any]]
    locked: asyncio.Task[bool]


@dataclass
class World:
    hass: HomeAssistant
    client: HomeAssistantClient
    manager: bm.BackupManager
    tools: IntegrationTools
    mutations: list[Mutation] = field(default_factory=list)
    # Entries are recorded only once the test's own setup is done.
    watching: bool = False

    def snapshots(self) -> list[dict[str, Any]]:
        return [
            self.manager.read_snapshot(row["name"])
            for row in self.manager.list_snapshots()
        ]


async def _write_lock_held(manager: bm.BackupManager, entry_id: str) -> bool:
    """Whether another write to the entry would have to wait."""
    try:
        async with asyncio.timeout(0), manager.config_entry_write_guard(entry_id):
            return False
    except TimeoutError:
        return True


@pytest.fixture
async def world(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[World]:
    """Auto-backup on, and a record of each entry's first change by Core."""
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("AUTO_BACKUP_THROTTLE_MINUTES", "0")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    reset_global_settings()
    assert await async_setup_component(hass, "config", {})
    manager = bm.get_backup_manager(component_core_client, get_global_settings())
    world = World(
        hass, component_core_client, manager, IntegrationTools(component_core_client)
    )
    seen: set[str] = set()

    @callback
    def _changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if not world.watching or entry.entry_id in seen:
            return
        seen.add(entry.entry_id)
        world.mutations.append(
            Mutation(
                world.snapshots(),
                hass.async_create_task(_write_lock_held(manager, entry.entry_id)),
            )
        )

    unsubscribe = async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _changed)
    yield world
    unsubscribe()
    await hass.async_block_till_done()


async def _flow_helper(
    hass: HomeAssistant, domain: str, user_input: dict[str, Any], menu: str = ""
) -> ConfigEntry:
    """A flow helper created through Core's own config flow."""
    flow = await hass.config_entries.flow.async_init(domain, context={"source": "user"})
    if menu:
        flow = await hass.config_entries.flow.async_configure(
            flow["flow_id"], {"next_step_id": menu}
        )
    flow = await hass.config_entries.flow.async_configure(flow["flow_id"], user_input)
    assert flow["type"] == FlowResultType.CREATE_ENTRY, flow
    await hass.async_block_till_done()
    entry: ConfigEntry = flow["result"]
    return entry


async def _template(hass: HomeAssistant, template_type: str = "sensor") -> ConfigEntry:
    return await _flow_helper(
        hass, "template", {"name": "Example", "state": "{{ 12 }}"}, template_type
    )


async def _min_max(hass: HomeAssistant) -> ConfigEntry:
    hass.states.async_set("sensor.a", "1")
    hass.states.async_set("sensor.b", "2")
    return await _flow_helper(
        hass,
        "min_max",
        {"name": "Lowest", "entity_ids": ["sensor.a", "sensor.b"], "type": "min"},
    )


def _entity_id(hass: HomeAssistant, entry: ConfigEntry) -> str:
    (row,) = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return row.entity_id


async def _first_mutation(world: World) -> tuple[list[dict[str, Any]], bool]:
    (mutation, *_) = world.mutations
    return mutation.snapshots, await mutation.locked


class _PlainOptions(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema({probatio.Required("host"): str}),
        )


class _PlainFlow(ConfigFlow):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return _PlainOptions()


@pytest.fixture
def plain_entry(hass: HomeAssistant) -> Iterator[MockConfigEntry]:
    """An entry of an integration that is not a helper, with an options flow."""
    mock_integration(hass, MockModule(_PLAIN))
    mock_platform(hass, f"{_PLAIN}.config_flow")
    entry = MockConfigEntry(
        domain=_PLAIN, title="Hub", data={"host": "10.0.0.1"}, options={"host": "a"}
    )
    entry.add_to_hass(hass)
    with mock_config_flow(_PLAIN, _PlainFlow):
        yield entry


async def _mutate(world: World, entry_id: str, operation: str) -> dict[str, Any]:
    """Edit the entry's options, or delete it, through the generic tools."""
    world.watching = True
    if operation == "options":
        return await world.tools.ha_set_integration(
            entry_id=entry_id, config={"state": "{{ 99 }}"}
        )
    return await world.tools.ha_remove_helpers_integrations(
        target=entry_id, confirm=True
    )


@pytest.mark.parametrize("operation", ["options", "delete"])
@pytest.mark.parametrize("template_type", ["sensor", "binary_sensor"])
async def test_generic_template_mutation_captures_options_before_write(
    world: World, operation: str, template_type: str
) -> None:
    entry = await _template(world.hass, template_type)
    entity_id = _entity_id(world.hass, entry)
    original = dict(entry.options)

    result = await _mutate(world, entry.entry_id, operation)

    assert result["success"] is True
    snapshots, locked = await _first_mutation(world)
    assert locked is True
    (snapshot,) = snapshots
    assert snapshot["domain"] == "helper_template"
    assert snapshot["entity_id"] == entry.entry_id
    assert snapshot["tool"] == (
        "ha_set_integration"
        if operation == "options"
        else "ha_remove_helpers_integrations"
    )
    assert snapshot["config"]["entry_id"] == entry.entry_id
    assert snapshot["config"]["options"] == original
    assert [row["entity_id"] for row in snapshot["config"]["entities"]] == [entity_id]


@pytest.mark.parametrize("operation", ["options", "delete"])
async def test_generic_mutation_of_any_flow_helper_captures_its_options(
    world: World, operation: str
) -> None:
    """The routing is by the entry's domain, not a template-only rule (#2632)."""
    entry = await _min_max(world.hass)
    original = dict(entry.options)
    world.watching = True
    if operation == "options":
        result = await world.tools.ha_set_integration(
            entry_id=entry.entry_id, config={"entity_ids": ["sensor.a"]}
        )
    else:
        result = await world.tools.ha_remove_helpers_integrations(
            target=entry.entry_id, confirm=True
        )

    assert result["success"] is True
    snapshots, locked = await _first_mutation(world)
    assert locked is True
    (snapshot,) = snapshots
    assert snapshot["domain"] == "helper_min_max"
    assert snapshot["config"]["options"] == original


async def test_explicit_flow_entry_id_is_rejected_without_capture(
    world: World,
) -> None:
    entry = await _template(world.hass)
    world.watching = True

    with (
        patch.object(
            world.client, "delete_config_entry", wraps=world.client.delete_config_entry
        ) as delete,
        pytest.raises(ToolError, match="ENTITY_NOT_FOUND"),
    ):
        await world.tools.ha_remove_helpers_integrations(
            target=entry.entry_id, helper_type="template", confirm=True, wait=False
        )

    delete.assert_not_awaited()
    assert world.hass.config_entries.async_get_entry(entry.entry_id) is not None
    assert world.mutations == []
    assert world.snapshots() == []


async def test_explicit_template_entity_id_captures_once_after_resolution(
    world: World,
) -> None:
    entry = await _template(world.hass)
    original = dict(entry.options)
    world.watching = True

    with patch.object(
        world.client, "delete_config_entry", wraps=world.client.delete_config_entry
    ) as delete:
        result = await world.tools.ha_remove_helpers_integrations(
            target=_entity_id(world.hass, entry),
            helper_type="template",
            confirm=True,
            wait=False,
        )

    assert result["success"] is True
    delete.assert_awaited_once_with(entry.entry_id)
    snapshots, locked = await _first_mutation(world)
    assert locked is True
    (snapshot,) = snapshots
    assert snapshot["domain"] == "helper_template"
    assert snapshot["entity_id"] == entry.entry_id
    assert snapshot["config"]["options"] == original
    assert len(world.snapshots()) == 1


@pytest.mark.parametrize("enabled", [False, True])
async def test_template_enable_disable_keeps_integration_backup(
    world: World, enabled: bool
) -> None:
    """Enabling or disabling keeps the entry's own row, its disabled flag
    included, rather than the helper's options."""
    entry = await _template(world.hass)
    if enabled:
        await world.hass.config_entries.async_set_disabled_by(
            entry.entry_id, ConfigEntryDisabler.USER
        )
        await world.hass.async_block_till_done()
    original = {"disabled_by": entry.disabled_by, "options": dict(entry.options)}
    world.watching = True

    with patch.object(
        world.client, "get_config_entry", wraps=world.client.get_config_entry
    ) as get_entry:
        result = await world.tools.ha_set_integration(
            entry_id=entry.entry_id, enabled=enabled
        )

    assert result["success"] is True
    assert entry.disabled_by == (None if enabled else ConfigEntryDisabler.USER)
    snapshots, locked = await _first_mutation(world)
    assert locked is True
    (snapshot,) = snapshots
    assert snapshot["domain"] == "integration"
    assert snapshot["config"]["entry_id"] == entry.entry_id
    assert snapshot["config"]["domain"] == "template"
    assert {key: snapshot["config"][key] for key in original} == original
    # Only the own-entry guard reads the entry; the backup keeps the whole
    # integration fragment without resolving a helper domain.
    get_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("operation", ["options", "delete"])
async def test_other_integration_mutations_keep_existing_backup(
    world: World, plain_entry: MockConfigEntry, operation: str
) -> None:
    original = dict(plain_entry.options)
    world.watching = True
    if operation == "options":
        result = await world.tools.ha_set_integration(
            entry_id=plain_entry.entry_id, config={"host": "b"}
        )
    else:
        result = await world.tools.ha_remove_helpers_integrations(
            target=plain_entry.entry_id, confirm=True
        )

    assert result["success"] is True
    snapshots, locked = await _first_mutation(world)
    assert locked is True
    (snapshot,) = snapshots
    assert snapshot["domain"] == "integration"
    assert snapshot["config"]["entry_id"] == plain_entry.entry_id
    assert snapshot["config"]["domain"] == _PLAIN
    assert snapshot["config"]["options"] == original


@pytest.mark.parametrize("operation", ["options", "delete"])
@pytest.mark.parametrize("capture_state", ["disabled", "lookup_failed"])
async def test_generic_template_mutation_preserves_best_effort_capture_policy(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    capture_state: str,
) -> None:
    entry = await _template(world.hass)
    if capture_state == "disabled":
        monkeypatch.setattr(get_global_settings(), "enable_auto_backup", False)
    else:
        # Core's list of helper flows, which names the snapshot's domain,
        # gets no answer.
        monkeypatch.setattr(
            config_entry_backup,
            "helper_flow_types",
            AsyncMock(side_effect=HomeAssistantConnectionError("no answer")),
        )

    with patch.object(
        world.client, "get_config_entry", wraps=world.client.get_config_entry
    ) as get_entry:
        result = await _mutate(world, entry.entry_id, operation)

    assert result["success"] is True
    snapshots, locked = await _first_mutation(world)
    assert (snapshots, locked) == ([], True)
    assert world.snapshots() == []
    # The mutation itself reads the entry once (the options walker, or the
    # delete's own-entry guard), independently of backup.
    assert get_entry.await_count == 1 + int(capture_state == "lookup_failed")


@pytest.mark.parametrize("confirm", [False, True])
async def test_flow_helper_removal_captures_once_and_only_when_confirmed(
    world: World, confirm: bool
) -> None:
    """Every flow helper, not only template, is captured by the inner backup."""
    entry = await _min_max(world.hass)
    target = _entity_id(world.hass, entry)
    world.watching = True
    remove = world.tools.ha_remove_helpers_integrations
    if not confirm:
        with pytest.raises(ToolError, match="not confirmed"):
            await remove(target=target, helper_type="min_max")
        assert world.snapshots() == []
        assert world.hass.config_entries.async_get_entry(entry.entry_id) is not None
        return

    result = await remove(target=target, helper_type="min_max", confirm=True)

    assert result["success"] is True
    assert [s["domain"] for s in world.snapshots()] == ["helper_min_max"]


async def test_entity_only_removal_captures_only_the_resolved_helper(
    world: World,
) -> None:
    """Without helper_type the registry names the helper; the removal captures
    that helper once, and the unresolved outer call attempts no snapshot (it
    would list every config entry looking for the entity_id)."""
    entry = await _min_max(world.hass)
    world.watching = True

    with patch.object(
        world.manager, "maybe_snapshot", wraps=world.manager.maybe_snapshot
    ) as capture:
        result = await world.tools.ha_remove_helpers_integrations(
            target=_entity_id(world.hass, entry), confirm=True
        )

    assert result["success"] is True
    assert [c.args for c in capture.call_args_list] == [
        ("helper_min_max", entry.entry_id)
    ]
    assert [s["domain"] for s in world.snapshots()] == ["helper_min_max"]
