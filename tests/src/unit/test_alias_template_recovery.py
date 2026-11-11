"""Prove alias deletion captures a fresh, restorable Template snapshot."""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import _reset_global_settings, get_global_settings
from ha_mcp.tools.tools_integrations import IntegrationTools

RENAMED = "sensor.renamed_example"
CUSTOM_NAME = "Custom name"


async def _create_renamed_template(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> ConfigEntry:
    """A real Template helper from Core's config flow, its entity renamed."""
    flow = await hass.config_entries.flow.async_init(
        "template", context={"source": "user"}
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "sensor"}
    )
    flow = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "Example", "state": "{{ 12 }}"}
    )
    assert flow["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry: ConfigEntry = flow["result"]
    entity = entity_registry.async_get_or_create("sensor", "template", entry.entry_id)
    entity_registry.async_update_entity(
        entity.entity_id, new_entity_id=RENAMED, name=CUSTOM_NAME
    )
    await hass.async_block_till_done()
    assert entity_registry.async_get(RENAMED) is not None
    return entry


async def test_alias_deletion_captures_before_remove_and_restores_exact_snapshot(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    component_core_client: HomeAssistantClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_AUTO_BACKUP", "true")
    monkeypatch.setenv("AUTO_BACKUP_RETAIN_PER_ENTITY", "5")
    monkeypatch.setenv("HAMCP_BACKUP_DIR", str(tmp_path / "backups"))
    _reset_global_settings()
    manager = bm.get_backup_manager(component_core_client, get_global_settings())
    assert await async_setup_component(hass, "config", {})

    old_entry = await _create_renamed_template(hass, entity_registry)
    options = dict(old_entry.options)

    # An earlier snapshot must not be mistaken for the one deletion captures.
    stale = await manager.maybe_snapshot(
        "helper_template", old_entry.entry_id, force=True
    )
    assert stale is not None
    before = {row["name"] for row in manager.list_snapshots()}

    removed = await IntegrationTools(
        component_core_client
    ).ha_remove_helpers_integrations(
        target=RENAMED, helper_type="template", confirm=True, wait=False
    )
    assert removed["success"] is True
    assert removed["entry_id"] == old_entry.entry_id
    assert removed["entity_ids"] == [RENAMED]
    assert hass.config_entries.async_get_entry(old_entry.entry_id) is None
    assert entity_registry.async_get(RENAMED) is None

    # Capture reads the live entry, so a snapshot of the full config can only
    # have been taken before the entry was removed.
    names = {row["name"] for row in manager.list_snapshots()} - before
    assert len(names) == 1, "Alias deletion must capture its own fresh snapshot"
    (name,) = names
    assert name != stale.name
    snapshot = manager.read_snapshot(name)
    assert snapshot["entity_id"] == old_entry.entry_id
    assert snapshot["config"]["entry_id"] == old_entry.entry_id
    assert snapshot["config"]["options"] == options
    assert [row["entity_id"] for row in snapshot["config"]["entities"]] == [RENAMED]

    restored = await manager.restore_snapshot(name)
    replacement = restored["result"]["entry_id"]
    assert replacement != old_entry.entry_id
    assert restored["original_entry_id"] == old_entry.entry_id
    assert restored["restore_mode"] == "recreated"
    assert restored["apply_status"] == "applied"
    assert restored["verification_status"] == "matched"
    await hass.async_block_till_done()
    new_entry = hass.config_entries.async_get_entry(replacement)
    assert new_entry is not None
    assert dict(new_entry.options) == options
    row = entity_registry.async_get(RENAMED)
    assert row is not None
    assert row.name == CUSTOM_NAME
    assert row.config_entry_id == replacement
    assert manager.read_snapshot(name) == snapshot
    assert len(hass.config_entries.async_entries("template")) == 1
