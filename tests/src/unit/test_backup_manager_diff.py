"""Comparing a snapshot with what Home Assistant holds now.

A label, an ``input_select`` helper and a file in Core's config directory are
changed through Core after their snapshot is taken, and the backup manager
diffs the snapshot against what the client reads back.
"""

from __future__ import annotations

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._backup_core import (
    add_label,
    place_snapshot,
    snapshot_of,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files
from ._helper_tool_core import create_item, set_up

MAX_PATCH_OPS = 200


async def test_an_unchanged_label_diffs_to_an_empty_patch(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)

    result = await snapshot_manager.diff_snapshot(path.name)

    assert result["kind"] == "dict"
    assert result["domain"] == "label"
    assert result["entity_id"] == kitchen
    assert result["entity_missing"] is False
    assert result["unchanged"] is True
    assert result["patch"] == []
    assert result["counts"] == {"add": 0, "remove": 0, "replace": 0, "total": 0}
    assert result["truncated"] is False
    assert result["captured_at"] is not None


async def test_a_renamed_label_reports_the_stored_name_as_a_replace(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    """The patch is what turns the live label back into the stored one."""
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="Pantry")

    result = await snapshot_manager.diff_snapshot(path.name)

    assert result["kind"] == "dict"
    assert result["unchanged"] is False
    assert {"op": "replace", "path": "/name", "value": "Kitchen"} in result["patch"]
    assert result["counts"]["replace"] >= 1


async def test_a_deleted_label_is_flagged_missing_not_unchanged(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    path = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_delete(kitchen)

    result = await snapshot_manager.diff_snapshot(path.name)

    assert result["kind"] == "dict"
    assert result["entity_missing"] is True
    # The empty patch comes from the absent target, not from a match: the
    # restore would re-create the label, not apply any operation.
    assert result["patch"] == []
    assert result["truncated"] is False
    assert result["unchanged"] is False
    assert result["captured_at"] is not None


async def test_a_diff_that_cannot_read_home_assistant_raises_instead_of_reading_missing(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    unreachable_client: HomeAssistantClient,
    backup_settings: config.Settings,
) -> None:
    path = await snapshot_of(snapshot_manager, add_label(label_registry, "Kitchen"))
    cut_off = bm.get_backup_manager(unreachable_client, backup_settings)

    with pytest.raises(HomeAssistantConnectionError):
        await cut_off.diff_snapshot(path.name)


async def test_a_diff_of_an_unknown_domain_is_a_lookup_error(
    snapshot_manager: bm.BackupManager,
) -> None:
    bogus = place_snapshot(
        snapshot_manager.backup_dir,
        "alien.x.20260521_120000.yaml",
        "x",
        domain="alien",
        held={"v": 1},
        text=False,
    )

    with pytest.raises(LookupError):
        await snapshot_manager.diff_snapshot(bogus.name)


async def test_a_diff_of_a_missing_snapshot_is_file_not_found(
    snapshot_manager: bm.BackupManager,
) -> None:
    with pytest.raises(FileNotFoundError):
        await snapshot_manager.diff_snapshot("label.x.20260521_120000.yaml")


async def test_a_diff_larger_than_the_cap_is_cut_short_and_flagged(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    snapshot_manager: bm.BackupManager,
) -> None:
    """A rewritten 250-option select must not flood the tool response."""
    await set_up(hass, "input_select")
    many = [f"option {i}" for i in range(MAX_PATCH_OPS + 50)]
    item_id = await create_item(core_client, "input_select", {"options": many})
    captured = await snapshot_manager.maybe_snapshot("helper_input_select", item_id)
    assert captured is not None
    updated = await core_client.send_websocket_message(
        {
            "type": "input_select/update",
            "input_select_id": item_id,
            "name": "Shrunk",
            "options": ["only"],
        }
    )
    assert updated["success"], updated

    result = await snapshot_manager.diff_snapshot(captured.name)

    assert result["kind"] == "dict"
    assert result["truncated"] is True
    assert len(result["patch"]) == MAX_PATCH_OPS


async def _file_snapshot(
    manager: bm.BackupManager, hass: HomeAssistant, body: str
) -> str:
    await write_config_files(hass, {"www/x.css": body})
    captured = await manager.maybe_snapshot("file", "www/x.css", force=True)
    assert captured is not None
    return captured.name


async def test_a_file_snapshot_is_text_and_a_label_snapshot_is_not(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
) -> None:
    """The kind marker picks the diff a snapshot gets: a unified text diff for
    file content, a JSON patch for a structured config."""
    name = await _file_snapshot(file_snapshot_manager, hass, "file body\n")
    label = await snapshot_of(
        file_snapshot_manager, add_label(label_registry, "Kitchen")
    )

    text = yaml.safe_load((file_snapshot_manager.backup_dir / name).read_text())
    structured = yaml.safe_load(label.read_text())

    assert text["kind"] == "text"
    assert text["config"] == "file body\n"
    assert "kind" not in structured


async def test_a_changed_file_diffs_as_unified_text(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    name = await _file_snapshot(file_snapshot_manager, hass, "a\nb\nc\n")
    await write_config_files(hass, {"www/x.css": "a\nB\nc\n"})

    diff = await file_snapshot_manager.diff_snapshot(name)

    assert diff["kind"] == "text"
    assert diff["unchanged"] is False
    assert "-B" in diff["diff"] and "+b" in diff["diff"]


async def test_an_unchanged_file_diffs_as_unchanged(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    name = await _file_snapshot(file_snapshot_manager, hass, "a\nb\nc\n")

    diff = await file_snapshot_manager.diff_snapshot(name)

    assert diff["kind"] == "text"
    assert diff["unchanged"] is True
