"""A backup holds the item a write changes, says when it cannot, and Settings shows it.

The restore writes a stored item by its id, while its backup is keyed by the
entity_id the item had, which a rename since can leave naming nothing or
another item (#2632). ``ha_config_set_helper`` and ``ha_remove_helpers_integrations``
resolve a storage helper as ``stored_target`` does: a storage id first, else the
entity registry entry whose entity_id the target is (``<type>.`` optional), and
the snapshot resolves the same way. Items are created and renamed through Core,
snapshots are placed as an older capture left them, restores run through
``ha_manage_backup`` and the Settings route (a real server over ``core_client``),
and the route's reply goes to the page's script.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.setup import async_setup_component

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantAPIError, HomeAssistantClient
from ha_mcp.tools import tools_integrations
from ha_mcp.tools.tools_config_helpers import HelperConfigTools

from ._backup_core import add_label, captured_name, place_snapshot, snapshot_of
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._backup_restore_helpers import run_restore
from ._backup_target_identity_core import (
    config_file_text,
    create_named,
    create_tag,
    entity_of,
    failed_restore,
    held_by,
    move_entity,
    place_item_snapshot,
    rejected_script_snapshot,
    remove_tag,
    rename_item,
    restore_of,
    settings_restore,
    tag_ids,
    yaml_snapshot_whose_write_is_refused,
)
from ._config_dir_yaml import setup_from_configuration, write_config_files
from ._helper_tool_core import set_up, stored_item
from ._tool_errors import rejected_call_body

_ITEMS = [
    pytest.param("helper_input_boolean", "input_boolean", {}, id="input_boolean"),
    pytest.param("zone", "zone", {"latitude": 1.0, "longitude": 2.0}, id="zone"),
]


def _snapshot_files(manager: bm.BackupManager) -> list[str]:
    return sorted(p.name for p in manager.backup_dir.glob("*.yaml"))


async def _hall_and_porch(
    hass: HomeAssistant,
    client: HomeAssistantClient,
    registry: er.EntityRegistry,
) -> None:
    """Helper "hall" renamed to input_boolean.porch; helper "porch" now has the
    entity input_boolean.porch_2."""
    await set_up(hass, "input_boolean")
    hall = await create_named(client, "input_boolean", "Hall")
    porch = await create_named(client, "input_boolean", "Porch")
    move_entity(registry, "input_boolean", porch, "input_boolean.porch_2")
    move_entity(registry, "input_boolean", hall, "input_boolean.porch")


async def _tag_front_is_xyzs(hass: HomeAssistant, client: HomeAssistantClient) -> None:
    """Tag xyz, named "Front", has the entity tag.front; tag front, with
    tag.front_2, is removed again."""
    await set_up(hass, "tag")
    await create_tag(client, "xyz", "Front")
    await create_tag(client, "front")
    deleted = await client.send_websocket_message(
        {"type": "tag/delete", "tag_id": "front"}
    )
    assert deleted["success"], deleted


# -- the safety backup of a restore ------------------------------------------


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
    name = await yaml_snapshot_whose_write_is_refused(
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


# -- Settings shows what a restore reports -----------------------------------


def test_settings_shows_the_restore_warnings(settings_script: str) -> None:
    """Settings showed only the safety backup's name, never why it is missing."""
    reply = {
        "status": 200,
        "json": {
            "success": True,
            "data": {
                "safety_backup": None,
                "warnings": ["No safety backup was taken: nothing was found"],
                "result": {"warnings": ["The zone keeps its stored icon"]},
            },
        },
    }

    rendered = run_restore(settings_script, reply)

    assert "nothing was found" in rendered.alerts[0]
    assert "keeps its stored icon" in rendered.alerts[0]


