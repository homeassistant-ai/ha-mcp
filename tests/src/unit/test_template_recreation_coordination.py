"""Recreation protects a new entry from its first visibility through verification."""

import asyncio
from contextlib import asynccontextmanager, suppress
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm

from .test_template_deleted_recovery import recovery as recovery


async def _cancel_tasks(*tasks):
    for task in tasks:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, bm.BackupRestoreError):
                await task


@pytest.mark.parametrize("stage", ["create_reply", "rename", "verification"])
@pytest.mark.parametrize("mutation", ["edit", "delete"])
async def test_new_entry_writes_wait_until_recreation_finishes(
    recovery, monkeypatch, stage, mutation
):
    reached = asyncio.Event()
    release = asyncio.Event()
    attempted = asyncio.Event()
    entered = asyncio.Event()
    original_create = recovery.create.side_effect
    original_send = bm._ws_send
    original_verify = bm._verify_template_restore

    async def pause():
        reached.set()
        await release.wait()

    async def create(*args):
        result = await original_create(*args)
        if stage == "create_reply":
            await pause()  # HA exposes the new entry before the response returns.
        return result

    async def send(client, message):
        if stage == "rename" and message["type"] == "config/entity_registry/update":
            await pause()
        return await original_send(client, message)

    async def verify(*args):
        if stage == "verification":
            await pause()
        return await original_verify(*args)

    async def write():
        attempted.set()
        async with recovery.manager.config_entry_write_guard("new-entry"):
            entered.set()
            if mutation == "edit":
                recovery.state.records[0]["options"]["state"] = "{{ 99 }}"
            else:
                recovery.state.entries.clear()
                recovery.state.records.clear()
                recovery.state.registry.clear()

    recovery.create.side_effect = create
    monkeypatch.setattr(bm, "_ws_send", AsyncMock(side_effect=send))
    monkeypatch.setattr(bm, "_verify_template_restore", verify)
    restore = asyncio.create_task(recovery.manager.restore_snapshot(recovery.name))
    writer = None
    try:
        async with asyncio.timeout(2):
            await reached.wait()
            assert recovery.state.entries[0]["entry_id"] == "new-entry"
            writer = asyncio.create_task(write())
            await attempted.wait()
            assert not entered.is_set(), "The replacement entry is still being restored"
            release.set()
            result = await restore
            await writer
        assert result["verification_status"] == "matched"
        assert entered.is_set()
    finally:
        release.set()
        await _cancel_tasks(restore, writer)


async def test_recreation_drains_existing_and_queued_entry_writes(
    recovery, monkeypatch
):
    manager = recovery.manager
    first_entered = asyncio.Event()
    first_release = asyncio.Event()
    second_attempted = asyncio.Event()
    second_entered = asyncio.Event()
    second_release = asyncio.Event()
    restore_attempted = asyncio.Event()
    created = asyncio.Event()
    original_create = recovery.create.side_effect
    original_guard = manager.config_entry_write_guard

    @asynccontextmanager
    async def guard(entry_id, **kwargs):
        if entry_id == "old-entry":
            restore_attempted.set()
        async with original_guard(entry_id, **kwargs):
            yield

    async def write(entered, release, attempted=None):
        if attempted is not None:
            attempted.set()
        async with manager.config_entry_write_guard("new-entry"):
            entered.set()
            await release.wait()

    async def create(*args):
        created.set()
        return await original_create(*args)

    recovery.create.side_effect = create
    monkeypatch.setattr(manager, "config_entry_write_guard", guard)
    tasks = [asyncio.create_task(write(first_entered, first_release))]
    try:
        async with asyncio.timeout(2):
            await first_entered.wait()
            tasks.append(
                asyncio.create_task(
                    write(second_entered, second_release, second_attempted)
                )
            )
            await second_attempted.wait()
            tasks.append(asyncio.create_task(manager.restore_snapshot(recovery.name)))
            await restore_attempted.wait()
            assert not created.is_set(), "An existing guarded write must finish first"
            first_release.set()
            await second_entered.wait()
            assert not created.is_set(), "Already queued guards must retain ownership"
            second_release.set()
            await asyncio.gather(*tasks)
        assert created.is_set()
    finally:
        first_release.set()
        second_release.set()
        await _cancel_tasks(*tasks)


async def test_unrelated_ordinary_writes_remain_parallel(recovery):
    entered = asyncio.Event()

    async def other():
        async with recovery.manager.config_entry_write_guard("another-entry"):
            entered.set()

    async with asyncio.timeout(2):
        async with recovery.manager.config_entry_write_guard("old-entry"):
            task = asyncio.create_task(other())
            await entered.wait()
            await task


async def test_cancelled_recreation_releases_waiting_entry_write(recovery):
    reached = asyncio.Event()
    blocked = asyncio.Event()
    attempted = asyncio.Event()
    entered = asyncio.Event()
    original_create = recovery.create.side_effect

    async def create(*args):
        result = await original_create(*args)
        reached.set()
        await blocked.wait()
        return result

    recovery.create.side_effect = create
    task = asyncio.create_task(recovery.manager.restore_snapshot(recovery.name))

    async def write():
        attempted.set()
        async with recovery.manager.config_entry_write_guard("new-entry"):
            entered.set()

    async with asyncio.timeout(2):
        await reached.wait()
        writer = asyncio.create_task(write())
        await attempted.wait()
        assert not entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await writer
        assert entered.is_set()
        assert recovery.state.entries[0]["entry_id"] == "new-entry"
    assert recovery.name not in recovery.manager._protected_snapshot_names


@pytest.mark.parametrize("exclusive", [False, True])
async def test_entry_guard_is_reentrant_for_its_task(recovery, exclusive):
    guard = recovery.manager.config_entry_write_guard
    async with asyncio.timeout(2):
        async with guard("old-entry", exclusive=exclusive):
            async with guard("old-entry", exclusive=exclusive):
                async with guard("new-entry"):
                    pass


async def test_cancelled_waiting_restore_does_not_block_new_writes(recovery):
    guard = recovery.manager.config_entry_write_guard
    attempted = asyncio.Event()

    async def restore():
        attempted.set()
        async with guard("old-entry", exclusive=True):
            pytest.fail("The existing write has not finished")

    async def another_write():
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


async def test_nested_ordinary_write_can_finish_after_restore_queues(recovery):
    guard = recovery.manager.config_entry_write_guard
    attempted = asyncio.Event()
    entered = asyncio.Event()

    async def restore():
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


async def test_cancelled_queued_entry_guard_does_not_hold_up_restore(recovery):
    guard = recovery.manager.config_entry_write_guard
    attempted = asyncio.Event()

    async def queued_write():
        attempted.set()
        async with guard("old-entry"):
            pytest.fail("The original entry write has not finished")

    async def restore():
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


async def test_guard_upgrade_refuses_instead_of_deadlocking(recovery):
    guard = recovery.manager.config_entry_write_guard
    async with asyncio.timeout(2):
        async with guard("old-entry"):
            with pytest.raises(RuntimeError, match="inside an entry write"):
                async with guard("old-entry", exclusive=True):
                    pytest.fail("An ordinary write cannot upgrade its admission")


async def test_exclusive_restores_for_different_entries_serialize(recovery):
    guard = recovery.manager.config_entry_write_guard
    attempted = asyncio.Event()
    entered = asyncio.Event()

    async def restore():
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
