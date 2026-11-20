"""Template-helper backups hold the complete options and restore them whole.

The helper is a Template sensor (or button) that Core's own config flow made;
the manager captures and restores it over the server's own client, and a
restore goes through Core's options flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._command_faults import received
from ._template_backup_core import legacy_snapshot, real_snapshot
from ._template_backup_core import manager as manager
from ._template_backup_core import settings as settings
from ._template_backup_core import template_entry as template_entry
from ._template_helper_core import (
    create_template_sensor,
    set_template_state,
    state_template,
)
from ._template_recovery_core import DOMAIN, edit_snapshot, stored_state

HELPERS_LIST = "ha_mcp_tools/helpers_list"
RENAMED = "sensor.renamed_example"


def _renamed(
    entity_registry: er.EntityRegistry, entry: ConfigEntry, new_entity_id: str
) -> er.RegistryEntry:
    """The helper's one entity, renamed to ``new_entity_id``."""
    (row,) = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    return entity_registry.async_update_entity(
        row.entity_id, new_entity_id=new_entity_id
    )


def _flow_starts(client: HomeAssistantClient) -> Any:
    """A spy on each options flow the client opens; Core still answers it."""
    return patch.object(client, "start_options_flow", wraps=client.start_options_flow)


async def test_capture_holds_complete_options_and_entities_without_opening_a_flow(
    entity_registry: er.EntityRegistry,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    """A capture that missed an option, or the entity it names, could not put
    the helper back; one that opened an options flow would touch the helper."""
    row = _renamed(entity_registry, template_entry, RENAMED)

    with _flow_starts(component_core_client) as opened:
        path = await real_snapshot(manager, template_entry)

    snapshot = manager.read_snapshot(path.name)
    assert snapshot["entity_id"] == template_entry.entry_id
    assert snapshot["config"] == {
        "entry_id": template_entry.entry_id,
        "options": dict(template_entry.options),
        "entities": [
            {
                "entity_id": RENAMED,
                "unique_id": row.unique_id,
                "name": None,
                "original_name": row.original_name,
            }
        ],
    }
    opened.assert_not_awaited()


async def test_capture_of_secret_scrubbed_options_is_refused(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    """The component scrubs an option equal to a secrets.yaml value; a backup
    holding the scrub instead of the value could never restore it."""
    await hass.async_add_executor_job(
        Path(hass.config.path("secrets.yaml")).write_text,
        f'example_state: "{state_template("saved")}"\n',
    )

    with pytest.raises(bm.MandatoryBackupError, match="redacted"):
        await manager.maybe_snapshot(DOMAIN, template_entry.entry_id, mandatory=True)

    assert not list(manager.backup_dir.glob("*.yaml"))


async def test_absent_helper_is_not_captured(
    manager: bm.BackupManager, template_entry: ConfigEntry
) -> None:
    """A target no Template entry has gets no snapshot, not a stand-in."""
    assert await manager.maybe_snapshot(DOMAIN, "missing-entry", force=True) is None

    assert not list(manager.backup_dir.glob("*.yaml"))


@pytest.mark.parametrize(
    "added",
    [
        {"unit_of_measurement": "W"},
        {"additional_options": {"availability": "{{ false }}"}},
    ],
    ids=["top-level", "in-section"],
)
async def test_restore_clears_options_added_after_the_snapshot(
    hass: HomeAssistant,
    component_core_client: HomeAssistantClient,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    added: dict[str, Any],
) -> None:
    """The form suggests the current values; a restore that sent them back
    would keep an option the snapshot did not have."""
    saved = dict(template_entry.options)
    path = await real_snapshot(manager, template_entry)
    hass.config_entries.async_update_entry(
        template_entry,
        options={**saved, "state": state_template("edited"), **added},
    )
    await hass.async_block_till_done()

    with _flow_starts(component_core_client) as opened:
        result = await manager.restore_snapshot(path.name)

    assert result["apply_status"] == "applied"
    opened.assert_awaited_once()
    assert dict(template_entry.options) == saved


async def test_restore_refuses_snapshot_whose_target_is_another_entry(
    hass: HomeAssistant, manager: bm.BackupManager, template_entry: ConfigEntry
) -> None:
    """A snapshot whose header names another entry must not be applied to
    either one."""
    path = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    edit_snapshot(path, lambda data: data.update(entity_id="different-entry"))

    with (
        received(hass, HELPERS_LIST) as reads,
        pytest.raises(bm.BackupRestoreError) as caught,
    ):
        await manager.restore_snapshot(path.name)

    assert caught.value.outcome["reason"] == "invalid_snapshot"
    assert caught.value.outcome["apply_status"] == "not_applied"
    assert reads[HELPERS_LIST] == []
    assert template_entry.options["state"] == state_template("before")


async def test_safety_capture_follows_renamed_entity_past_toggle_and_throttle(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot an older release keyed by the entity id, restored after the
    entity was renamed again: the safety capture still covers the entry,
    though auto-backup is off and a capture was just taken."""
    _renamed(entity_registry, template_entry, RENAMED)
    path = legacy_snapshot(
        manager, await real_snapshot(manager, template_entry), RENAMED
    )
    _renamed(entity_registry, template_entry, "sensor.now_renamed")
    await set_template_state(hass, template_entry, "before")
    assert await manager.maybe_snapshot(DOMAIN, template_entry.entry_id) is None, (
        "the throttle window is open"
    )
    monkeypatch.setattr(settings, "enable_auto_backup", False)

    result = await manager.restore_snapshot(path.name)

    assert result["entity_id"] == template_entry.entry_id
    safety = manager.read_snapshot(result["safety_backup"])
    assert safety["entity_id"] == template_entry.entry_id
    assert safety["config"]["options"]["state"] == state_template("before")
    assert template_entry.options["state"] == state_template("saved")


async def test_failed_restore_still_names_its_safety_backup(
    hass: HomeAssistant,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
    component_core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The options flow's answer is lost: the user needs the safety backup's
    name to undo whatever was applied."""
    path = await real_snapshot(manager, template_entry)
    await set_template_state(hass, template_entry, "before")
    monkeypatch.setattr(
        component_core_client,
        "submit_options_flow_step",
        AsyncMock(side_effect=HomeAssistantConnectionError("reply lost")),
    )

    with pytest.raises(bm.BackupRestoreError) as caught:
        await manager.restore_snapshot(path.name)

    assert caught.value.outcome["apply_status"] == "unknown"
    safety = caught.value.outcome["safety_backup"]
    assert manager.read_snapshot(safety)["entity_id"] == template_entry.entry_id
    assert stored_state(manager, safety) == state_template("before")


async def test_diff_compares_the_captured_entry_not_the_entity_id_now_reused(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    manager: bm.BackupManager,
    template_entry: ConfigEntry,
) -> None:
    """The entity id an older snapshot was keyed by now belongs to another
    Template helper: the diff still compares the snapshot's own entry."""
    _renamed(entity_registry, template_entry, RENAMED)
    path = legacy_snapshot(
        manager, await real_snapshot(manager, template_entry), RENAMED
    )
    _renamed(entity_registry, template_entry, "sensor.now_renamed")
    replacement = await create_template_sensor(hass, "replacement")
    _renamed(entity_registry, replacement, RENAMED)

    diff = await manager.diff_snapshot(path.name)

    assert diff["unchanged"] is True


async def _restart_button(hass: HomeAssistant) -> ConfigEntry:
    """A Template button whose device class, offered only at creation, is
    ``restart``."""
    assert await async_setup_component(hass, "config", {})
    flow = await hass.config_entries.flow.async_init(
        "template", context={"source": "user"}
    )
    await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"next_step_id": "button"}
    )
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": "Example", "device_class": "restart"}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


async def test_button_restore_clears_press_and_keeps_creation_only_device_class(
    hass: HomeAssistant, manager: bm.BackupManager
) -> None:
    """The button's options form has no device class: a restore that sent it
    would be refused, and one that kept the later press action would run it."""
    entry = await _restart_button(hass)
    saved = dict(entry.options)
    path = await real_snapshot(manager, entry)
    press = [{"action": "light.turn_on", "target": {"entity_id": "light.example"}}]
    hass.config_entries.async_update_entry(entry, options={**saved, "press": press})
    await hass.async_block_till_done()

    result = await manager.restore_snapshot(path.name)

    assert result["apply_status"] == "applied"
    assert dict(entry.options) == saved
    assert entry.options["device_class"] == "restart"
