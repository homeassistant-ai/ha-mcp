"""Deleted Template helpers recreate without losing references or another entity.

The helper is a Template sensor that Core's own config flow made, captured
over the server's own client and then removed from Core; a restore recreates
it through Core's real creation flow.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from pytest_homeassistant.common import MockConfigEntry

from ha_mcp import backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._backup_core import add_label
from ._command_faults import received, unanswered
from ._template_backup_core import manager as manager
from ._template_backup_core import real_snapshot
from ._template_backup_core import settings as settings
from ._template_backup_core import template_entry as template_entry
from ._template_helper_core import set_template_state, state_template
from ._template_recovery_core import (
    CUSTOM_NAME,
    DOMAIN,
    RENAMED,
    DeletedTemplate,
    edit_snapshot,
    recreated_entry,
)
from ._template_recovery_core import deleted_template as deleted_template

RENAME = "config/entity_registry/update"


@pytest.fixture
def creation_flow(component_core_client: HomeAssistantClient) -> Iterator[AsyncMock]:
    """A spy on opening a creation flow; Core still answers it."""
    with patch.object(
        component_core_client,
        "start_config_flow",
        wraps=component_core_client.start_config_flow,
    ) as spy:
        yield spy


def _entity(entity_registry: er.EntityRegistry, entry: ConfigEntry) -> er.RegistryEntry:
    (row,) = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    return row


def _take_saved_id(entity_registry: er.EntityRegistry) -> er.RegistryEntry:
    """Another integration's entity registered under the snapshot's entity ID."""
    occupant = entity_registry.async_get_or_create(
        "sensor", "demo", "occupant", suggested_object_id=RENAMED.split(".")[1]
    )
    assert occupant.entity_id == RENAMED
    return occupant


async def _refused(deleted: DeletedTemplate) -> bm.BackupRestoreError:
    with pytest.raises(bm.BackupRestoreError) as caught:
        await deleted.manager.restore_snapshot(deleted.name)
    return caught.value


async def test_deleted_template_comes_back_under_its_saved_entity_id_and_name(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    deleted_template: DeletedTemplate,
    creation_flow: AsyncMock,
) -> None:
    manager = deleted_template.manager
    original = manager.read_snapshot(deleted_template.name)

    result = await manager.restore_snapshot(deleted_template.name)

    entry = recreated_entry(hass)
    assert result["restore_mode"] == "recreated"
    assert result["entity_id"] == entry.entry_id
    assert result["original_entry_id"] == deleted_template.entry_id
    assert result["verification_status"] == "matched"
    assert result["safety_backup"] is None
    assert "warnings" not in result["result"]
    row = _entity(entity_registry, entry)
    assert (row.entity_id, row.name) == (RENAMED, CUSTOM_NAME)
    assert manager.read_snapshot(deleted_template.name) == original
    creation_flow.assert_awaited_once()
    assert hass.config_entries.flow.async_progress() == []


@pytest.mark.parametrize("occupied_in", ["registry", "states"])
async def test_occupied_saved_entity_id_refuses_before_creating(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    deleted_template: DeletedTemplate,
    creation_flow: AsyncMock,
    occupied_in: str,
) -> None:
    if occupied_in == "registry":
        occupant = _take_saved_id(entity_registry)
    else:
        hass.states.async_set(RENAMED, "theirs")

    error = await _refused(deleted_template)

    assert error.outcome["apply_status"] == "not_applied"
    assert error.outcome["reason"] == "entity_id_collision"
    if occupied_in == "registry":
        assert entity_registry.async_get(RENAMED) == occupant
    else:
        state = hass.states.get(RENAMED)
        assert state is not None and state.state == "theirs"
    creation_flow.assert_not_awaited()
    assert not hass.config_entries.async_entries("template")


async def test_entry_back_before_recreation_is_neither_duplicated_nor_edited(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    component_core_client: HomeAssistantClient,
    creation_flow: AsyncMock,
) -> None:
    """The entry comes back (a restore of Home Assistant, another client) right
    after the restore found it absent: Core's own listing stops the recreation,
    and no options flow edits the entry without a safety snapshot."""
    manager = deleted_template.manager
    handler = manager.handler_for(DOMAIN)
    assert handler is not None
    back = MockConfigEntry(
        domain="template",
        entry_id=deleted_template.entry_id,
        options=manager.read_snapshot(deleted_template.name)["config"]["options"],
    )

    async def absent_then_back(client: Any, entity_id: str) -> Any:
        current = await handler.fetch(client, entity_id)
        back.add_to_hass(hass)
        return current

    manager.register(dataclasses.replace(handler, fetch=absent_then_back))

    with patch.object(
        component_core_client,
        "start_options_flow",
        wraps=component_core_client.start_options_flow,
    ) as options_flow:
        error = await _refused(deleted_template)

    assert error.outcome["reason"] == "entry_still_exists"
    assert error.outcome["apply_status"] == "not_applied"
    creation_flow.assert_not_awaited()
    options_flow.assert_not_awaited()
    assert hass.config_entries.async_entries("template") == [back]


async def test_a_flow_submission_with_no_reply_is_reported_unknown_and_not_retried(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first submission of the creation flow (its menu choice) gets no
    reply: the tool cannot tell how far Core got, so it reports the creation
    as unknown and sends nothing again."""
    monkeypatch.setattr(
        component_core_client,
        "submit_config_flow_step",
        submit := AsyncMock(side_effect=HomeAssistantConnectionError("reply lost")),
    )

    error = await _refused(deleted_template)

    assert error.outcome["apply_status"] == "unknown"
    assert error.outcome["reason"] == "creation_outcome_unknown"
    assert error.outcome["restore_mode"] == "recreated"
    assert error.outcome["original_entry_id"] == deleted_template.entry_id
    submit.assert_awaited_once()
    assert not hass.config_entries.async_entries("template")


