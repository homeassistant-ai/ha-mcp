"""The pre-#1579 ``.bak`` store, surfaced through the edits scope (#1579).

The legacy backups are real files in ``.ha_mcp_tools_backups/`` of Core's config
directory, listed and read by the File & YAML Tools entry's services. Without
the entry the store is unavailable, which the listing reports as a warning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import (
    add_label,
    break_backup_dir,
    manage_backup,
    snapshot_of,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files

BAK = "configuration.yaml.20260101_120000.bak"
LEGACY = f"legacy:{BAK}"


async def _legacy(hass: HomeAssistant, **files: str) -> None:
    """Legacy backups (file name: content) in the legacy directory."""
    await write_config_files(
        hass, {f".ha_mcp_tools_backups/{name}": body for name, body in files.items()}
    )


def _read(hass: HomeAssistant, path: str) -> str:
    return Path(hass.config.path(path)).read_text(encoding="utf-8")


async def test_without_the_tools_entry_the_legacy_store_is_empty_with_a_reason(
    snapshot_manager: bm.BackupManager,
) -> None:
    """The caller-token gates raise a structured error; the listing still works
    and the reason is the human message, not the raw JSON envelope (#1996)."""
    entries, reason = await snapshot_manager.list_legacy()

    assert entries == []
    assert reason
    assert not reason.startswith("{")


async def test_without_the_tools_entry_a_legacy_read_is_refused_with_that_reason(
    snapshot_manager: bm.BackupManager,
) -> None:
    _, reason = await snapshot_manager.list_legacy()

    with pytest.raises(ValueError) as caught:
        await snapshot_manager.read_legacy(BAK)

    assert reason and reason in str(caught.value)


async def test_legacy_entries_are_listed_under_a_synthetic_name_with_their_decode(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await _legacy(hass, **{BAK: "a: 1\n"})

    out, reason = await file_snapshot_manager.list_legacy()

    assert reason is None
    (entry,) = out
    assert entry["name"] == LEGACY
    assert entry["domain"] == "yaml_file"
    assert entry["source"] == "legacy"
    assert entry["entity_id"] == "configuration.yaml"
    assert entry["path_ambiguous"] is False


async def test_a_legacy_backup_is_read_with_its_content(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await _legacy(hass, **{BAK: "a: 1\n"})

    info = await file_snapshot_manager.read_legacy(BAK)

    assert info["content"] == "a: 1\n"


async def test_reading_a_legacy_backup_that_does_not_exist_is_file_not_found(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    with pytest.raises(FileNotFoundError):
        await file_snapshot_manager.read_legacy("x.20260101_120000.bak")


async def test_reading_a_legacy_backup_with_an_invalid_name_is_a_value_error(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    with pytest.raises(ValueError):
        await file_snapshot_manager.read_legacy("bad")


async def test_an_unambiguous_legacy_backup_diffs_against_the_current_file(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await write_config_files(hass, {"configuration.yaml": "a: 1\n"})
    await _legacy(hass, **{BAK: "a: 1\nb: 2\n"})

    diff = await file_snapshot_manager.diff_snapshot(LEGACY)

    assert diff["kind"] == "text"
    assert diff["entity_missing"] is False
    assert "b: 2" in diff["diff"]


async def test_a_legacy_backup_whose_path_is_ambiguous_has_no_live_target(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    """With no trustworthy live file the diff shows the stored content as a full
    add, and the live file is never read."""
    await _legacy(hass, **{"weird_name.20260101_120000.bak": "a: 1\n"})

    diff = await file_snapshot_manager.diff_snapshot(
        "legacy:weird_name.20260101_120000.bak"
    )

    assert diff["entity_missing"] is True


async def test_restoring_a_legacy_backup_replaces_the_file_after_a_safety_snapshot(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await write_config_files(hass, {"configuration.yaml": "a: OLD\n"})
    await _legacy(hass, **{BAK: "a: 1\n"})

    result = await file_snapshot_manager.restore_snapshot(LEGACY)

    assert _read(hass, "configuration.yaml") == "a: 1\n"
    assert result["restored_from"] == LEGACY
    assert result["domain"] == "yaml_file"
    assert result["entity_id"] == "configuration.yaml"
    # The safety snapshot is mandatory: it landed on disk, holding the prior content.
    (safety,) = file_snapshot_manager.backup_dir.glob("yaml_file.*.yaml")
    assert safety.name == result["safety_backup"]
    assert "a: OLD" in safety.read_text()


async def test_a_legacy_backup_with_an_ambiguous_path_is_not_restored(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await _legacy(hass, **{"weird_name.20260101_120000.bak": "a: 1\n"})

    with pytest.raises(ValueError, match="unambiguously"):
        await file_snapshot_manager.restore_snapshot(
            "legacy:weird_name.20260101_120000.bak"
        )


async def test_a_failed_safety_snapshot_blocks_a_legacy_restore(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The restore overwrites the whole config file, so unlike a per-key restore
    it fails closed: nothing is overwritten un-backed-up."""
    await write_config_files(hass, {"configuration.yaml": "a: OLD\n"})
    await _legacy(hass, **{BAK: "a: 1\n"})
    break_backup_dir(backup_settings, monkeypatch, tmp_path)

    with pytest.raises(bm.MandatoryBackupError):
        await file_snapshot_manager.restore_snapshot(LEGACY)

    assert _read(hass, "configuration.yaml") == "a: OLD\n"


async def test_the_merged_listing_has_both_stores(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
) -> None:
    await _legacy(hass, **{BAK: "a: 1\n"})
    edit = await snapshot_of(
        file_snapshot_manager, add_label(label_registry, "Kitchen")
    )

    entries, warnings = await file_snapshot_manager.list_edits_and_legacy()

    assert {e["name"] for e in entries} == {edit.name, LEGACY}
    assert warnings == []


async def test_a_filter_leaves_the_legacy_backups_out(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
) -> None:
    """Legacy backups map to ``yaml_file``, and their decoded path is not the
    sanitised form an entity filter matches on."""
    await _legacy(hass, **{BAK: "a: 1\n"})
    kitchen = add_label(label_registry, "Kitchen")
    edit = await snapshot_of(file_snapshot_manager, kitchen)

    by_domain, _ = await file_snapshot_manager.list_edits_and_legacy(domain="label")
    by_entity, _ = await file_snapshot_manager.list_edits_and_legacy(entity_id=kitchen)
    by_yaml_file, _ = await file_snapshot_manager.list_edits_and_legacy(
        domain="yaml_file"
    )

    assert [e["name"] for e in by_domain] == [edit.name]
    assert [e["name"] for e in by_entity] == [edit.name]
    assert [e["name"] for e in by_yaml_file] == [LEGACY]


async def test_a_full_edits_store_cannot_crowd_the_legacy_entries_out(
    hass: HomeAssistant,
    file_snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
) -> None:
    """Surfacing the few historical entries is the point of the merge."""
    await _legacy(
        hass, **{BAK: "a: 1\n", "configuration.yaml.20260102_120000.bak": "a: 2\n"}
    )
    for index in range(6):
        await snapshot_of(
            file_snapshot_manager, add_label(label_registry, f"Label {index}")
        )

    entries, _ = await file_snapshot_manager.list_edits_and_legacy(limit=4)

    names = {e["name"] for e in entries}
    assert len(entries) == 4
    assert {LEGACY, "legacy:configuration.yaml.20260102_120000.bak"} <= names


async def test_an_unavailable_legacy_store_is_a_warning_not_a_clean_listing(
    snapshot_manager: bm.BackupManager,
) -> None:
    """With the tools entry not set up the merged list must say the ``.bak``
    history is missing instead of reading as complete (#1996)."""
    entries, warnings = await snapshot_manager.list_edits_and_legacy()

    assert entries == []
    (warning,) = warnings
    assert "omitted" in warning


async def test_the_listing_tool_reports_an_unavailable_legacy_store_as_a_warning(
    core_client: HomeAssistantClient, snapshot_manager: bm.BackupManager
) -> None:
    """The top-level ``warnings`` contract: present when the legacy fetch is
    suppressed, absent when it is clean."""
    degraded = await manage_backup(core_client, scope="edits", action="list")

    assert degraded["success"] is True
    assert "omitted" in degraded["warnings"][0]


async def test_the_listing_tool_has_no_warnings_when_the_legacy_store_is_available(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    file_snapshot_manager: bm.BackupManager,
) -> None:
    clean = await manage_backup(core_client, scope="edits", action="list")

    assert clean["success"] is True
    assert "warnings" not in clean
