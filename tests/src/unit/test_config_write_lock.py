"""Tests for the cross-process config write lock (#1993 round 3)."""

import threading

from ha_mcp.utils.config_write_lock import config_file_lock
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
