"""Unit tests for the usage logger with ring buffer."""

import asyncio
import errno
import logging
import os
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ha_mcp.utils.usage_logger import (
    _MAX_REDACT_DEPTH,
    _REDACTED_VALUE,
    AVG_LOG_ENTRIES_PER_TOOL,
    DEFAULT_RING_BUFFER_SIZE,
    ToolUsageLog,
    UsageLogger,
    _redact_parameters,
    get_recent_logs,
    log_tool_call,
    shutdown_usage_logger,
)


class TestUsageLoggerRingBuffer:
    """Test suite for the UsageLogger ring buffer functionality."""

    @pytest.fixture
    def temp_log_dir(self):
        """Create a temporary directory for log files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def logger(self, temp_log_dir):
        """Create a UsageLogger instance with small ring buffer for testing."""
        log_path = Path(temp_log_dir) / "test_usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=10)
        yield logger
        logger.shutdown()

    def test_ring_buffer_stores_entries(self, logger):
        """Test that entries are stored in the ring buffer."""
        logger.log_tool_usage(
            tool_name="ha_test_tool",
            parameters={"key": "value"},
            execution_time_ms=100.0,
            success=True,
        )

        entries = logger.get_recent_entries(10)
        assert len(entries) == 1
        assert entries[0]["tool_name"] == "ha_test_tool"
        assert entries[0]["success"] is True
        assert entries[0]["execution_time_ms"] == 100.0

    def test_ring_buffer_newest_first(self, logger):
        """Test that entries are returned newest-first."""
        for i in range(3):
            logger.log_tool_usage(
                tool_name=f"ha_tool_{i}",
                parameters={},
                execution_time_ms=float(i * 10),
                success=True,
            )

        entries = logger.get_recent_entries(10)
        assert len(entries) == 3
        # Newest first
        assert entries[0]["tool_name"] == "ha_tool_2"
        assert entries[1]["tool_name"] == "ha_tool_1"
        assert entries[2]["tool_name"] == "ha_tool_0"

    def test_ring_buffer_limit_works(self, logger):
        """Test that requesting fewer entries than available works."""
        for i in range(5):
            logger.log_tool_usage(
                tool_name=f"ha_tool_{i}",
                parameters={},
                execution_time_ms=0,
                success=True,
            )

        entries = logger.get_recent_entries(2)
        assert len(entries) == 2
        # Should get the 2 most recent
        assert entries[0]["tool_name"] == "ha_tool_4"
        assert entries[1]["tool_name"] == "ha_tool_3"

    def test_ring_buffer_overflow(self, logger):
        """Test that ring buffer correctly drops old entries when full."""
        # Logger has ring_buffer_size=10, so add 15 entries
        for i in range(15):
            logger.log_tool_usage(
                tool_name=f"ha_tool_{i}",
                parameters={},
                execution_time_ms=0,
                success=True,
            )

        entries = logger.get_recent_entries(20)
        # Should only have 10 entries (buffer size)
        assert len(entries) == 10
        # Should have entries 5-14 (oldest 0-4 should be dropped)
        assert entries[0]["tool_name"] == "ha_tool_14"  # newest
        assert entries[9]["tool_name"] == "ha_tool_5"  # oldest in buffer

    def test_ring_buffer_thread_safety(self, logger):
        """Test that ring buffer is thread-safe under concurrent access."""
        num_threads = 5
        entries_per_thread = 20
        errors = []

        def writer_thread(thread_id):
            try:
                for i in range(entries_per_thread):
                    logger.log_tool_usage(
                        tool_name=f"ha_thread_{thread_id}_entry_{i}",
                        parameters={},
                        execution_time_ms=0,
                        success=True,
                    )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def reader_thread():
            try:
                for _ in range(50):
                    _ = logger.get_recent_entries(5)
                    time.sleep(0.001)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = []
        # Start writer threads
        for i in range(num_threads):
            t = threading.Thread(target=writer_thread, args=(i,))
            threads.append(t)
            t.start()

        # Start reader threads
        for _ in range(3):
            t = threading.Thread(target=reader_thread)
            threads.append(t)
            t.start()

        # Wait for all threads
        for t in threads:
            t.join(timeout=5.0)

        # No errors should have occurred
        assert len(errors) == 0

    def test_ring_buffer_empty(self, logger):
        """Test getting entries from empty buffer."""
        entries = logger.get_recent_entries(10)
        assert entries == []

    def test_ring_buffer_with_error_entries(self, logger):
        """Test that error entries are properly stored."""
        logger.log_tool_usage(
            tool_name="ha_failing_tool",
            parameters={"entity_id": "light.test"},
            execution_time_ms=50.0,
            success=False,
            error_message="Entity not found",
        )

        entries = logger.get_recent_entries(1)
        assert len(entries) == 1
        assert entries[0]["success"] is False
        assert entries[0]["error_message"] == "Entity not found"


class TestUsageLoggerDefaults:
    """Test UsageLogger default behavior."""

    @pytest.fixture(autouse=True)
    def _reset_data_dir_cache(self):
        from ha_mcp.utils.data_paths import get_data_dir

        get_data_dir.cache_clear()
        yield
        get_data_dir.cache_clear()

    def test_default_log_path(self, monkeypatch, tmp_path):
        """Default log path lives under ``~/.ha-mcp/logs/`` when no
        overrides are in play."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        monkeypatch.delenv("HA_MCP_CONFIG_DIR", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        logger = UsageLogger()
        assert logger.log_file_path == tmp_path / ".ha-mcp" / "logs" / "mcp_usage.jsonl"
        logger.shutdown()

    def test_honors_ha_mcp_config_dir(self, monkeypatch, tmp_path):
        """``HA_MCP_CONFIG_DIR`` redirects logs the same way it redirects
        the settings-UI tool config — the two share ``get_data_dir()``."""
        monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
        custom = tmp_path / "custom"
        monkeypatch.setenv("HA_MCP_CONFIG_DIR", str(custom))
        logger = UsageLogger()
        assert logger.log_file_path == custom / "logs" / "mcp_usage.jsonl"
        logger.shutdown()


