"""
Usage logging for MCP tool calls to track usage patterns and performance metrics.
"""

import json
import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any

# Default ring buffer size - keeps last N entries in memory
DEFAULT_RING_BUFFER_SIZE = 200

# Average log entries per tool call (empirically observed)
# This includes the tool itself plus any associated operations
AVG_LOG_ENTRIES_PER_TOOL = 3

# Startup log collection duration in seconds
STARTUP_LOG_DURATION_SECONDS = 60


class StartupLogCollector(logging.Handler):
    """Collects log messages during the first minute of server startup."""

    def __init__(self, duration_seconds: int = STARTUP_LOG_DURATION_SECONDS):
        super().__init__()
        self._start_time = time.time()
        self._duration = duration_seconds
        self._logs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._active = True

    def emit(self, record: logging.LogRecord) -> None:
        """Capture log record if within startup window."""
        if not self._active:
            return

        elapsed = time.time() - self._start_time
        if elapsed > self._duration:
            self._active = False
            return

        with self._lock:
            self._logs.append({
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "elapsed_seconds": round(elapsed, 2),
            })

    def get_logs(self) -> list[dict[str, Any]]:
        """Get collected startup logs."""
        with self._lock:
            return list(self._logs)

    def is_active(self) -> bool:
        """Check if still collecting startup logs."""
        return self._active and (time.time() - self._start_time) <= self._duration


# Global startup log collector - initialized at module import
_startup_collector: StartupLogCollector | None = None


def _init_startup_collector() -> None:
    """Initialize startup log collector and attach to root logger."""
    global _startup_collector
    if _startup_collector is None:
        _startup_collector = StartupLogCollector()
        _startup_collector.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(_startup_collector)


# Initialize on module load
_init_startup_collector()


def get_startup_logs() -> list[dict[str, Any]]:
    """Get startup logs collected during the first minute."""
    if _startup_collector is None:
        return []
    return _startup_collector.get_logs()


@dataclass
class ToolUsageLog:
    """Single tool usage log entry."""

    timestamp: str
    tool_name: str
    parameters: dict[str, Any]
    execution_time_ms: float
    success: bool
    error_message: str | None = None
    response_size_bytes: int | None = None
    user_context: str | None = None


class UsageLogger:
    """Async disk logger for MCP tool usage tracking with in-memory ring buffer."""

    def __init__(
        self,
        log_file_path: str | None = None,
        ring_buffer_size: int = DEFAULT_RING_BUFFER_SIZE,
    ):
        self._enabled = True

        if log_file_path:
            self.log_file_path = Path(log_file_path)
        else:
            # Use user's home directory by default to avoid read-only filesystem errors
            # when running via uvx/npx which might have read-only CWD
            self.log_file_path = Path.home() / ".ha-mcp" / "logs" / "mcp_usage.jsonl"

        try:
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            # Directory creation failed (e.g., read-only filesystem)
            # Disable logging silently to avoid disrupting the MCP server
            self._enabled = False

        # In-memory ring buffer for fast access to recent logs
        # Thread-safe: deque is thread-safe for append/pop operations
        self._ring_buffer: deque[dict[str, Any]] = deque(maxlen=ring_buffer_size)
        self._buffer_lock = threading.Lock()

        # Thread-safe queue for disk writes
        self._log_queue: Queue[ToolUsageLog] = Queue()
        self._logger_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        if self._enabled:
            self._start_logger_thread()

    def _start_logger_thread(self) -> None:
        """Start background thread for disk writes."""
        self._logger_thread = threading.Thread(
            target=self._log_writer_worker, daemon=True
        )
        self._logger_thread.start()

    def _log_writer_worker(self) -> None:
        """Background thread worker for writing logs to disk."""
        while not self._stop_event.is_set():
            try:
                # Wait for log entry with timeout to check stop event
                if not self._log_queue.empty():
                    log_entry = self._log_queue.get(timeout=1.0)
                    self._write_log_entry(log_entry)
                else:
                    # Sleep briefly to avoid busy waiting
                    threading.Event().wait(0.1)
            except Exception as e:
                # Silent error handling to avoid disrupting MCP server
                print(f"Usage logger error: {e}")

    def _write_log_entry(self, log_entry: ToolUsageLog) -> None:
        """Write single log entry to disk."""
        try:
            with open(self.log_file_path, "a", encoding="utf-8") as f:
                json.dump(asdict(log_entry), f, ensure_ascii=False)
                f.write("\n")
        except Exception:
            # Silent error handling
            pass

    def log_tool_usage(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        execution_time_ms: float,
        success: bool,
        error_message: str | None = None,
        response_size_bytes: int | None = None,
        user_context: str | None = None,
    ) -> None:
        """Log tool usage (non-blocking)."""
        if not self._enabled:
            return
        try:
            log_entry = ToolUsageLog(
                timestamp=datetime.now(UTC).isoformat(),
                tool_name=tool_name,
                parameters=parameters,
                execution_time_ms=execution_time_ms,
                success=success,
                error_message=error_message,
                response_size_bytes=response_size_bytes,
                user_context=user_context,
            )

            # Add to ring buffer (thread-safe)
            entry_dict = asdict(log_entry)
            with self._buffer_lock:
                self._ring_buffer.append(entry_dict)

            # Queue for disk write
            self._log_queue.put(log_entry)
        except Exception:
            # Silent error handling to never break MCP server
            pass

    def get_recent_entries(self, count: int) -> list[dict[str, Any]]:
        """
        Get recent log entries from the in-memory ring buffer.

        Args:
            count: Maximum number of entries to return

        Returns:
            List of log entries as dictionaries, ordered newest to oldest
        """
        with self._buffer_lock:
            # Get the last N entries, reversed to newest-first order
            buffer_list = list(self._ring_buffer)
            return list(reversed(buffer_list[-count:]))

    def shutdown(self) -> None:
        """Gracefully shutdown logger."""
        self._stop_event.set()
        if self._logger_thread and self._logger_thread.is_alive():
            self._logger_thread.join(timeout=2.0)


# Global logger instance
_usage_logger: UsageLogger | None = None


def get_usage_logger() -> UsageLogger:
    """Get global usage logger instance."""
    global _usage_logger
    if _usage_logger is None:
        _usage_logger = UsageLogger()
    return _usage_logger


def log_tool_call(
    tool_name: str,
    parameters: dict[str, Any],
    execution_time_ms: float,
    success: bool,
    error_message: str | None = None,
    response_size_bytes: int | None = None,
    user_context: str | None = None,
) -> None:
    """Convenience function to log tool usage."""
    logger = get_usage_logger()
    logger.log_tool_usage(
        tool_name=tool_name,
        parameters=parameters,
        execution_time_ms=execution_time_ms,
        success=success,
        error_message=error_message,
        response_size_bytes=response_size_bytes,
        user_context=user_context,
    )


def shutdown_usage_logger() -> None:
    """Shutdown global usage logger."""
    global _usage_logger
    if _usage_logger:
        _usage_logger.shutdown()
        _usage_logger = None


def get_recent_logs(max_entries: int = 20) -> list[dict[str, Any]]:
    """
    Get recent log entries from the in-memory ring buffer.

    This is O(1) access - no file I/O required.

    Args:
        max_entries: Maximum number of log entries to return (most recent first)

    Returns:
        List of log entries as dictionaries, ordered newest to oldest
    """
    logger = get_usage_logger()
    if not logger._enabled:
        return []

    return logger.get_recent_entries(max_entries)
