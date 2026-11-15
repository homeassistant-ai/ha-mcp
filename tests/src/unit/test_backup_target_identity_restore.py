"""A restore's safety backup holds the item the restore overwrites, and says
when there is none.

The restore writes a stored item by its id, while its backup is keyed by the
entity_id the item had, which a rename since can leave naming nothing or
another item (#2632). Items are created and renamed through Core, snapshots
are placed as an older capture left them, and the restore runs through
``ha_manage_backup``.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import add_label, captured_name, place_snapshot, snapshot_of
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_target_identity_core import (
    config_file_text,
    create_named,
    entity_of,
    failed_restore,
    move_entity,
    older_yaml_snapshot,
    place_item_snapshot,
    rejected_script_snapshot,
    rename_item,
    restore_of,
)
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._helper_tool_core import set_up, stored_item

_ITEMS = [
    pytest.param("helper_input_boolean", "input_boolean", {}, id="input_boolean"),
    pytest.param("zone", "zone", {"latitude": 1.0, "longitude": 2.0}, id="zone"),
]


def _snapshot_files(manager: bm.BackupManager) -> list[str]:
    return sorted(p.name for p in manager.backup_dir.glob("*.yaml"))


async def test_a_target_not_found_under_its_id_is_reported(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """Renamed since the backup, its current state is overwritten unsaved."""
    name = place_snapshot(
        file_snapshot_manager.backup_dir,
        "file.hall.20260521_120000.yaml",
        "www/hall.txt",
        held="restored\n",
    ).name

    result = await restore_of(core_client, name)

    assert "www/hall.txt" in result["warnings"][0]
    assert "No safety backup" in result["warnings"][0]
    assert result["data"]["safety_backup"] is None
    assert await config_file_text(hass, "www/hall.txt") == "restored\n"


async def test_a_recent_snapshot_does_not_stop_the_safety_backup(
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    label_registry: lr.LabelRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The throttle's snapshot holds an older state than the one replaced."""
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 60)
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="Edited")
    assert await snapshot_manager.maybe_snapshot("label", kitchen) is None

    result = await restore_of(core_client, path.name)

    safety = snapshot_manager.backup_dir / result["data"]["safety_backup"]
    assert captured_name(snapshot_manager, safety) == "Edited"


