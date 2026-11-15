"""A renamed entity is still found by its entry id, and the restore keeps its locks.

The backup is keyed by the entity_id the entity had; after a rename the safety
backup and the diff must still find it by its entry id, and a restore holds the
locks of both entity_ids from before its safety backup through its write.
Renames and removals are made in Core's own registry, in the middle of a
restore where a test needs a writer the locks do not cover.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from ha_mcp import backup_manager as bm
from ha_mcp import config
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantError
from ha_mcp.utils.registry_update_lock import registry_update_lock

from ._backup_core import backup_settings as backup_settings
from ._backup_core import place_snapshot
from ._command_faults import received, unanswered
from ._entity_backup_core import (
    Desk,
    backup_dir,
    backup_files,
    before_command,
    entity_snapshot,
    first_unanswered,
    until,
    write_count,
    written,
)
from ._entity_backup_core import desk as desk
from ._entity_backup_core import entity_client as entity_client
from ._entity_backup_core import entity_handler as entity_handler
from ._entity_backup_core import entity_manager as entity_manager
from ._entity_backup_core import entity_path as entity_path
from ._entity_backup_core import lookup_command as lookup_command


def _rename(registry: er.EntityRegistry, new_entity_id: str) -> Callable[[], None]:
    def rename() -> None:
        registry.async_update_entity("light.desk", new_entity_id=new_entity_id)

    return rename


def _area(registry: er.EntityRegistry, entity_id: str = "light.desk") -> Any:
    entry = registry.async_get(entity_id)
    assert entry is not None
    return entry.area_id


@pytest.mark.parametrize("taken", [False, True])
async def test_a_rename_between_lookup_and_lock_writes_nothing(
    hass: HomeAssistant,
    entity_client: HomeAssistantClient,
    entity_handler: bm.DomainHandler,
    entity_registry: er.EntityRegistry,
    lookup_command: str,
    desk: Desk,
    taken: bool,
) -> None:
    """Without the re-read the update would go to whatever now holds the
    entity_id: nothing, or another entity."""
    snapshot = await entity_handler.fetch(entity_client, desk.entity_id)
    with written(hass) as seen, received(hass, lookup_command) as looked:
        async with registry_update_lock("entity", desk.entity_id):
            restore: asyncio.Future[Any] = asyncio.ensure_future(
                entity_handler.restore(entity_client, desk.entity_id, snapshot)
            )
            await until(lambda: bool(looked[lookup_command]))
            _rename(entity_registry, "light.lamp")()
            if taken:
                entity_registry.async_get_or_create(
                    "light", "hue", "other", suggested_object_id="desk"
                )

        with pytest.raises(bm.BackupRestoreError) as err:
            await restore

    assert err.value.outcome["reason"] == "entity_identity_mismatch"
    assert write_count(seen) == 0


@pytest.mark.parametrize("held_id", ["light.desk", "light.lamp"])
async def test_a_rename_back_waits_for_the_lock_of_either_entity_id(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    backup_settings: config.Settings,
    desk: Desk,
    held_id: str,
) -> None:
    """The restore holds both IDs from before its safety backup: while an edit
    holds either one, it takes no safety backup and writes nothing."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, new_entity_id="light.lamp", area_id=desk.kitchen
    )
    files = await asyncio.to_thread(backup_files, backup_dir(backup_settings))

    with written(hass) as seen:
        async with registry_update_lock("entity", held_id):
            restore = asyncio.create_task(entity_manager.restore_snapshot(path.name))
            done, _ = await asyncio.wait({restore}, timeout=0.5)
            files_while_held = await asyncio.to_thread(
                backup_files, backup_dir(backup_settings)
            )
            writes_while_held = write_count(seen)

        outcome = await restore

    assert not done
    assert files_while_held == files
    assert writes_while_held == 0
    assert outcome["safety_backup"] is not None
    assert _area(entity_registry) == desk.office