async def test_snapshot_without_entity_mapping_recreates_with_a_warning(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    deleted_template: DeletedTemplate,
) -> None:
    """A snapshot from before entity IDs were recorded still brings the helper
    back, and says its entity may have a new ID."""
    manager = deleted_template.manager
    edit_snapshot(
        manager.backup_dir / deleted_template.name,
        lambda data: data["config"].pop("entities"),
    )

    result = await manager.restore_snapshot(deleted_template.name)

    entry = recreated_entry(hass)
    assert result["entity_id"] == entry.entry_id
    assert result["result"]["entity_ids_restored"] is False
    assert result["result"]["warnings"]
    assert _entity(entity_registry, entry).entity_id != RENAMED


async def test_capture_keeps_only_the_entity_fields_a_recreation_needs(
    entity_registry: er.EntityRegistry,
    label_registry: lr.LabelRegistry,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    """A label on the entity is not something recreation sets; it stays out."""
    row = _entity(entity_registry, template_entry)
    entity_registry.async_update_entity(
        row.entity_id,
        new_entity_id=RENAMED,
        name=CUSTOM_NAME,
        labels={add_label(label_registry, "Private")},
    )
    row = _entity(entity_registry, template_entry)

    path = await real_snapshot(manager, template_entry)

    assert manager.read_snapshot(path.name)["config"]["entities"] == [
        {
            "entity_id": RENAMED,
            "unique_id": row.unique_id,
            "name": CUSTOM_NAME,
            "original_name": row.original_name,
        }
    ]


@pytest.mark.parametrize("options_changed", [False, True])
async def test_diff_of_existing_template_ignores_entity_renames(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    options_changed: bool,
) -> None:
    """A restore of an existing entry applies options only, so a later entity
    rename is no difference to preview; an options edit is."""
    path = await real_snapshot(manager, template_entry)
    row = _entity(entity_registry, template_entry)
    entity_registry.async_update_entity(
        row.entity_id, new_entity_id="sensor.now_renamed", name="Renamed later"
    )
    if options_changed:
        await set_template_state(hass, template_entry, "edited")

    diff = await manager.diff_snapshot(path.name)
    data, current = await manager.snapshot_comparison(path.name)

    assert diff["kind"] == "dict"
    assert diff["unchanged"] is not options_changed
    assert diff["patch"] == (
        [{"op": "replace", "path": "/options/state", "value": state_template("saved")}]
        if options_changed
        else []
    )
    assert "entities" not in data["config"]
    assert "entities" not in current
    assert manager.read_snapshot(path.name)["config"]["entities"]


async def test_saved_entity_id_taken_after_creation_keeps_both_helpers(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    deleted_template: DeletedTemplate,
) -> None:
    """Another entity takes the saved ID once the creation flow has made the
    entry: the restore reports the new helper and leaves both entities alone."""
    occupants: list[er.RegistryEntry] = []

    @callback
    def take_on_creation(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if change == ConfigEntryChange.ADDED:
            occupants.append(_take_saved_id(entity_registry))

    stop = async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, take_on_creation)
    try:
        error = await _refused(deleted_template)
    finally:
        stop()

    entry = recreated_entry(hass)
    assert error.outcome["apply_status"] == "applied"
    assert error.outcome["reason"] == "entity_id_collision"
    assert error.outcome["entity_id"] == entry.entry_id
    assert error.outcome["original_entry_id"] == deleted_template.entry_id
    assert _entity(entity_registry, entry).entity_id != RENAMED
    assert [entity_registry.async_get(RENAMED)] == occupants
    assert deleted_template.manager.read_snapshot(deleted_template.name)


async def test_lost_rename_reply_reports_the_created_helper_without_retrying(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Core gets the recreated entity's rename but its answer never arrives."""
    async with unanswered(hass, RENAME, monkeypatch):
        with received(hass, RENAME) as renames:
            error = await _refused(deleted_template)

    entry = recreated_entry(hass)
    assert error.outcome["apply_status"] == "applied"
    assert error.outcome["entity_id"] == entry.entry_id
    assert error.outcome["verification_status"] == "unavailable"
    assert error.outcome["reason"] == "entity_rename_outcome_unknown"
    (rename,) = renames[RENAME]
    assert rename["new_entity_id"] == RENAMED


@pytest.mark.parametrize("mapping", ["foreign_unique_id", "non_string_name"])
async def test_unusable_entity_mapping_refuses_before_creating(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    creation_flow: AsyncMock,
    mapping: str,
) -> None:
    """A hand-edited mapping whose entity the recreated entry cannot own, or
    whose name is no name, is refused before Core is asked to create anything."""

    def change(data: dict[str, Any]) -> None:
        (row,) = data["config"]["entities"]
        if mapping == "foreign_unique_id":
            row["unique_id"] = "another-integration-entity"
        else:
            row["name"] = 5

    edit_snapshot(deleted_template.manager.backup_dir / deleted_template.name, change)

    error = await _refused(deleted_template)

    assert error.outcome["reason"] == "invalid_snapshot"
    assert error.outcome["apply_status"] == "not_applied"
    creation_flow.assert_not_awaited()
    assert not hass.config_entries.async_entries("template")


@pytest.mark.parametrize(
    ("options", "field"),
    [
        ({"icon": "mdi:gauge"}, "icon"),
        ({"additional_options": {"icon": "mdi:gauge"}}, "additional_options.icon"),
    ],
    ids=["top_level", "in_section"],
)
async def test_option_no_creation_form_takes_is_refused_before_creating(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    options: dict[str, Any],
    field: str,
) -> None:
    """Core's sensor form has no icon field, at the top or in its section, so
    the restore names it and closes the flow unsubmitted."""
    edit_snapshot(
        deleted_template.manager.backup_dir / deleted_template.name,
        lambda data: data["config"]["options"].update(options),
    )

    error = await _refused(deleted_template)

    assert error.outcome["apply_status"] == "not_applied"
    assert error.outcome["reason"] == "unsupported_fields"
    assert error.outcome["fields"] == [field]
    assert not hass.config_entries.async_entries("template")
    assert hass.config_entries.flow.async_progress() == []


async def test_options_core_stored_differently_leave_the_entity_unrenamed(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    deleted_template: DeletedTemplate,
) -> None:
    """Core keeps a state template given as a number as text: the created
    entry differs from the snapshot, so its entity is not given the saved ID."""
    edit_snapshot(
        deleted_template.manager.backup_dir / deleted_template.name,
        lambda data: data["config"]["options"].update(state=12),
    )

    error = await _refused(deleted_template)

    entry = recreated_entry(hass)
    assert entry.options["state"] == "12"
    assert error.outcome["apply_status"] == "applied"
    assert error.outcome["verification_status"] == "mismatched"
    row = _entity(entity_registry, entry)
    assert row.entity_id != RENAMED
    assert row.name is None