class TestUsageLoggerConstants:
    """Test constants are properly defined."""

    def test_avg_log_entries_per_tool(self):
        """Test that AVG_LOG_ENTRIES_PER_TOOL is a reasonable value."""
        assert AVG_LOG_ENTRIES_PER_TOOL >= 1
        assert AVG_LOG_ENTRIES_PER_TOOL <= 10

    def test_default_ring_buffer_size(self):
        """Test that DEFAULT_RING_BUFFER_SIZE is reasonable."""
        assert DEFAULT_RING_BUFFER_SIZE >= 50
        assert DEFAULT_RING_BUFFER_SIZE <= 1000


class TestUsageLoggerGlobalFunctions:
    """Test module-level convenience functions."""

    @pytest.fixture(autouse=True)
    def reset_global_logger(self):
        """Reset the global logger before and after each test."""
        shutdown_usage_logger()
        yield
        shutdown_usage_logger()

    def test_get_recent_logs_returns_list(self):
        """Test that get_recent_logs returns a list."""
        logs = get_recent_logs(10)
        assert isinstance(logs, list)

    def test_log_tool_call_and_retrieve(self):
        """Test logging via global function and retrieving."""
        log_tool_call(
            tool_name="ha_global_test",
            parameters={"test": True},
            execution_time_ms=25.0,
            success=True,
        )

        logs = get_recent_logs(5)
        assert len(logs) >= 1
        # Find our entry (there might be others from previous tests)
        our_entry = next(
            (entry for entry in logs if entry["tool_name"] == "ha_global_test"), None
        )
        assert our_entry is not None
        assert our_entry["success"] is True


class TestLogToolUsageDecorator:
    """Tests for the @log_tool_usage decorator in ha_mcp.tools.helpers."""

    @pytest.fixture(autouse=True)
    def reset_global_logger(self):
        shutdown_usage_logger()
        yield
        shutdown_usage_logger()

    async def test_cancelled_call_is_not_logged_as_success(self):
        """A cancelled tool call never completed, so recording it as a success
        would make the usage log claim a result that was never produced.

        ``CancelledError`` skips ``except Exception``, so the decorator's
        handler must catch ``BaseException`` for the ``finally`` log to carry
        ``success: False``; ``str()`` is empty on a bare ``CancelledError``, so
        the class name stands in as the error message.
        """
        from ha_mcp.tools.helpers import log_tool_usage

        @log_tool_usage
        async def ha_cancelled_tool():
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await ha_cancelled_tool()

        entry = next(
            e for e in get_recent_logs(5) if e["tool_name"] == "ha_cancelled_tool"
        )
        assert entry["success"] is False
        assert entry["error_message"] == "CancelledError"


