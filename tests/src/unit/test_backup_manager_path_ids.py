"""Snapshot file names and histories of path-shaped ids (#2329).

A file's id is its path in Core's config directory, read through the File &
YAML Tools entry, so the id has characters a file name cannot carry. Two
different paths can clean to one name; the file name gets a digest of the
original id, and the snapshots an older release wrote under the bare cleaned
name must still be found, rotated and listed under the right id.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp import config

from ._backup_core import (
    SnapshotClock,
    place_snapshot,
    stem_of,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_clock as snapshot_clock
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files

NESTED = "www/x/y.css"
FLAT = "www/x_y.css"
# The bare stem an older release gave NESTED, and the one FLAT has today.
BARE_STEM = "www_x_y.css"


@pytest.fixture(autouse=True)
async def css_files(
    hass: HomeAssistant, file_snapshot_manager: bm.BackupManager
) -> None:
    await write_config_files(
        hass,
        {
            NESTED: "nested\n",
            FLAT: "flat\n",
            "www/foo\U0001f389bar.css": "party\n",
        },
    )


async def _capture(manager: bm.BackupManager, path: str) -> Path:
    captured = await manager.maybe_snapshot("file", path)
    assert captured is not None
    return captured


async def test_a_path_is_captured_as_a_file_in_the_backup_directory(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    captured = await _capture(file_snapshot_manager, NESTED)

    assert captured.parent == file_snapshot_manager.backup_dir
    held = file_snapshot_manager.read_snapshot(captured.name)
    assert held["entity_id"] == NESTED
    assert held["config"] == "nested\n"


async def test_characters_a_file_name_cannot_carry_are_replaced(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    captured = await _capture(file_snapshot_manager, "www/foo\U0001f389bar.css")

    assert stem_of(captured).startswith("www_foo_bar.css")
    assert captured.name.isascii()
    held = file_snapshot_manager.read_snapshot(captured.name)
    assert held["entity_id"] == "www/foo\U0001f389bar.css"


async def test_paths_that_clean_to_one_name_keep_separate_histories(
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    snapshot_clock: SnapshotClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing one name, captures in the same second overwrite each other and
    rotation counts both histories as one, so it can delete the only restore
    point of a file that was never written."""
    monkeypatch.setattr(backup_settings, "auto_backup_retain_per_entity", 1)

    nested = await _capture(file_snapshot_manager, NESTED)
    flat = await _capture(file_snapshot_manager, FLAT)

    assert stem_of(nested) != stem_of(flat)
    assert nested.exists() and flat.exists()
    assert file_snapshot_manager.read_snapshot(nested.name)["config"] == "nested\n"


