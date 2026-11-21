"""The pre-write snapshot: when it is taken, how many are kept, what stops it.

Snapshots are taken of a real label in Core's registry through the backup
manager's public calls; Core answers the registry reads. A failed read is the
client getting no answer (a refused connection), and a failed disk write is a
backup directory that cannot be created.
"""

from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.helpers import label_registry as lr

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient

from ._backup_core import (
    SnapshotClock,
    add_label,
    break_backup_dir,
    captured_name,
    snapshot_of,
    stem_of,
)
from ._backup_core import backup_settings as backup_settings
from ._backup_core import snapshot_clock as snapshot_clock
from ._backup_core import snapshot_manager as snapshot_manager


def _files(manager: bm.BackupManager, pattern: str = "*.yaml") -> list[Path]:
    return (
        sorted(manager.backup_dir.glob(pattern)) if manager.backup_dir.exists() else []
    )


async def test_disabled_means_no_snapshot(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)

    assert await snapshot_manager.maybe_snapshot("label", kitchen) is None

    assert _files(snapshot_manager) == []


async def test_unknown_domain_is_skipped(snapshot_manager: bm.BackupManager) -> None:
    assert await snapshot_manager.maybe_snapshot("unknown_domain", "x") is None


async def test_empty_entity_id_is_skipped(snapshot_manager: bm.BackupManager) -> None:
    """A create call has no id yet: there is nothing to back up."""
    assert await snapshot_manager.maybe_snapshot("label", "") is None
    assert _files(snapshot_manager) == []


async def test_an_entity_core_does_not_hold_is_skipped(
    snapshot_manager: bm.BackupManager,
) -> None:
    """A new entity has no prior state; the write goes ahead unbacked."""
    assert await snapshot_manager.maybe_snapshot("label", "missing") is None
    assert _files(snapshot_manager) == []


async def test_unreachable_home_assistant_does_not_block_the_write(
    unreachable_client: HomeAssistantClient, backup_settings: config.Settings
) -> None:
    """Capture is best-effort: a read that gets no answer is swallowed."""
    manager = bm.get_backup_manager(unreachable_client, backup_settings)

    assert await manager.maybe_snapshot("label", "x", tool_name="t") is None


async def test_a_programming_error_in_a_fetch_is_not_swallowed(
    snapshot_manager: bm.BackupManager,
) -> None:
    """A bug in a handler must surface, not hide behind the best-effort capture."""

    async def fetch(client: Any, entity_id: str) -> Any:
        return await client.get_label_config(entity_id)

    async def restore(client: Any, entity_id: str, held: Any) -> Any:
        return None

    snapshot_manager.register(bm.DomainHandler("label", fetch, restore))

    with pytest.raises(AttributeError):
        await snapshot_manager.maybe_snapshot("label", "x", tool_name="t")