class TestParameterRedaction:
    """Tests for secret masking at the logging chokepoint (GHSA-mc92-ww4q-6fg4)."""

    @pytest.fixture(autouse=True)
    def reset_global_logger(self):
        shutdown_usage_logger()
        yield
        shutdown_usage_logger()

    def test_nested_code_redacted_sibling_preserved(self):
        """ha_call_service(data={'code': ...}) leaks the code via the log; the
        recursive key match must mask it while keeping debug-useful siblings."""
        result = _redact_parameters(
            {"data": {"code": "4321", "entity_id": "lock.front", "brightness": 255}}
        )
        assert result["data"]["code"] == _REDACTED_VALUE
        assert result["data"]["entity_id"] == "lock.front"
        assert result["data"]["brightness"] == 255

    def test_top_level_sensitive_keys_redacted(self):
        result = _redact_parameters(
            {"token": "abc", "password": "hunter2", "secret": "s", "api_key": "k"}
        )
        assert all(result[k] == _REDACTED_VALUE for k in result)

    def test_suffix_match_redacts_token_family(self):
        result = _redact_parameters({"auth_token": "x", "device_secret": "y"})
        assert result["auth_token"] == _REDACTED_VALUE
        assert result["device_secret"] == _REDACTED_VALUE

    def test_non_secret_code_key_suffixes_preserved(self):
        """status_code / cache_key are NOT secrets — _code/_key are deliberately
        excluded from suffix matching so debug value survives."""
        result = _redact_parameters({"status_code": 500, "cache_key": "abc"})
        assert result["status_code"] == 500
        assert result["cache_key"] == "abc"

    def test_assist_sentence_redacted(self):
        """ha_manage_pipeline(action='process') carries user speech, which
        ha_report_issue would otherwise publish verbatim."""
        result = _redact_parameters(
            {"action": "process", "sentence": "unlock the front door for Alex"}
        )
        assert result["sentence"] == _REDACTED_VALUE
        assert result["action"] == "process"

    def test_case_insensitive(self):
        result = _redact_parameters({"CODE": "1234", "Token": "t"})
        assert result["CODE"] == _REDACTED_VALUE
        assert result["Token"] == _REDACTED_VALUE

    def test_benign_params_unchanged(self):
        params = {"entity_id": "light.test", "key": "value", "nested": {"a": 1}}
        assert _redact_parameters(params) == params

    def test_list_of_dicts_recursed(self):
        result = _redact_parameters({"items": [{"code": "1"}, {"entity_id": "x"}]})
        assert result["items"][0]["code"] == _REDACTED_VALUE
        assert result["items"][1]["entity_id"] == "x"

    def test_fail_closed_past_max_depth(self):
        """A subtree nested past the recursion guard is masked wholesale, never
        returned verbatim."""
        deep = current = {}
        for _ in range(_MAX_REDACT_DEPTH + 3):
            current["next"] = {}
            current = current["next"]
        current["code"] = "leak"
        result = _redact_parameters(deep)
        # Walk down until we hit the masked cutoff; the deep 'code' must not survive.
        flat = repr(result)
        assert "leak" not in flat
        assert _REDACTED_VALUE in flat

    def test_does_not_mutate_input(self):
        """log_tool_usage receives the live kwargs dict — redaction must copy."""
        original = {"data": {"code": "4321"}}
        _redact_parameters(original)
        assert original["data"]["code"] == "4321"

    def test_end_to_end_buffer_redacted(self):
        """The ring buffer (read by ha_report_issue) must hold redacted params."""
        log_tool_call(
            tool_name="ha_call_service",
            parameters={"data": {"code": "4321", "entity_id": "lock.front"}},
            execution_time_ms=10.0,
            success=False,
        )
        entry = next(
            e for e in get_recent_logs(5) if e["tool_name"] == "ha_call_service"
        )
        assert entry["parameters"]["data"]["code"] == _REDACTED_VALUE
        assert entry["parameters"]["data"]["entity_id"] == "lock.front"

    def test_secrets_not_persisted_to_jsonl(self, tmp_path):
        """The on-disk mcp_usage.jsonl must not contain raw secrets either — the
        redacted entry is what gets queued for the background disk write."""
        import json

        log_path = tmp_path / "usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            logger.log_tool_usage(
                tool_name="ha_call_service",
                # Digits alone can turn up in the entry's timestamp.
                parameters={"data": {"code": "code-4321", "entity_id": "lock.front"}},
                execution_time_ms=10.0,
                success=True,
            )
            # The disk write happens on a background thread; wait for it to flush.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if log_path.exists() and log_path.read_text().strip():
                    break
                time.sleep(0.05)
        finally:
            logger.shutdown()

        content = log_path.read_text()
        assert "code-4321" not in content
        assert "lock.front" in content
        entry = json.loads(content.splitlines()[0])
        assert entry["parameters"]["data"]["code"] == _REDACTED_VALUE