async def test_restore_backs_up_the_renamed_entity_first(
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, new_entity_id="light.lamp", area_id=desk.kitchen
    )

    outcome = await entity_manager.restore_snapshot(path.name)

    safety = entity_manager.read_snapshot(outcome["safety_backup"])
    assert (safety["entity_id"], safety["config"]["area_id"]) == (
        "light.lamp",
        desk.kitchen,
    )
    assert _area(entity_registry) == desk.office


async def test_a_rename_during_the_safety_backup_stops_the_restore(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The restore would find the entity by its entry id and overwrite it
    with no safety backup taken."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, area_id=desk.kitchen)

    with (
        written(hass) as seen,
        before_command(
            hass, "config/entity_registry/get", _rename(entity_registry, "light.lamp")
        ),
        pytest.raises(bm.MandatoryBackupError),
    ):
        await entity_manager.restore_snapshot(path.name)

    assert write_count(seen) == 0
    assert _area(entity_registry, "light.lamp") == desk.kitchen


async def test_an_edit_queued_during_the_safety_backup_waits_for_the_write(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The restore holds the entity lock from before its safety backup
    through its write, so an edit cannot land between the two: it lands after
    the restore and keeps its own name."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, area_id=desk.kitchen)
    edits: list[asyncio.Task[None]] = []

    async def edit() -> None:
        async with registry_update_lock("entity", desk.entity_id):
            entity_registry.async_update_entity(desk.entity_id, name="Edited")

    with before_command(
        hass,
        "config/entity_registry/get",
        lambda: edits.append(asyncio.create_task(edit())),
    ):
        await entity_manager.restore_snapshot(path.name)
    await asyncio.gather(*edits)

    entry = entity_registry.async_get(desk.entity_id)
    assert entry is not None
    assert (entry.area_id, entry.name) == (desk.office, "Edited")


async def test_a_rename_past_the_held_locks_stops_the_restore(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    lookup_command: str,
    desk: Desk,
) -> None:
    """Renamed to a third ID after the safety backup, by a writer the locks do
    not cover: the restore holds no lock on that ID and stops."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, area_id=desk.kitchen)

    # The lookups are: its locks, the safety backup's target, the write's.
    rename = _rename(entity_registry, "light.third")
    with (
        written(hass) as seen,
        before_command(hass, lookup_command, rename, nth=3),
        pytest.raises(bm.BackupRestoreError) as err,
    ):
        await entity_manager.restore_snapshot(path.name)

    assert err.value.outcome["reason"] == "entity_identity_mismatch"
    assert write_count(seen) == 0


async def test_a_lookup_that_gets_no_answer_keeps_the_restore_outcome(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entity_manager: bm.BackupManager,
    lookup_command: str,
    desk: Desk,
) -> None:
    path = await entity_snapshot(entity_manager, desk.entity_id)

    with written(hass) as seen:
        async with unanswered(hass, lookup_command, monkeypatch):
            with pytest.raises(HomeAssistantError) as err:
                await entity_manager.restore_snapshot(path.name)

    assert vars(err.value)["restore_outcome"]["restored_from"] == path.name
    assert write_count(seen) == 0


async def test_a_cancel_during_the_lock_lookup_writes_nothing(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    lookup_command: str,
    desk: Desk,
) -> None:
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, area_id=desk.kitchen)

    with written(hass) as seen:
        async with unanswered(hass, lookup_command, monkeypatch):
            with received(hass, lookup_command) as looked:
                restore = asyncio.create_task(
                    entity_manager.restore_snapshot(path.name)
                )
                await until(lambda: bool(looked[lookup_command]))
                restore.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await restore

    assert write_count(seen) == 0


@pytest.mark.parametrize("first", ["gets_no_answer", "finds_nothing"])
async def test_an_entry_not_locked_before_the_safety_backup_is_not_written(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    lookup_command: str,
    desk: Desk,
    first: str,
) -> None:
    """The lookup before the safety backup got no answer or found no entry,
    and a later one finds it: the restore refuses instead of writing it
    without its locks."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(desk.entity_id, area_id=desk.kitchen)

    async def restore() -> bm.BackupRestoreError:
        with written(hass) as seen, pytest.raises(bm.BackupRestoreError) as err:
            await entity_manager.restore_snapshot(path.name)
        assert write_count(seen) == 0
        return err.value

    if first == "gets_no_answer":
        async with first_unanswered(hass, lookup_command, monkeypatch):
            error = await restore()
    else:
        entity_registry.async_remove(desk.entity_id)

        # The entity comes back (Core gives its entry back) before the third lookup.
        def reappear() -> None:
            entity_registry.async_get_or_create(
                "light", "hue", "desk", suggested_object_id="desk"
            )

        with before_command(hass, lookup_command, reappear, nth=3):
            error = await restore()

    assert error.outcome["reason"] == "entity_identity_mismatch"
    assert error.outcome["verification_status"] == "mismatched"
    assert "could not be locked before its safety backup" in str(error)
    assert _area(entity_registry) == desk.kitchen


@pytest.mark.parametrize("missing", ["deleted", "none", "empty"])
async def test_a_backup_without_an_entity_id_is_refused(
    entity_manager: bm.BackupManager,
    backup_settings: config.Settings,
    desk: Desk,
    hass: HomeAssistant,
    missing: str,
) -> None:
    """Refused before its locks and its safety backup; before, a missing key
    raised a KeyError in the lock lookup and a None one let the restore go
    ahead."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    held = entity_manager.read_snapshot(path.name)["config"]
    if missing == "deleted":
        del held["entity_id"]
    else:
        held["entity_id"] = None if missing == "none" else ""
    name = "entity.light.desk.20260101_000000.yaml"
    place_snapshot(
        backup_dir(backup_settings),
        name,
        "light.desk",
        domain="entity",
        held=held,
        text=False,
    )
    files = await asyncio.to_thread(backup_files, backup_dir(backup_settings))

    with (
        written(hass) as seen,
        pytest.raises(bm.InvalidBackupSnapshotError, match="names no entity_id"),
    ):
        await entity_manager.restore_snapshot(name)

    assert await asyncio.to_thread(backup_files, backup_dir(backup_settings)) == files
    assert write_count(seen) == 0


async def test_diff_compares_with_the_renamed_entity(
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    path = await entity_snapshot(entity_manager, desk.entity_id)
    _rename(entity_registry, "light.lamp")()

    _, current = await entity_manager.snapshot_comparison(path.name)

    assert current is not None and current["entity_id"] == "light.lamp"


async def test_a_removed_entity_is_named_by_its_entity_id(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """The diff has no current entry, and the restore's warning names the
    entity, not its registry entry id."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_remove(desk.entity_id)

    _, current = await entity_manager.snapshot_comparison(path.name)
    with pytest.raises(bm.BackupRestoreError) as err:
        await entity_manager.restore_snapshot(path.name)

    assert current is None
    [warning] = err.value.outcome["warnings"]
    assert "light.desk" in warning and desk.entry_id not in warning


async def test_an_entity_now_holding_the_old_id_stops_the_rename_back(
    hass: HomeAssistant,
    entity_manager: bm.BackupManager,
    entity_registry: er.EntityRegistry,
    desk: Desk,
) -> None:
    """Checked under the lock, so the error names that entity and neither
    entity changes."""
    path = await entity_snapshot(entity_manager, desk.entity_id)
    entity_registry.async_update_entity(
        desk.entity_id, new_entity_id="light.lamp", area_id=desk.kitchen
    )
    entity_registry.async_get_or_create(
        "light", "hue", "other", suggested_object_id="desk"
    )

    with written(hass) as seen, pytest.raises(bm.BackupRestoreError) as err:
        await entity_manager.restore_snapshot(path.name)

    assert err.value.outcome["reason"] == "entity_id_collision"
    assert err.value.outcome["conflicting_entity_id"] == "light.desk"
    assert "light.desk" in str(err.value)
    assert write_count(seen) == 0
    assert _area(entity_registry, "light.lamp") == desk.kitchen
