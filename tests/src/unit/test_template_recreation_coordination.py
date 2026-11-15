"""Recreation protects a new entry from its first visibility through renaming.

The deleted helper is a Template sensor Core's config flow made; the restore
recreates it through Core's creation flow and renames its entity through
Core's entity registry, over the server's own client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ha_mcp import backup_manager as bm
from ha_mcp.tools import config_entry_flow

from ._held_command import held_command
from ._template_backup_core import manager as manager
from ._template_backup_core import settings as settings
from ._template_helper_core import state_template
from ._template_recovery_core import (
    DeletedTemplate,
    recreated_entry,
)
from ._template_recovery_core import deleted_template as deleted_template


async def _settle(*tasks: asyncio.Task[Any] | None) -> None:
    pending = [task for task in tasks if task is not None]
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def _pause_after_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the restore once Core created the entry, before the reply returns."""
    reached, release = asyncio.Event(), asyncio.Event()
    native = config_entry_flow.recreate_flow_helper

    async def create_then_wait(*args: Any, **kwargs: Any) -> Any:
        result = await native(*args, **kwargs)
        reached.set()
        await release.wait()
        return result

    monkeypatch.setattr(config_entry_flow, "recreate_flow_helper", create_then_wait)
    return reached, release


@pytest.mark.parametrize("stage", ["create_reply", "rename"])
async def test_edit_of_recreated_entry_waits_until_recreation_finishes(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """An edit of the new entry, visible in Core before the restore knows its
    id, must not land before the restore has verified and renamed it."""
    manager = deleted_template.manager
    attempted, entered = asyncio.Event(), asyncio.Event()

    async def edit(entry: ConfigEntry) -> None:
        attempted.set()
        async with manager.config_entry_write_guard(entry.entry_id):
            entered.set()
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, "state": state_template("edited")}
            )

    created, after_create = _pause_after_creation(monkeypatch)
    restore: asyncio.Task[dict[str, Any]] | None = None
    writer: asyncio.Task[None] | None = None
    async with held_command(hass, "config/entity_registry/update") as rename:
        if stage == "create_reply":
            rename.release.set()
            reached, release = created, after_create
        else:
            after_create.set()
            reached, release = rename.arrived, rename.release
        try:
            restore = asyncio.create_task(
                manager.restore_snapshot(deleted_template.name)
            )
            await asyncio.wait_for(reached.wait(), 2)
            entry = recreated_entry(hass)
            writer = asyncio.create_task(edit(entry))
            await attempted.wait()
            assert not entered.is_set(), "the new entry is still being restored"
            release.set()
            result = await asyncio.wait_for(restore, 2)
            await asyncio.wait_for(writer, 2)
        finally:
            after_create.set()
            rename.release.set()
            await _settle(restore, writer)

    assert result["verification_status"] == "matched"
    assert entry.options["state"] == state_template("edited")


async def test_recreation_waits_for_entry_writes_already_running_or_queued(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = deleted_template.manager
    first_entered, first_release = asyncio.Event(), asyncio.Event()
    second_attempted, second_entered = asyncio.Event(), asyncio.Event()
    second_release, restore_waiting = asyncio.Event(), asyncio.Event()
    native_guard = manager.config_entry_write_guard

    @asynccontextmanager
    async def guard(entity_id: str, *, exclusive: bool = False) -> AsyncIterator[None]:
        if exclusive:
            restore_waiting.set()
        async with native_guard(entity_id, exclusive=exclusive):
            yield

    async def write(
        entered: asyncio.Event,
        release: asyncio.Event,
        attempted: asyncio.Event | None = None,
    ) -> None:
        if attempted is not None:
            attempted.set()
        async with manager.config_entry_write_guard("other-entry"):
            entered.set()
            await release.wait()

    monkeypatch.setattr(manager, "config_entry_write_guard", guard)
    tasks: list[asyncio.Task[Any]] = [
        asyncio.create_task(write(first_entered, first_release))
    ]
    try:
        await asyncio.wait_for(first_entered.wait(), 2)
        tasks.append(
            asyncio.create_task(write(second_entered, second_release, second_attempted))
        )
        await second_attempted.wait()
        tasks.append(
            asyncio.create_task(manager.restore_snapshot(deleted_template.name))
        )
        await asyncio.wait_for(restore_waiting.wait(), 2)
        assert not hass.config_entries.async_entries("template"), (
            "a running entry write must finish first"
        )
        first_release.set()
        await asyncio.wait_for(second_entered.wait(), 2)
        assert not hass.config_entries.async_entries("template"), (
            "an already queued write keeps its turn"
        )
        second_release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 2)
    finally:
        first_release.set()
        second_release.set()
        await _settle(*tasks)

    assert recreated_entry(hass)