def _log_one(logger: UsageLogger, tool_name: str = "ha_get_state") -> None:
    logger.log_tool_usage(
        tool_name=tool_name,
        parameters={"entity_id": "sun.sun"},
        execution_time_ms=1.0,
        success=True,
    )


def _wait_for(condition: Callable[[], bool], timeout: float = 3.0) -> None:
    """Wait for the background disk writer to reach ``condition``."""
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.05)


def _disk_disabled_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "ha_mcp.utils.usage_logger" and r.levelno == logging.WARNING
    ]


class TestLogFileMode:
    """mcp_usage.jsonl records tool arguments and results, so only the owner reads it."""

    @staticmethod
    def _write_one(log_path: Path) -> None:
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            _log_one(logger)
            _wait_for(lambda: log_path.exists() and bool(log_path.read_text().strip()))
        finally:
            logger.shutdown()

    @pytest.fixture
    def permissive_umask(self) -> Iterator[None]:
        """Run under the common ``022`` umask, which leaves new files ``0644``."""
        previous = os.umask(0o022)
        try:
            yield
        finally:
            os.umask(previous)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    @pytest.mark.usefixtures("permissive_umask")
    def test_new_log_file_is_owner_only(self, tmp_path: Path) -> None:
        log_path = tmp_path / "logs" / "usage.jsonl"
        self._write_one(log_path)
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(log_path.parent.stat().st_mode) == 0o700

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_existing_world_readable_log_is_tightened(self, tmp_path: Path) -> None:
        log_path = tmp_path / "usage.jsonl"
        log_path.write_text("")
        log_path.chmod(0o644)
        self._write_one(log_path)
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
    def test_log_that_cannot_be_tightened_stops_file_writes_and_names_the_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        log_path = tmp_path / "usage.jsonl"
        log_path.write_text("")
        log_path.chmod(0o644)

        def refuse(self: Path, mode: int) -> None:
            raise PermissionError("not the owner")

        monkeypatch.setattr(Path, "chmod", refuse)
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            assert logger._disk_enabled is False
        finally:
            logger.shutdown()
        [warning] = _disk_disabled_warnings(caplog)
        # The directory exists and is writable; only the file's mode is at fault,
        # so the operator must be pointed at the file, not told to fix a mkdir.
        assert str(log_path) in warning
        assert "could not create" not in warning


