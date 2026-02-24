"""Unit tests for the usage logger with ring buffer."""

import tempfile
import threading
import time
from pathlib import Path

import pytest

from ha_mcp.utils.usage_logger import (
    AVG_LOG_ENTRIES_PER_TOOL,
    DEFAULT_RING_BUFFER_SIZE,
    UsageLogger,
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
            except Exception as e:
                errors.append(e)

        def reader_thread():
            try:
                for _ in range(50):
                    _ = logger.get_recent_entries(5)
                    time.sleep(0.001)
            except Exception as e:
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

    def test_default_log_path(self):
        """Test that default log path is in user home directory."""
        logger = UsageLogger()
        assert str(logger.log_file_path).startswith(str(Path.home()))
        assert ".ha-mcp" in str(logger.log_file_path)
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