async def test_cancelled_recreation_releases_waiting_entry_write(
    hass: HomeAssistant,
    deleted_template: DeletedTemplate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = deleted_template.manager
    reached, _release = _pause_after_creation(monkeypatch)
    attempted, entered = asyncio.Event(), asyncio.Event()
    restore = asyncio.create_task(manager.restore_snapshot(deleted_template.name))

    async def write(entry_id: str) -> None:
        attempted.set()
        async with manager.config_entry_write_guard(entry_id):
            entered.set()

    writer: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(reached.wait(), 2)
        entry = recreated_entry(hass)
        writer = asyncio.create_task(write(entry.entry_id))
        await attempted.wait()
        assert not entered.is_set()
        restore.cancel()
        with pytest.raises(asyncio.CancelledError):
            await restore
        await asyncio.wait_for(writer, 2)
    finally:
        await _settle(restore, writer)

    assert entered.is_set()
    assert hass.config_entries.async_get_entry(entry.entry_id) is entry
    # The source is no longer pinned by the cancelled restore.
    manager.delete_snapshot(deleted_template.name)


async def test_unrelated_ordinary_writes_remain_parallel(
    manager: bm.BackupManager,
) -> None:
    entered = asyncio.Event()

    async def other() -> None:
        async with manager.config_entry_write_guard("another-entry"):
            entered.set()

    async with asyncio.timeout(2), manager.config_entry_write_guard("old-entry"):
        await asyncio.create_task(other())
    assert entered.is_set()


@pytest.mark.parametrize("exclusive", [False, True])
async def test_entry_guard_is_reentrant_for_its_task(
    manager: bm.BackupManager, exclusive: bool
) -> None:
    guard = manager.config_entry_write_guard
    async with (
        asyncio.timeout(2),
        guard("old-entry", exclusive=exclusive),
        guard("old-entry", exclusive=exclusive),
        guard("new-entry"),
    ):
        pass


async def test_cancelled_waiting_restore_does_not_block_new_writes(
    manager: bm.BackupManager,
) -> None:
    guard = manager.config_entry_write_guard
    attempted = asyncio.Event()

    async def restore() -> None:
        attempted.set()
        async with guard("old-entry", exclusive=True):
            pytest.fail("The existing write has not finished")

    async def another_write() -> None:
        async with guard("another-entry"):
            pass

    async with asyncio.timeout(2):
        async with guard("new-entry"):
            task = asyncio.create_task(restore())
            await attempted.wait()
            task.cancel()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.create_task(another_write())


async def test_nested_ordinary_write_can_finish_after_restore_queues(
    manager: bm.BackupManager,
) -> None:
    guard = manager.config_entry_write_guard
    attempted, entered = asyncio.Event(), asyncio.Event()

    async def restore() -> None:
        attempted.set()
        async with guard("old-entry", exclusive=True):
            entered.set()

    async with asyncio.timeout(2):
        async with guard("new-entry"):
            task = asyncio.create_task(restore())
            await attempted.wait()
            async with guard("new-entry"), guard("another-entry"):
                assert not entered.is_set()
        await task
        assert entered.is_set()


async def test_cancelled_queued_entry_guard_does_not_hold_up_restore(
    manager: bm.BackupManager,
) -> None:
    guard = manager.config_entry_write_guard
    attempted = asyncio.Event()

    async def queued_write() -> None:
        attempted.set()
        async with guard("old-entry"):
            pytest.fail("The original entry write has not finished")

    async def restore() -> None:
        async with guard("old-entry", exclusive=True):
            pass

    async with asyncio.timeout(2):
        async with guard("old-entry"):
            task = asyncio.create_task(queued_write())
            await attempted.wait()
            restoring = asyncio.create_task(restore())
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await restoring


async def test_guard_upgrade_refuses_instead_of_deadlocking(
    manager: bm.BackupManager,
) -> None:
    guard = manager.config_entry_write_guard
    async with asyncio.timeout(2), guard("old-entry"):
        with pytest.raises(RuntimeError, match="inside an entry write"):
            async with guard("old-entry", exclusive=True):
                pytest.fail("An ordinary write cannot upgrade its admission")


async def test_exclusive_restores_for_different_entries_serialize(
    manager: bm.BackupManager,
) -> None:
    guard = manager.config_entry_write_guard
    attempted, entered = asyncio.Event(), asyncio.Event()

    async def restore() -> None:
        attempted.set()
        async with guard("another-entry", exclusive=True):
            entered.set()

    async with asyncio.timeout(2):
        async with guard("old-entry", exclusive=True):
            task = asyncio.create_task(restore())
            await attempted.wait()
            assert not entered.is_set()
        await task
        assert entered.is_set()