async def test_a_safety_backup_of_another_item_blocks_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Renamed since, the id can name another item: the item the restore
    overwrites would have no safety backup."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": "automation: !include automations.yaml\n",
            "automations.yaml": "- id: other\n  alias: Hall\n  trigger: []\n  action: []\n",
        },
    )
    await setup_from_configuration(hass, "automation")
    name = place_snapshot(
        snapshot_manager.backup_dir,
        "automation.hall.20260521_120000.yaml",
        "automation.hall",
        domain="automation",
        held={"id": "hall", "alias": "Hall lights", "trigger": [], "action": []},
        text=False,
    ).name

    reply, _ = await failed_restore(core_client, name)

    assert reply["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert "would hold other, not hall" in reply["error"]["message"]
    assert reply["data"]["apply_status"] == "not_applied"
    assert reply["data"]["safety_backup"] is None
    assert _snapshot_files(snapshot_manager) == [name]
    assert (await core_client.get_automation_config("automation.hall"))["id"] == "other"


@pytest.mark.parametrize(("domain", "platform", "fields"), _ITEMS)
async def test_the_safety_backup_follows_a_renamed_item(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    domain: str,
    platform: str,
    fields: dict[str, Any],
) -> None:
    """The backup's entity_id names item_a now and item_b was renamed; the
    restore writes item_b, so the safety backup holds item_b. A counter with
    the same storage id is another platform's item."""
    await set_up(hass, platform)
    await set_up(hass, "counter")
    await create_named(core_client, platform, "Item A", **fields)
    await create_named(core_client, platform, "Item B", **fields)
    assert await create_named(core_client, "counter", "Item B") == "item_b"
    await rename_item(core_client, platform, "item_b", "B2")
    move_entity(entity_registry, platform, "item_b", f"{platform}.renamed")
    move_entity(entity_registry, platform, "item_a", f"{platform}.item_b")
    name = place_item_snapshot(
        snapshot_manager.backup_dir,
        domain,
        f"{platform}.item_b",
        "item_b",
        "Item B",
        **fields,
    )

    result = await restore_of(core_client, name)

    held = snapshot_manager.read_snapshot(result["data"]["safety_backup"])["config"]
    assert (held["id"], held["name"]) == ("item_b", "B2")
    assert any("restore the safety_backup" in w for w in result["warnings"])
    assert (await stored_item(core_client, platform, "item_b"))["name"] == "Item B"
    assert (await stored_item(core_client, platform, "item_a"))["name"] == "Item A"


async def test_a_removed_zone_whose_entity_id_names_another_is_recreated(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Nothing is overwritten, so no backup is taken, of the other zone either."""
    await set_up(hass, "zone")
    fields = {"latitude": 1.0, "longitude": 2.0}
    porch = await create_named(core_client, "zone", "Porch", **fields)
    move_entity(entity_registry, "zone", porch, "zone.hall")
    name = place_item_snapshot(
        snapshot_manager.backup_dir, "zone", "zone.hall", "hall", "Hall", **fields
    )

    result = await restore_of(core_client, name)

    assert result["data"]["result"]["restore_mode"] == "recreated"
    assert result["data"]["safety_backup"] is None
    assert "zone hall no longer exists" in result["warnings"][0]
    assert _snapshot_files(snapshot_manager) == [name]
    assert (await stored_item(core_client, "zone", "hall"))["name"] == "Hall"
    assert (await stored_item(core_client, "zone", "porch"))["name"] == "Porch"


@pytest.mark.parametrize(("domain", "platform", "fields"), _ITEMS)
async def test_a_stored_item_without_a_registry_entry_is_backed_up(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    domain: str,
    platform: str,
    fields: dict[str, Any],
) -> None:
    """Removing its registry entry leaves the stored item, which the restore
    overwrites: it is backed up first, not reported gone. A zone resolves
    through its own fetcher, which takes the storage id too."""
    await set_up(hass, platform)
    await create_named(core_client, platform, "Item B", **fields)
    await rename_item(core_client, platform, "item_b", "B2")
    entity_registry.async_remove(entity_of(entity_registry, platform, "item_b"))
    # Keyed by its old entity_id: the backup must follow the stored id.
    name = place_item_snapshot(
        snapshot_manager.backup_dir,
        domain,
        f"{platform}.renamed",
        "item_b",
        "Item B",
        **fields,
    )

    result = await restore_of(core_client, name)

    held = snapshot_manager.read_snapshot(result["data"]["safety_backup"])["config"]
    assert held["name"] == "B2"
    assert not any("no longer exists" in w for w in result["warnings"])
    assert (await stored_item(core_client, platform, "item_b"))["name"] == "Item B"


async def test_a_stored_item_whose_id_names_another_entity_is_backed_up(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """Without its own registry entry, <type>.<id> can be another item's
    entity: the safety backup is taken by the stored id, so it holds the
    item the restore overwrites."""
    await set_up(hass, "input_boolean")
    await create_named(core_client, "input_boolean", "Item A")
    await create_named(core_client, "input_boolean", "Item B")
    await rename_item(core_client, "input_boolean", "item_b", "B2")
    entity_registry.async_remove(entity_of(entity_registry, "input_boolean", "item_b"))
    move_entity(entity_registry, "input_boolean", "item_a", "input_boolean.item_b")
    name = place_item_snapshot(
        snapshot_manager.backup_dir,
        "helper_input_boolean",
        "input_boolean.item_b",
        "item_b",
        "Item B",
    )

    result = await restore_of(core_client, name)

    held = snapshot_manager.read_snapshot(result["data"]["safety_backup"])["config"]
    assert (held["id"], held["name"]) == ("item_b", "B2")
    assert (await stored_item(core_client, "input_boolean", "item_b"))["name"] == (
        "Item B"
    )
    assert (await stored_item(core_client, "input_boolean", "item_a"))["name"] == (
        "Item A"
    )


async def test_an_unreadable_registry_blocks_it(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    backup_settings: config.Settings,
) -> None:
    """The lookup is part of the safety capture: its failure is one. Without
    Core's ``config`` commands the registry read is refused."""
    assert await async_setup_component(hass, "input_boolean", {})
    await create_named(core_client, "input_boolean", "Item B")
    await rename_item(core_client, "input_boolean", "item_b", "B2")
    manager = bm.get_backup_manager(core_client, backup_settings)
    name = place_item_snapshot(
        manager.backup_dir,
        "helper_input_boolean",
        "input_boolean.item_b",
        "item_b",
        "Item B",
    )

    reply, _ = await failed_restore(core_client, name)

    assert reply["error"]["code"] == "BACKUP_CAPTURE_FAILED"
    assert reply["data"]["apply_status"] == "not_applied"
    assert (await stored_item(core_client, "input_boolean", "item_b"))["name"] == "B2"


async def test_a_tool_error_on_apply_names_the_safety_backup(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The MCP reply of a tool error is its text, so the outcome must be in it."""
    name = await older_yaml_snapshot(
        hass, file_snapshot_manager, backup_settings, monkeypatch
    )

    reply, raised = await failed_restore(core_client, name)

    assert reply["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert reply["data"]["safety_backup"] is not None
    assert reply["data"]["apply_status"] == "unknown"
    assert isinstance(raised.__cause__, ToolError)
    assert "severity" not in await config_file_text(hass, "packages/alerts.yaml")


async def test_a_failed_apply_names_the_safety_backup(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """The write may have applied partly; the error keeps its own code."""
    await write_config_files(
        hass,
        {
            "configuration.yaml": "script: !include scripts.yaml\n",
            "scripts.yaml": "hall_lights:\n  alias: Hall\n  sequence: []\n",
        },
    )
    await setup_from_configuration(hass, "script")

    reply, _ = await failed_restore(
        core_client, rejected_script_snapshot(snapshot_manager.backup_dir)
    )

    assert reply["error"]["code"] == "VALIDATION_FAILED"
    assert reply["data"]["safety_backup"] is not None
    assert reply["data"]["apply_status"] == "unknown"


async def test_with_auto_backup_off_none_is_promised(
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    label_registry: lr.LabelRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)

    result = await restore_of(core_client, path.name)

    assert result["data"]["safety_backup"] is None
    assert "warnings" not in result


async def test_a_removed_helper_is_named_by_its_type(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """Its backup domain carries the helper_ prefix; the warning names the type."""
    await set_up(hass, "tag")
    name = place_item_snapshot(
        snapshot_manager.backup_dir, "helper_tag", "front_door", "front_door", "Door"
    )

    reply, _ = await failed_restore(core_client, name)

    warning = reply["data"]["warnings"][0]
    assert "helper_" not in warning
    assert "tag front_door no longer exists" in warning


@pytest.mark.parametrize(
    ("recorded", "unchanged"),
    [
        ({}, True),
        ({"registry_icon": "mdi:star"}, True),
        ({"registry_icon": "mdi:heart"}, False),
    ],
    ids=["from before registry icons", "same icon recorded", "other icon recorded"],
)
async def test_a_snapshot_of_the_current_registry_icon_shows_no_change(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    recorded: dict[str, Any],
    unchanged: bool,
) -> None:
    """A snapshot from before registry icons were recorded restores no icon, so
    the current one is no difference; a recorded icon is compared."""
    await set_up(hass, "input_boolean")
    await create_named(core_client, "input_boolean", "Item B")
    entity_registry.async_update_entity(
        entity_of(entity_registry, "input_boolean", "item_b"), icon="mdi:star"
    )
    name = place_item_snapshot(
        snapshot_manager.backup_dir,
        "helper_input_boolean",
        "input_boolean.item_b",
        "item_b",
        "Item B",
        **recorded,
    )

    result = await snapshot_manager.diff_snapshot(name)

    assert result["unchanged"] is unchanged, result
