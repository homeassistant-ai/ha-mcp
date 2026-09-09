"""Cancelled backup I/O retains serialization until its executor worker stops."""

import asyncio
import threading
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_mcp import backup_manager as bm


@pytest.mark.parametrize("worker_fails", [False, True])
async def test_cancelled_capture_waits_for_writer_before_next_safety_capture(
    tmp_path, monkeypatch, worker_fails
):
    manager = bm.get_backup_manager(
        SimpleNamespace(),
        SimpleNamespace(
            enable_auto_backup=True,
            auto_backup_throttle_minutes=0,
            auto_backup_retain_per_entity=5,
            auto_backup_dir=str(tmp_path),
        ),
    )
    current = {
        "entry_id": "entry",
        "options": {"name": "Example", "template_type": "sensor", "state": "first"},
    }
    restore = AsyncMock(return_value={})
    manager.register(
        bm.DomainHandler(
            "helper_template",
            AsyncMock(side_effect=lambda *_: deepcopy(current)),
            restore,
        )
    )
    source = manager._write_snapshot("helper_template", "entry", current, "source")
    monkeypatch.setattr(bm, "_now_ts", lambda: "20260909_120000")
    first_reserved = asyncio.Event()
    release_writer = threading.Event()
    loop = asyncio.get_running_loop()
    original = manager._unclaimed_target
    calls = 0

    def reserve(*args):
        nonlocal calls
        calls += 1
        target = original(*args)
        if calls == 1:
            loop.call_soon_threadsafe(first_reserved.set)
            if not release_writer.wait(5):
                raise TimeoutError("test did not release the writer")
            if worker_fails:
                raise OSError("controlled writer failure after cancellation")
        return target

    monkeypatch.setattr(manager, "_unclaimed_target", reserve)
    first = asyncio.create_task(manager.restore_snapshot(source.name))
    second = None
    try:
        await asyncio.wait_for(first_reserved.wait(), 2)
        entry_lock = manager._entry_write_locks["entry"]
        first.cancel("first cancellation")
        await asyncio.sleep(0)
        first.cancel("second cancellation")
        await asyncio.sleep(0)
        held_capture_lock = manager._locks["helper_template:entry"].locked()
        held_entry_lock = entry_lock.locked()
        current["options"]["state"] = "second"
        second = asyncio.create_task(manager.restore_snapshot(source.name))
        await asyncio.sleep(0)
    finally:
        release_writer.set()
        results = await asyncio.gather(
            first, *([second] if second else []), return_exceptions=True
        )
    assert held_capture_lock and held_entry_lock
    assert isinstance(results[0], asyncio.CancelledError)
    assert len(results) == 2
    assert not isinstance(results[1], BaseException)
    safety = manager.read_snapshot(results[1]["safety_backup"])
    assert safety["config"]["options"]["state"] == "second"
    assert source.exists()
    restore.assert_awaited_once()
    assert not manager._locks["helper_template:entry"].locked()
