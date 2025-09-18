"""
Usage logging for MCP tool calls to track usage patterns and performance metrics.
"""

import json
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any


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
    """Async disk logger for MCP tool usage tracking."""

    def __init__(self, log_file_path: str = "logs/mcp_usage.jsonl"):
        self.log_file_path = Path(log_file_path)
        self.log_file_path.parent.mkdir(parents=True, exist_ok=True)

        # Thread-safe queue for log entries
        self._log_queue: Queue = Queue()
        self._logger_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
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
            self._log_queue.put(log_entry)
        except Exception:
            # Silent error handling to never break MCP server
            pass

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