async def test_settings_reports_a_failed_write_with_its_warnings(
    settings_script: str,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The route answered a failed write with a plain-text HTTP 500. The reply
    carries only the error's message, so the traceback is logged."""
    name = rejected_script_snapshot(snapshot_manager.backup_dir)

    with caplog.at_level(logging.ERROR):
        status, body = await settings_restore(core_client, name)
    rendered = run_restore(settings_script, {"status": status, "json": body}, name)

    assert status == 409
    assert body["data"]["apply_status"] == "unknown"
    assert "nothing was found under hall_lights" in rendered.alerts[0]
    logged = [r.exc_info[1] for r in caplog.records if r.exc_info]
    assert len(logged) == 1
    assert isinstance(logged[0], HomeAssistantAPIError)
    assert logged[0].status_code == 400


async def test_settings_keeps_a_structured_tool_error(
    hass: HomeAssistant,
    settings_script: str,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ToolError already says what failed and how to fix it; re-classified,
    Settings showed "An unexpected error occurred"."""
    name = await yaml_snapshot_whose_write_is_refused(
        hass, file_snapshot_manager, backup_settings, monkeypatch
    )

    status, body = await settings_restore(core_client, name)
    rendered = run_restore(settings_script, {"status": status, "json": body}, name)

    assert status == 409
    assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
    assert body["data"]["apply_status"] == "unknown"
    assert "too old" in " ".join(rendered.alerts)


def test_settings_names_a_recreated_helper_s_mapping_warning_once(
    settings_script: str,
) -> None:
    """Its outcome message already renders the result's warning."""
    note = "This snapshot has no entity mapping; the recreated helper may have a new entity ID."
    outcome = {
        "restore_mode": "recreated",
        "entity_id": "new-entry",
        "apply_status": "applied",
        "verification_status": "matched",
        "safety_backup": None,
        "result": {
            "entry_id": "new-entry",
            "entity_ids_restored": False,
            "warnings": [note],
        },
    }

    rendered = run_restore(
        settings_script, {"status": 200, "json": {"success": True, "data": outcome}}
    )

    assert rendered.alerts[0].count("may have a new entity ID") == 1


@pytest.mark.parametrize(
    "outcome",
    [
        # A flow helper's recreation carries restore_mode at the top level and
        # today returns no other warning; this pins that one would show.
        {
            "restore_mode": "recreated",
            "apply_status": "applied",
            "result": {"entry_id": "new-entry", "entity_ids_restored": True},
        },
        # A zone's recreation keeps restore_mode inside the result, as sent.
        {"result": {"id": "hall", "restore_mode": "recreated"}},
    ],
    ids=["flow-helper-with-mapping", "zone"],
)
def test_settings_keeps_a_recreated_item_s_own_warnings(
    settings_script: str, outcome: dict[str, Any]
) -> None:
    """Only the no-mapping warning is rendered by the outcome message."""
    outcome = {
        **outcome,
        "safety_backup": None,
        "result": {**outcome["result"], "warnings": ["Created with a warning"]},
    }

    rendered = run_restore(
        settings_script, {"status": 200, "json": {"success": True, "data": outcome}}
    )

    assert "Created with a warning" in rendered.alerts[0]


# -- a backup snapshots the helper the write changes -------------------------


@pytest.mark.parametrize(
    ("target", "helper"),
    [("porch", "porch"), ("input_boolean.porch", "hall")],
    ids=["storage-id-wins", "entity-id-is-looked-up"],
)
async def test_a_storage_id_wins_over_a_renamed_entity_with_its_text(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    target: str,
    helper: str,
) -> None:
    """The write changes "porch", whose entity is input_boolean.porch_2; only
    the entity_id form follows input_boolean.porch to the helper "hall"."""
    await _hall_and_porch(hass, core_client, entity_registry)

    held = await held_by(snapshot_manager, core_client, "helper_input_boolean", target)

    assert held is not None
    assert held["id"] == helper


async def test_an_entity_of_no_listed_helper_snapshots_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """The write fails on the registry's unique_id; the object id names
    another helper, which a snapshot of it would mislabel."""
    await set_up(hass, "input_boolean")
    porch = await create_named(core_client, "input_boolean", "Porch")
    move_entity(entity_registry, "input_boolean", porch, "input_boolean.porch_2")
    entity_registry.async_get_or_create(
        "input_boolean", "input_boolean", "yaml_only", suggested_object_id="porch"
    )

    held = await held_by(
        snapshot_manager, core_client, "helper_input_boolean", "input_boolean.porch"
    )

    assert held is None


async def test_an_entity_id_of_another_type_snapshots_nothing(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """The write takes counter.porch for input_boolean.counter.porch and
    fails; a snapshot of helper "porch" would back up a helper not written."""
    await set_up(hass, "input_boolean")
    await create_named(core_client, "input_boolean", "Porch")

    other_type = await held_by(
        snapshot_manager, core_client, "helper_input_boolean", "counter.porch"
    )
    own_type = await held_by(
        snapshot_manager, core_client, "helper_input_boolean", "input_boolean.porch"
    )

    assert other_type is None
    assert own_type is not None
    assert own_type["id"] == "porch"


async def test_a_helper_without_a_registry_entry_is_found_by_its_id(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
) -> None:
    """The writes find a stored id with or without a registry entry."""
    await set_up(hass, "input_boolean")
    porch = await create_named(core_client, "input_boolean", "Porch")
    entity_registry.async_remove(entity_of(entity_registry, "input_boolean", porch))

    held = await held_by(snapshot_manager, core_client, "helper_input_boolean", "porch")

    assert held is not None
    assert held["id"] == "porch"


async def test_a_tag_entity_id_with_no_entry_names_no_tag(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """tag.abc-1 is no entity (Core's is tag.abc_1); the id abc-1 is not guessed
    from it."""
    await set_up(hass, "tag")
    await create_tag(core_client, "abc-1")

    unguessed = await held_by(snapshot_manager, core_client, "helper_tag", "tag.abc-1")
    entity = await held_by(snapshot_manager, core_client, "helper_tag", "tag.abc_1")

    assert unguessed is None
    assert entity is not None
    assert entity["id"] == "abc-1"


async def test_a_repeated_bare_id_removal_deletes_no_other_tag(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _tag_front_is_xyzs(hass, core_client)

    body = await rejected_call_body(remove_tag(core_client, "front"))

    assert body["error"]["code"] == "ENTITY_NOT_FOUND"
    assert await tag_ids(core_client) == ["xyz"]


async def test_an_entity_id_removes_the_tag_its_entity_belongs_to(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    await _tag_front_is_xyzs(hass, core_client)

    result = await remove_tag(core_client, "tag.front")

    assert await tag_ids(core_client) == []
    assert result["entity_ids"] == ["tag.front"]


async def test_an_unreadable_registry_does_not_stop_the_removal(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entity only names the reply; the delete goes by the stored id. Without
    Core's ``config`` commands the registry read is refused."""
    assert await async_setup_component(hass, "tag", {})
    await create_tag(core_client, "front")
    waited = AsyncMock(wraps=tools_integrations.verify_entity_removed)
    monkeypatch.setattr(tools_integrations, "verify_entity_removed", waited)

    result = await remove_tag(core_client, "front", wait=True)

    assert await tag_ids(core_client) == []
    assert result["success"] is True
    assert result["entity_ids"] == []
    waited.assert_not_awaited()
    assert "registry could not be read" in result["warnings"][0]


async def test_a_readable_registry_names_the_entity_and_waits_for_its_removal(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The twin of the unreadable registry: the entity is named and waited on."""
    await set_up(hass, "tag")
    await create_tag(core_client, "front")
    waited = AsyncMock(wraps=tools_integrations.verify_entity_removed)
    monkeypatch.setattr(tools_integrations, "verify_entity_removed", waited)

    result = await remove_tag(core_client, "front", wait=True)

    assert await tag_ids(core_client) == []
    assert result["entity_ids"] == ["tag.front"]
    waited.assert_awaited_once()


async def test_an_unknown_tag_id_is_not_called_yaml_configured(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Tags have no YAML form; the id may be a name or a typo."""
    await set_up(hass, "tag")
    await create_tag(core_client, "front")

    error = (await rejected_call_body(remove_tag(core_client, "front_door")))["error"]

    assert error["code"] == "ENTITY_NOT_FOUND"
    assert "front_door" in error["message"]
    assert "YAML" not in error["message"] + error["suggestion"]
    assert await tag_ids(core_client) == ["front"]


async def test_an_update_by_a_storage_id_that_starts_with_the_type_finds_it(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """A helper named "Input Boolean Hall" has the storage id input_boolean_hall;
    without the dot, the prefix check took that id for an entity_id."""
    await set_up(hass, "input_boolean")
    item_id = await create_named(core_client, "input_boolean", "Input Boolean Hall")
    assert item_id == "input_boolean_hall"

    await HelperConfigTools(core_client).ha_config_set_helper(
        helper_type="input_boolean", helper_id=item_id, name="Hall"
    )

    assert (await stored_item(core_client, "input_boolean", item_id))["name"] == "Hall"