async def test_the_same_path_always_gets_the_same_name(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """Rotation and listing look files up by this name, so it has to be stable."""
    first = await _capture(file_snapshot_manager, NESTED)
    second = await _capture(file_snapshot_manager, NESTED)

    assert stem_of(first) == stem_of(second)


async def test_rotation_ages_files_by_timestamp_not_by_stem(
    file_snapshot_manager: bm.BackupManager,
    backup_settings: config.Settings,
    snapshot_clock: SnapshotClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest stem sorts before the bare one on every file name whatever
    the timestamps say. Aged by name, rotation pruned the NEW history first:
    once the pre-digest files alone filled the cap, the snapshot just written
    was the one deleted."""
    monkeypatch.setattr(backup_settings, "auto_backup_retain_per_entity", 2)
    directory = file_snapshot_manager.backup_dir
    legacy_new = place_snapshot(
        directory, f"file.{BARE_STEM}.20260903_110000.yaml", NESTED
    )
    legacy_old = place_snapshot(
        directory, f"file.{BARE_STEM}.20260903_090000.yaml", NESTED
    )

    written = await _capture(file_snapshot_manager, NESTED)

    assert written.exists(), "the fresh pre-write snapshot must survive"
    assert legacy_new.exists()
    assert not legacy_old.exists(), "the oldest file is the one rotated out"


async def _collision(
    manager: bm.BackupManager,
) -> dict[str, Path]:
    """NESTED's histories under both stems, and FLAT's under the bare one.

    The bare stem is both what an older release gave NESTED and what FLAT has
    today, so the name alone cannot tell the two histories apart.
    """
    directory = manager.backup_dir
    return {
        "digest": await _capture(manager, NESTED),
        "legacy": place_snapshot(
            directory, f"file.{BARE_STEM}.20260903_110000.yaml", NESTED
        ),
        "other": place_snapshot(
            directory, f"file.{BARE_STEM}.20260903_100000.yaml", FLAT
        ),
    }


async def test_an_entity_filter_reads_the_payload_across_the_stem_collision(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A filtered listing is that entity's history, not its stem's: reporting
    FLAT's snapshots as NESTED's would have a bulk delete unlink them."""
    files = await _collision(file_snapshot_manager)

    for_nested = {
        m["name"] for m in file_snapshot_manager.list_snapshots(entity_id=NESTED)
    }
    for_flat = {m["name"] for m in file_snapshot_manager.list_snapshots(entity_id=FLAT)}

    assert for_nested == {files["digest"].name, files["legacy"].name}
    assert for_flat == {files["other"].name}


async def test_a_bulk_delete_by_entity_leaves_the_colliding_file_alone(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """Both spellings of NESTED's stem go, including the pre-digest one; FLAT,
    which shares the bare stem, keeps its only restore point."""
    files = await _collision(file_snapshot_manager)

    bulk = file_snapshot_manager.delete_bulk(entity_id=NESTED)

    assert set(bulk["deleted"]) == {files["digest"].name, files["legacy"].name}
    assert bulk["failed"] == []
    assert not files["digest"].exists()
    assert not files["legacy"].exists()
    assert files["other"].exists()


async def test_listing_orders_across_stems_by_timestamp(
    file_snapshot_manager: bm.BackupManager,
    snapshot_clock: SnapshotClock,
) -> None:
    """Newest first means by capture time, not by which stem the file has."""
    snapshot_clock.set("2026-09-03 12:00:00")
    files = await _collision(file_snapshot_manager)
    older = place_snapshot(
        file_snapshot_manager.backup_dir,
        f"file.{BARE_STEM}.20260903_090000.yaml",
        NESTED,
    )

    names = [m["name"] for m in file_snapshot_manager.list_snapshots(entity_id=NESTED)]

    assert names == [files["digest"].name, files["legacy"].name, older.name]


async def test_every_row_reports_the_id_its_payload_names(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A stem is a file name artefact; the row shows the entity. For the
    pre-digest file the stem is the bare cleaned name, which the filter refuses
    as ambiguous, so a row showing it would show a value that selects nothing,
    or the other entity."""
    files = await _collision(file_snapshot_manager)

    shown = {m["name"]: m["entity_id"] for m in file_snapshot_manager.list_snapshots()}

    assert shown[files["digest"].name] == NESTED
    assert shown[files["legacy"].name] == NESTED
    assert shown[files["other"].name] == FLAT


async def test_a_listed_id_selects_the_whole_history_when_sent_back(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """What a listing shows is what the settings UI and ha_manage_backup send
    back as the filter, so it has to select that entity's whole history, both
    stems, and nothing else, from whichever row it was read."""
    files = await _collision(file_snapshot_manager)
    shown = {m["name"]: m["entity_id"] for m in file_snapshot_manager.list_snapshots()}

    for row in (files["digest"].name, files["legacy"].name):
        listed = {
            m["name"]
            for m in file_snapshot_manager.list_snapshots(entity_id=shown[row])
        }
        assert listed == {files["digest"].name, files["legacy"].name}, row
    deleted = set(
        file_snapshot_manager.delete_bulk(entity_id=shown[files["legacy"].name])[
            "deleted"
        ]
    )

    assert deleted == {files["digest"].name, files["legacy"].name}
    assert files["other"].exists()


async def test_the_digest_stem_selects_only_its_own_entity(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A caller holding the digest stem (an older listing, a file name) gets
    that entity's post-digest files, reported under the id they belong to."""
    files = await _collision(file_snapshot_manager)

    rows = file_snapshot_manager.list_snapshots(entity_id=stem_of(files["digest"]))

    assert [m["name"] for m in rows] == [files["digest"].name]
    assert rows[0]["entity_id"] == NESTED


async def test_a_listing_identifies_a_snapshot_from_its_header_alone(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """Every listing row opens its file, so the read stops at ``config:``: a
    snapshot whose body is not even valid YAML still identifies itself. An
    unfiltered listing of hundreds of whole-file snapshots stays a header read
    per row, not a full parse."""
    bad = (
        file_snapshot_manager.backup_dir / "file.packages_foo.yaml.20260903_110000.yaml"
    )
    file_snapshot_manager.backup_dir.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "# ha_mcp_backup\n"
        "schema_version: 1\n"
        "domain: file\n"
        "entity_id: packages/foo.yaml\n"
        "captured: '2026-09-03T00:00:00+00:00'\n"
        "tool: t\n"
        "config: [this body never closes\n"
        "kind: text\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        file_snapshot_manager.read_snapshot(bad.name)
    rows = file_snapshot_manager.list_snapshots()

    assert [m["entity_id"] for m in rows] == ["packages/foo.yaml"]
    assert [
        m["name"]
        for m in file_snapshot_manager.list_snapshots(entity_id="packages/foo.yaml")
    ] == [bad.name]