async def test_successful_capture_writes_the_label_as_core_holds_it(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")

    path = await snapshot_of(snapshot_manager, kitchen, tool_name="ha_config_set_label")

    data = yaml.safe_load(path.read_text())
    assert data["schema_version"] == bm.SCHEMA_VERSION
    assert data["domain"] == "label"
    assert data["entity_id"] == kitchen
    assert data["tool"] == "ha_config_set_label"
    assert data["config"]["name"] == "Kitchen"
    assert data["config"]["label_id"] == kitchen


async def test_an_entity_id_is_its_own_file_name_stem(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    """An id that needs no cleaning keeps its name, so snapshots taken by
    earlier releases stay discoverable."""
    kitchen = add_label(label_registry, "Kitchen")

    path = await snapshot_of(snapshot_manager, kitchen)

    assert stem_of(path) == kitchen


async def test_throttle_blocks_a_second_capture_of_the_same_entity(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 10)
    kitchen = add_label(label_registry, "Kitchen")

    first = await snapshot_manager.maybe_snapshot("label", kitchen)
    second = await snapshot_manager.maybe_snapshot("label", kitchen)

    assert first is not None
    assert second is None
    assert _files(snapshot_manager) == [first]


async def test_throttle_is_per_entity(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 10)
    alpha = add_label(label_registry, "Alpha")
    beta = add_label(label_registry, "Beta")

    assert await snapshot_manager.maybe_snapshot("label", alpha) is not None
    assert await snapshot_manager.maybe_snapshot("label", beta) is not None


async def test_without_a_throttle_every_capture_is_kept(
    snapshot_manager: bm.BackupManager, label_registry: lr.LabelRegistry
) -> None:
    kitchen = add_label(label_registry, "Kitchen")

    first = await snapshot_of(snapshot_manager, kitchen)
    second = await snapshot_of(snapshot_manager, kitchen)

    assert first != second
    assert _files(snapshot_manager) == sorted([first, second])


async def test_concurrent_captures_of_one_entity_write_one_snapshot(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two writes racing on one entity inside the throttle window take one
    snapshot between them, not two that both saw no earlier one."""
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 10)
    kitchen = add_label(label_registry, "Kitchen")

    results = await asyncio.gather(
        snapshot_manager.maybe_snapshot("label", kitchen),
        snapshot_manager.maybe_snapshot("label", kitchen),
    )

    assert len([r for r in results if r is not None]) == 1
    assert len(_files(snapshot_manager)) == 1


async def test_throttle_forgets_the_oldest_entities_past_the_cap(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-running server editing many entities does not grow its throttle
    bookkeeping without bound: the entities captured longest ago drop out."""
    monkeypatch.setattr(bm, "TRACKER_SOFT_CAP", 5)
    monkeypatch.setattr(bm, "TRACKER_PRUNE_BATCH", 2)
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 10)
    labels = [add_label(label_registry, f"Label {i}") for i in range(7)]
    for label_id in labels:
        await snapshot_of(snapshot_manager, label_id)

    assert await snapshot_manager.maybe_snapshot("label", labels[0]) is not None
    assert await snapshot_manager.maybe_snapshot("label", labels[6]) is None


async def test_throttle_keeps_every_entity_under_the_cap(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 10)
    labels = [add_label(label_registry, f"Label {i}") for i in range(7)]
    for label_id in labels:
        await snapshot_of(snapshot_manager, label_id)

    assert await snapshot_manager.maybe_snapshot("label", labels[0]) is None


async def test_two_captures_in_one_second_keep_both_states(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    snapshot_clock: SnapshotClock,
) -> None:
    """The throttle defaults to 0 and the file name's timestamp is a second.

    A save then a delete of one blueprint, or two rapid edits of one
    automation, land inside a second; with one name for both the atomic
    replace discarded the earlier pre-write state. The later captures take a
    suffixed name that still sorts newest-last and reports the same second.
    """
    snapshot_clock.set("2026-09-03 12:00:00")
    kitchen = add_label(label_registry, "v1")
    first = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="v2")
    second = await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="v3")
    third = await snapshot_of(snapshot_manager, kitchen)

    assert len({first.name, second.name, third.name}) == 3
    assert [captured_name(snapshot_manager, p) for p in (first, second, third)] == [
        "v1",
        "v2",
        "v3",
    ]
    listed = snapshot_manager.list_snapshots(entity_id=kitchen)
    assert [m["name"] for m in listed] == [third.name, second.name, first.name]
    assert {m["timestamp"] for m in listed} == {"20260903_120000"}


