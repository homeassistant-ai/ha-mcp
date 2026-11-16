"""Backup manager snapshots read back or named through Core.

Covers diffing a snapshot against what Home Assistant holds now (a label, an
``input_select`` helper and a file in Core's config directory changed after
their snapshot), and the snapshot file names and histories of path-shaped ids
(#2329): two paths can clean to one name, so the file name carries a digest of
the original id and snapshots an older release wrote under the bare cleaned
name must still be found, rotated and listed under the right id.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError

from ._backup_core import (
    SnapshotClock,
    add_label,
    place_snapshot,
    snapshot_of,
    stem_of,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import file_snapshot_manager as file_snapshot_manager
from ._backup_core import snapshot_clock as snapshot_clock
from ._backup_core import snapshot_manager as snapshot_manager
from ._config_dir_yaml import write_config_files
from ._helper_tool_core import create_item, set_up

MAX_PATCH_OPS = 200
NESTED = "www/x/y.css"
FLAT = "www/x_y.css"
# The bare stem an older release gave NESTED, and the one FLAT has today.
BARE_STEM = "www_x_y.css"

uses_css_files = pytest.mark.usefixtures("css_files")

# -- diffing a snapshot against live state ---


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


# -- path-shaped ids ---
@pytest.fixture
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


@uses_css_files
async def test_a_path_is_captured_as_a_file_in_the_backup_directory(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    captured = await _capture(file_snapshot_manager, NESTED)

    assert captured.parent == file_snapshot_manager.backup_dir
    held = file_snapshot_manager.read_snapshot(captured.name)
    assert held["entity_id"] == NESTED
    assert held["config"] == "nested\n"


@uses_css_files
async def test_characters_a_file_name_cannot_carry_are_replaced(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    captured = await _capture(file_snapshot_manager, "www/foo\U0001f389bar.css")

    assert stem_of(captured).startswith("www_foo_bar.css")
    assert captured.name.isascii()
    held = file_snapshot_manager.read_snapshot(captured.name)
    assert held["entity_id"] == "www/foo\U0001f389bar.css"


@uses_css_files
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


@uses_css_files
async def test_the_same_path_always_gets_the_same_name(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """Rotation and listing look files up by this name, so it has to be stable."""
    first = await _capture(file_snapshot_manager, NESTED)
    second = await _capture(file_snapshot_manager, NESTED)

    assert stem_of(first) == stem_of(second)


@uses_css_files
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


@uses_css_files
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


@uses_css_files
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


@uses_css_files
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


@uses_css_files
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


@uses_css_files
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


@uses_css_files
async def test_the_digest_stem_selects_only_its_own_entity(
    file_snapshot_manager: bm.BackupManager,
) -> None:
    """A caller holding the digest stem (an older listing, a file name) gets
    that entity's post-digest files, reported under the id they belong to."""
    files = await _collision(file_snapshot_manager)

    rows = file_snapshot_manager.list_snapshots(entity_id=stem_of(files["digest"]))

    assert [m["name"] for m in rows] == [files["digest"].name]
    assert rows[0]["entity_id"] == NESTED


@uses_css_files
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