class TestLogPathControlledByOthers:
    """Another local user must not be able to redirect or read the usage log."""

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    def test_log_directory_owned_by_another_user_stops_file_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        real_euid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
        logger = UsageLogger(str(log_dir / "usage.jsonl"), ring_buffer_size=5)
        try:
            assert logger._disk_enabled is False
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    def test_refused_log_file_keeps_recent_calls_for_bug_reports(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        real_euid = os.geteuid()
        monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
        logger = UsageLogger(str(log_dir / "usage.jsonl"), ring_buffer_size=5)
        monkeypatch.setattr("ha_mcp.utils.usage_logger._usage_logger", logger)
        try:
            assert logger._disk_enabled is False
            _log_one(logger, tool_name="ha_kept_in_memory")
            assert [e["tool_name"] for e in get_recent_logs(5)] == ["ha_kept_in_memory"]
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    def test_world_writable_log_directory_is_closed_to_other_writers(
        self, tmp_path: Path
    ) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_dir.chmod(0o777)
        logger = UsageLogger(str(log_dir / "usage.jsonl"), ring_buffer_size=5)
        try:
            assert logger._disk_enabled is True
            assert log_dir.stat().st_mode & (stat.S_IWGRP | stat.S_IWOTH) == 0
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_symlinked_log_directory_is_refused_and_left_unchanged(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "shared"
        target.mkdir()
        target.chmod(0o777)
        log_dir = tmp_path / "logs"
        log_dir.symlink_to(target, target_is_directory=True)
        logger = UsageLogger(str(log_dir / "usage.jsonl"), ring_buffer_size=5)
        try:
            assert logger._disk_enabled is False
            assert stat.S_IMODE(target.stat().st_mode) == 0o777
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
    def test_symlink_planted_before_startup_is_refused(self, tmp_path: Path) -> None:
        target = tmp_path / "victim.txt"
        target.write_text("")
        target.chmod(0o644)
        log_path = tmp_path / "usage.jsonl"
        log_path.symlink_to(target)
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            assert logger._disk_enabled is False
            assert stat.S_IMODE(target.stat().st_mode) == 0o644
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
    @pytest.mark.parametrize("link", ["symlink", "hardlink"])
    def test_log_swapped_after_startup_receives_no_tool_data(
        self, tmp_path: Path, link: str
    ) -> None:
        log_path = tmp_path / "usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            target = tmp_path / "planted.jsonl"
            fd = os.open(target, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            if link == "symlink":
                log_path.symlink_to(target)
            else:
                os.link(target, log_path)
            _log_one(logger)
            _wait_for(lambda: not logger._disk_enabled)
            assert logger._disk_enabled is False
            assert target.read_text() == ""
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX owner and modes")
    @pytest.mark.parametrize("change", ["group_readable", "other_owner"])
    def test_log_no_longer_private_after_startup_receives_no_tool_data(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
    ) -> None:
        log_path = tmp_path / "usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            fd = os.open(log_path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
            if change == "group_readable":
                log_path.chmod(0o644)
            else:
                real_euid = os.geteuid()
                monkeypatch.setattr(os, "geteuid", lambda: real_euid + 1)
            _log_one(logger)
            _wait_for(lambda: not logger._disk_enabled)
            assert logger._disk_enabled is False
            assert log_path.read_text() == ""
        finally:
            logger.shutdown()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFOs and modes")
    @pytest.mark.parametrize("swap", ["fifo_without_reader", "unwritable_file"])
    def test_log_that_cannot_be_opened_after_startup_warns_once(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, swap: str
    ) -> None:
        if swap == "unwritable_file" and os.geteuid() == 0:
            pytest.skip("root bypasses file modes")
        log_path = tmp_path / "usage.jsonl"
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            if swap == "fifo_without_reader":
                os.mkfifo(log_path, 0o600)
            else:
                fd = os.open(log_path, os.O_CREAT | os.O_WRONLY, 0o600)
                os.close(fd)
                log_path.chmod(0o000)
            _log_one(logger)
            _log_one(logger)
            _wait_for(lambda: logger._log_queue.empty() and not logger._disk_enabled)
            assert logger._disk_enabled is False
        finally:
            logger.shutdown()
        [warning] = _disk_disabled_warnings(caplog)
        assert str(log_path) in warning

    def test_log_directory_removed_and_recreated_resumes_writing(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        log_dir = tmp_path / "logs"
        log_dir.mkdir(mode=0o700)
        log_path = log_dir / "usage.jsonl"
        caplog.set_level(logging.WARNING, logger="ha_mcp.utils.usage_logger")
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:
            log_dir.rmdir()
            _log_one(logger, tool_name="ha_while_gone")
            _wait_for(lambda: logger._log_queue.unfinished_tasks == 0)
            assert logger._disk_enabled is True
            log_dir.mkdir(mode=0o700)
            _log_one(logger, tool_name="ha_after_recreate")
            _wait_for(lambda: log_path.exists() and log_path.read_text() != "")
            assert "ha_after_recreate" in log_path.read_text()
            assert "ha_while_gone" not in log_path.read_text()
        finally:
            logger.shutdown()
        assert _disk_disabled_warnings(caplog) == []

    @pytest.mark.parametrize("err", [errno.EMFILE, errno.ENFILE])
    def test_out_of_descriptors_keeps_the_log_file_on(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: int
    ) -> None:
        log_path = tmp_path / "usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        try:

            def exhausted(path: str, flags: int) -> int:
                raise OSError(err, os.strerror(err))

            monkeypatch.setattr("ha_mcp.utils.usage_logger._private_opener", exhausted)
            _log_one(logger)
            _wait_for(lambda: logger._log_queue.unfinished_tasks == 0)
            assert logger._disk_enabled is True
        finally:
            logger.shutdown()

    def test_descriptor_is_closed_when_the_file_object_cannot_be_made(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log_path = tmp_path / "usage.jsonl"
        logger = UsageLogger(str(log_path), ring_buffer_size=5)
        closed: list[int] = []
        real_close = os.close

        def failing_open(*args: object, **kwargs: object) -> None:
            raise ValueError("cannot wrap the descriptor")

        def tracking_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        try:
            monkeypatch.setattr("builtins.open", failing_open)
            monkeypatch.setattr(os, "close", tracking_close)
            logger._write_log_entry(
                ToolUsageLog(
                    timestamp="2026-10-29T00:00:00+00:00",
                    tool_name="ha_get_state",
                    parameters={},
                    execution_time_ms=1.0,
                    success=True,
                )
            )
            assert len(closed) == 1
        finally:
            monkeypatch.undo()
            logger.shutdown()