async def test_same_second_suffix_counts_toward_rotation(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    snapshot_clock: SnapshotClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Suffixed names are ordinary snapshots: rotation prunes the oldest of
    them and the entity's own retention cap still holds."""
    monkeypatch.setattr(backup_settings, "auto_backup_retain_per_entity", 2)
    snapshot_clock.set("2026-09-03 12:00:00")
    kitchen = add_label(label_registry, "Kitchen")

    first = await snapshot_of(snapshot_manager, kitchen)
    await snapshot_of(snapshot_manager, kitchen)
    await snapshot_of(snapshot_manager, kitchen)

    assert not first.exists()
    assert len(_files(snapshot_manager)) == 2


@pytest.mark.parametrize("clock_rollback", [False, True])
async def test_new_capture_survives_older_filename_order(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    snapshot_clock: SnapshotClock,
    monkeypatch: pytest.MonkeyPatch,
    clock_rollback: bool,
) -> None:
    """A returned recovery point survives gap reuse and wall-clock rollback."""
    monkeypatch.setattr(backup_settings, "auto_backup_retain_per_entity", 1)
    snapshot_clock.set("2026-09-10 12:00:00")
    kitchen = add_label(label_registry, "previous")
    await snapshot_of(snapshot_manager, kitchen)
    if clock_rollback:
        snapshot_clock.set("2026-09-09 12:00:00")
    else:
        # The second capture takes _01 and rotates away the base name.
        await snapshot_of(snapshot_manager, kitchen)
    label_registry.async_update(kitchen, name="fresh")

    fresh = await snapshot_of(snapshot_manager, kitchen, mandatory=True)

    assert fresh.exists()
    assert captured_name(snapshot_manager, fresh) == "fresh"
    assert [row["name"] for row in snapshot_manager.list_snapshots()] == [fresh.name]


async def test_rotation_removes_the_oldest(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    snapshot_clock: SnapshotClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "auto_backup_retain_per_entity", 3)
    snapshot_clock.set("2026-09-03 12:00:00")
    kitchen = add_label(label_registry, "v0")
    captured = []
    for version in range(5):
        label_registry.async_update(kitchen, name=f"v{version}")
        captured.append(await snapshot_of(snapshot_manager, kitchen))
        snapshot_clock.tick()

    kept = [path for path in captured if path.exists()]

    assert [captured_name(snapshot_manager, p) for p in kept] == ["v2", "v3", "v4"]


async def test_rotation_does_not_touch_other_entities(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "auto_backup_retain_per_entity", 2)
    alpha = add_label(label_registry, "Alpha")
    beta = add_label(label_registry, "Beta")
    for _ in range(3):
        await snapshot_of(snapshot_manager, alpha)
    await snapshot_of(snapshot_manager, beta)

    assert len(snapshot_manager.list_snapshots(entity_id=alpha)) == 2
    assert len(snapshot_manager.list_snapshots(entity_id=beta)) == 1


async def test_a_broken_backup_directory_turns_capture_off(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    break_backup_dir(backup_settings, monkeypatch, tmp_path)
    add_label(label_registry, "Kitchen")

    await snapshot_manager.list_edits_and_legacy(domain="label")

    assert snapshot_manager.enabled is False
    assert (snapshot_manager.init_dir_error or "").startswith("NotADirectoryError")


async def test_force_bypasses_the_disabled_toggle(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An on-demand capture is an explicit request, so the toggle does not apply."""
    monkeypatch.setattr(backup_settings, "enable_auto_backup", False)
    kitchen = add_label(label_registry, "Kitchen")

    assert await snapshot_manager.maybe_snapshot("label", kitchen) is None
    forced = await snapshot_of(snapshot_manager, kitchen, force=True)

    assert forced.exists()


async def test_force_bypasses_the_throttle_window(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(backup_settings, "auto_backup_throttle_minutes", 1)
    kitchen = add_label(label_registry, "Kitchen")

    assert await snapshot_manager.maybe_snapshot("label", kitchen) is not None
    assert await snapshot_manager.maybe_snapshot("label", kitchen) is None
    assert await snapshot_manager.maybe_snapshot("label", kitchen, force=True)


async def test_force_still_skips_an_unregistered_domain(
    snapshot_manager: bm.BackupManager,
) -> None:
    assert await snapshot_manager.maybe_snapshot("unknown", "foo", force=True) is None


async def test_force_still_skips_an_entity_core_does_not_hold(
    snapshot_manager: bm.BackupManager,
) -> None:
    assert await snapshot_manager.maybe_snapshot("label", "gone", force=True) is None


async def test_force_still_fails_for_an_unwritable_directory(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    break_backup_dir(backup_settings, monkeypatch, tmp_path)
    await snapshot_manager.list_edits_and_legacy(domain="label")

    assert snapshot_manager.init_dir_error is not None
    assert await snapshot_manager.maybe_snapshot("label", kitchen, force=True) is None


async def test_a_forced_mandatory_capture_fails_when_the_directory_cannot_be_made(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Force retries the directory after an earlier failure; when that retry
    fails too, a mandatory capture is a failure, not a skip."""
    kitchen = add_label(label_registry, "Kitchen")
    break_backup_dir(backup_settings, monkeypatch, tmp_path)
    await snapshot_manager.list_edits_and_legacy(domain="label")
    assert snapshot_manager.init_dir_error is not None

    with pytest.raises(bm.MandatoryBackupError):
        await snapshot_manager.maybe_snapshot(
            "label", kitchen, mandatory=True, force=True
        )


@pytest.mark.parametrize("mandatory", [False, True])
@pytest.mark.parametrize("force", [False, True])
async def test_capture_retries_a_repaired_directory(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    backup_settings: config.Settings,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mandatory: bool,
    force: bool,
) -> None:
    kitchen = add_label(label_registry, "Kitchen")
    blocker = break_backup_dir(backup_settings, monkeypatch, tmp_path)

    with pytest.raises(bm.MandatoryBackupError, match="NotADirectoryError"):
        await snapshot_manager.maybe_snapshot("label", kitchen, mandatory=True)
    assert snapshot_manager.init_dir_error is not None
    blocker.unlink()

    recovered = await snapshot_manager.maybe_snapshot(
        "label", kitchen, force=force, mandatory=mandatory
    )

    assert recovered is not None and recovered.exists()
    assert captured_name(snapshot_manager, recovered) == "Kitchen"
    assert snapshot_manager.init_dir_error is None
    assert snapshot_manager.enabled is True


async def test_mandatory_capture_raises_when_the_read_fails(
    unreachable_client: HomeAssistantClient, backup_settings: config.Settings
) -> None:
    manager = bm.get_backup_manager(unreachable_client, backup_settings)

    with pytest.raises(bm.MandatoryBackupError):
        await manager.maybe_snapshot("label", "x", mandatory=True, force=True)


async def test_mandatory_capture_raises_when_the_disk_is_full(
    snapshot_manager: bm.BackupManager,
    label_registry: lr.LabelRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full disk is carried with its remediation for the structured error."""
    kitchen = add_label(label_registry, "Kitchen")
    operation = "rename" if os.name == "nt" else "link"
    native_publish = getattr(os, operation)

    def publish(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(os.fsdecode(dst)).parent == snapshot_manager.backup_dir:
            raise OSError(errno.ENOSPC, "No space left on device")
        native_publish(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, operation, publish)

    with pytest.raises(bm.MandatoryBackupError) as exc:
        await snapshot_manager.maybe_snapshot(
            "label", kitchen, mandatory=True, force=True
        )

    assert exc.value.suggestions
    assert "No space left on device" in str(exc.value)


async def test_mandatory_capture_of_a_new_entity_is_a_skip_not_a_failure(
    snapshot_manager: bm.BackupManager,
) -> None:
    """Nothing to snapshot is legitimate: the write may go ahead."""
    assert (
        await snapshot_manager.maybe_snapshot(
            "label", "new", mandatory=True, force=True
        )
        is None
    )


async def test_supported_domains_are_sorted_with_flow_helpers_last(
    snapshot_manager: bm.BackupManager,
) -> None:
    domains = snapshot_manager.supported_domains()

    assert domains[-1] == "helper_<flow helper type>"
    assert domains[:-1] == sorted(domains[:-1])
    assert {"automation", "label", "zone"} <= set(domains)


async def test_a_manager_without_handlers_supports_only_flow_helpers(
    core_client: HomeAssistantClient, backup_settings: config.Settings
) -> None:
    empty = bm.BackupManager(backup_settings, core_client)

    assert empty.supported_domains() == ["helper_<flow helper type>"]


async def test_the_manager_is_cached_on_its_client(
    core_client: HomeAssistantClient, backup_settings: config.Settings
) -> None:
    assert bm.get_backup_manager(core_client, backup_settings) is (
        bm.get_backup_manager(core_client, backup_settings)
    )


async def test_default_handlers_cover_every_write_tool_domain(
    snapshot_manager: bm.BackupManager,
) -> None:
    covered = set(snapshot_manager.supported_domains())

    assert covered >= {
        "automation",
        "script",
        "scene",
        "dashboard",
        "label",
        "category",
        "group",
        "zone",
        "area_or_floor",
        "todo_item",
        "calendar_event",
        "entity",
        "device",
        "integration",
        "helper_input_boolean",
        "helper_timer",
    }
