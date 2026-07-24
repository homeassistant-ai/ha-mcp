"""Tests for the cross-process config write lock (#1993 round 3)."""

import asyncio
import contextlib
import threading
import time

from ha_mcp.utils import config_write_lock as cwl
from ha_mcp.utils.config_write_lock import config_file_lock, config_write_guard
from ha_mcp.utils.data_paths import get_data_dir


class TestConfigFileLock:
    def test_acquire_release_reacquire(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        get_data_dir.cache_clear()
        try:
            with config_file_lock():
                pass
            with config_file_lock():  # released cleanly -> reacquirable
                pass
            assert (tmp_path / ".config_write.lock").exists()
        finally:
            get_data_dir.cache_clear()

    def test_excludes_other_holders(self, tmp_path, monkeypatch):
        # flock is per open-file-description, so a second holder (thread here,
        # another PROCESS in production) blocks until release.
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(tmp_path))
        get_data_dir.cache_clear()
        order: list[str] = []
        held = threading.Event()
        release = threading.Event()

        def holder():
            with config_file_lock():
                order.append("A-acquired")
                held.set()
                release.wait(timeout=5)
                order.append("A-releasing")

        def contender():
            held.wait(timeout=5)
            with config_file_lock():
                order.append("B-acquired")

        try:
            a = threading.Thread(target=holder)
            b = threading.Thread(target=contender)
            a.start()
            b.start()
            held.wait(timeout=5)
            release.set()
            a.join(timeout=5)
            b.join(timeout=5)
            assert order == ["A-acquired", "A-releasing", "B-acquired"]
        finally:
            release.set()
            get_data_dir.cache_clear()


class TestConfigWriteGuard:
    async def test_event_loop_stays_live_while_file_lock_blocks(self, monkeypatch):
        # config_write_guard must take the (blocking) file lock in a worker
        # thread: a slow flock on the event loop would stall every connected
        # client. Simulate a slow lock and assert other coroutines keep
        # running during acquisition.
        @contextlib.contextmanager
        def slow_lock():
            time.sleep(0.3)
            yield

        monkeypatch.setattr(cwl, "config_file_lock", slow_lock)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(ticker())
        try:
            async with config_write_guard():
                pass
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        # A blocked loop would leave the ticker at ~0; the thread hop keeps
        # it running throughout the 0.3s acquisition (margin for slow CI).
        assert ticks >= 5
