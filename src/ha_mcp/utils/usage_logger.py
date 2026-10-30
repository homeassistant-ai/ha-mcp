"""
Usage logging for MCP tool calls to track usage patterns and performance metrics.
"""

import copy
import errno
import json
import logging
import os
import stat
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any

from .data_paths import get_data_dir

# The usage log records tool arguments and results, so it is created owner-only.
_PRIVATE_FILE_MODE = 0o600

# O_NOFOLLOW refuses a symlink planted at the log path. O_NONBLOCK keeps a
# planted FIFO from hanging the writer thread on open and has no effect on a
# regular file. A FIFO with no reader then fails the open with ENXIO; one that
# has a reader opens, and the fstat check below refuses it.
_PRIVATE_OPEN_FLAGS = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)

# What open(path, "a") passes to its opener. The file is opened here, not by
# open(), so an error from the open can be told apart from one from the write.
_APPEND_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)

# Open errors that say nothing about who controls the path: the logs directory
# is gone for now, or the process or system is out of descriptors. The entry is
# dropped and the next one tries again; every retry still goes through
# O_NOFOLLOW and the fstat checks.
_TRANSIENT_OPEN_ERRNOS = frozenset({errno.ENOENT, errno.EMFILE, errno.ENFILE})


class UnsafeLogPathError(PermissionError):
    """The log path is not a private file and directory owned by this process."""


def _check_private_fd(fd: int) -> None:
    """Raise ``UnsafeLogPathError`` unless ``fd`` is our own owner-only regular file.

    Checked on the opened descriptor, not the path, so a file swapped in
    between the directory check and the open is still caught.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafeLogPathError("the usage log is not a regular file")
    if not hasattr(os, "geteuid"):
        return  # POSIX owner and mode bits do not apply on Windows.
    if st.st_uid != os.geteuid():
        raise UnsafeLogPathError("the usage log is owned by another user")
    if st.st_nlink != 1:
        raise UnsafeLogPathError("the usage log has another hard link")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise UnsafeLogPathError("the usage log is accessible to other users")


def _private_opener(path: str, flags: int) -> int:
    """``open()`` opener: creates the file owner-only and opens only our own private file."""
    try:
        fd = os.open(path, flags | _PRIVATE_OPEN_FLAGS, _PRIVATE_FILE_MODE)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise UnsafeLogPathError("the usage log path is a symlink") from e
        raise
    try:
        _check_private_fd(fd)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _chmod_or_refuse(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError as e:
        raise UnsafeLogPathError(
            f"could not restrict {path} to its owner ({type(e).__name__}: {e})"
        ) from e


def _make_private_dir(path: Path) -> None:
    """Refuse a log directory another user controls; drop group/other write from our own.

    ``mkdir(mode=0o700)`` does not touch a directory that already exists, and
    whoever can write to it can replace the log file.
    """
    if not hasattr(os, "geteuid"):
        return  # POSIX owner and mode bits do not apply on Windows.
    st = path.lstat()
    if not stat.S_ISDIR(st.st_mode):
        raise UnsafeLogPathError(f"{path} is not a directory")
    if st.st_uid != os.geteuid():
        raise UnsafeLogPathError(f"{path} is owned by another user")
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        _chmod_or_refuse(
            path, stat.S_IMODE(st.st_mode) & ~(stat.S_IWGRP | stat.S_IWOTH)
        )


def _make_private(path: Path) -> None:
    """Drop group/other access from a log file an older version created 0644.

    A missing file is fine: the opener creates it owner-only. Any other
    ``OSError`` propagates, so the caller stops writing to disk rather than
    keep appending to a file that stays readable by others.
    """
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise UnsafeLogPathError(f"{path} is not a regular file")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        _chmod_or_refuse(path, _PRIVATE_FILE_MODE)


logger = logging.getLogger(__name__)

# Default ring buffer size - keeps last N entries in memory
DEFAULT_RING_BUFFER_SIZE = 200

# Average log entries per tool call (empirically observed)
# This includes the tool itself plus any associated operations
AVG_LOG_ENTRIES_PER_TOOL = 3

# Startup log collection duration in seconds
STARTUP_LOG_DURATION_SECONDS = 60

# Hard cap on collected startup entries. The collector sits on the root logger
# at DEBUG with no filter, so an instance with `logger:` debug overrides on a
# chatty integration can emit tens of thousands of records inside the window.
# Keep the earliest ones — ha_report_issue's startup diagnostics care about the
# boot sequence, and the tail of a debug flood is noise.
MAX_STARTUP_LOG_ENTRIES = 5000


class StartupLogCollector(logging.Handler):
    """Collects log messages during the first minute of server startup."""

    def __init__(self, duration_seconds: int = STARTUP_LOG_DURATION_SECONDS):
        super().__init__()
        self._start_time = time.time()
        self._duration = duration_seconds
        self._logs: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._active = True
        # Per-thread, not an instance flag: emit() runs on every thread that
        # logs, and a shared flag would drop unrelated records from other
        # threads while one thread is mid-format.
        self._formatting = threading.local()
        # Nested records the guard dropped; surfaced by get_logs() so the
        # startup diagnostics show the hole instead of hiding it.
        self._dropped_nested = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Capture log record if within startup window.

        ``record.getMessage()`` is deliberately called OUTSIDE ``self._lock``
        and under a per-thread reentrancy guard. Formatting a record runs
        arbitrary third-party code: ``%s`` of an entity invokes
        ``Entity.__repr__`` -> ``_stringify_state`` -> the entity's ``state``
        property, and an integration whose property logs at debug re-enters
        this handler on the same thread. This handler runs in-process with
        Home Assistant in embedded mode, so that thread is the event loop.

        Formatting inside the lock made that re-entry a hard self-deadlock on a
        non-reentrant ``threading.Lock``, freezing the whole HA process — no
        port 8123, no recorder writes, and no log line explaining it, because
        the freeze happens inside logging itself (#2357).
        """
        if not self._active:
            return

        # Nested emit from within our own formatting: drop the record rather
        # than recurse. The outer record still gets collected, and the chain
        # (repr -> log -> repr -> log) cannot run away. The lock is free on
        # this path (formatting happens outside it), so counting is safe.
        if getattr(self._formatting, "active", False):
            with self._lock:
                self._dropped_nested += 1
            return

        elapsed = time.time() - self._start_time
        if elapsed > self._duration:
            self._active = False
            return

        self._formatting.active = True
        try:
            message = record.getMessage()
        except Exception as exc:  # noqa: BLE001
            # Mirrors logging's own contract: a handler never propagates. Only
            # attributes the logging machinery itself set are interpolated —
            # ``exc``'s repr or ``record.msg`` could run the code that just
            # failed. pathname:lineno names the broken call site, which the
            # logger name alone does not.
            message = (
                f"<unformattable record from {record.pathname}:{record.lineno}: "
                f"{type(exc).__name__}>"
            )
            # Honour the stdlib path too (logging.raiseExceptions / stderr);
            # at DEBUG on root this may be the only handler that saw the record.
            # Hand it a copy carrying the synthetic message and no args:
            # handleError() formats ``record.args`` again and re-raises a
            # RecursionError from that, which would escape into the caller.
            safe = copy.copy(record)
            safe.msg = message
            safe.args = ()
            self.handleError(safe)
        finally:
            self._formatting.active = False

        with self._lock:
            if len(self._logs) >= MAX_STARTUP_LOG_ENTRIES:
                self._active = False
                return
            self._logs.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": record.levelname,
                    "logger": record.name,
                    "message": message,
                    "elapsed_seconds": round(elapsed, 2),
                }
            )

    def get_logs(self) -> list[dict[str, Any]]:
        """Get collected startup logs.

        If the reentrancy guard dropped any nested records, one synthetic
        trailing entry says how many, so a reader of the diagnostics can tell
        "the integration logged nothing" from "records were swallowed".
        """
        with self._lock:
            logs = list(self._logs)
            dropped = self._dropped_nested
        if dropped:
            logs.append(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "level": "WARNING",
                    "logger": __name__,
                    "message": (
                        f"<{dropped} nested log record(s) emitted during message "
                        "formatting were dropped by the reentrancy guard>"
                    ),
                    "elapsed_seconds": round(time.time() - self._start_time, 2),
                }
            )
        return logs

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


@contextmanager
def preserve_startup_collector() -> Iterator[None]:
    """Keep the startup collector attached across a logging reconfiguration.

    ``logging.basicConfig(force=True)`` removes *and closes* every handler on
    the root logger. The collector is attached there at import time and is the
    source of ``ha_report_issue``'s startup diagnostics, so it is detached for
    the duration of the block — leaving nothing for the sweep to close — and
    re-attached afterwards.
    """
    collector = _startup_collector
    root = logging.getLogger()
    if collector is None or collector not in root.handlers:
        yield
        return

    root.removeHandler(collector)
    try:
        yield
    finally:
        if collector not in root.handlers:
            root.addHandler(collector)


# Parameter keys whose values are masked before a tool call is recorded to the
# in-memory ring buffer or persisted to disk. Tool kwargs can carry secrets
# (alarm/lock ``code``, tokens, passwords); these must never reach the usage
# log, which ``ha_report_issue`` surfaces and which is written verbatim to
# plaintext ``mcp_usage.jsonl``. Matching is by key name (case-insensitive) and
# recursive, so a secret nested under a non-sensitive key — e.g.
# ``ha_call_service(data={"code": ...})`` — is caught while ``data`` siblings
# like ``entity_id``/``brightness`` survive for debugging.
_SENSITIVE_PARAM_KEYS = frozenset(
    {
        "code",
        "pin",
        "password",
        "passwd",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "client_secret",
        "authorization",
        # Not a credential but user speech: ha_manage_pipeline(action='process')
        # takes whatever the user said, which is as likely to name a person or
        # an address as it is to turn on a light.
        "sentence",
    }
)
# Suffix match only for families with a low false-positive rate. Deliberately
# excludes ``_code``/``_key`` so ``status_code``/``cache_key`` stay readable;
# the exact ``code``/``pin``/``api_key`` entries above already cover the real
# Home Assistant service-call surface.
_SENSITIVE_KEY_SUFFIXES = ("_token", "_secret", "_password", "_passwd")
_REDACTED_VALUE = "[REDACTED]"
_MAX_REDACT_DEPTH = 6


def _is_sensitive_key(key: Any) -> bool:
    """Return True if a parameter key's value should be masked."""
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in _SENSITIVE_PARAM_KEYS or lowered.endswith(_SENSITIVE_KEY_SUFFIXES)


def _redact_parameters(value: Any, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with sensitive values masked by key name.

    Never mutates the input: it is the live tool ``kwargs`` dict, still
    referenced by the caller after this returns. Recurses through nested
    dicts/lists/tuples so secrets nested under non-sensitive keys are caught
    (a tuple is returned as a list). Fails closed past ``_MAX_REDACT_DEPTH`` —
    a subtree too deep to walk is masked wholesale rather than returned verbatim.
    """
    if _depth > _MAX_REDACT_DEPTH:
        return _REDACTED_VALUE
    if isinstance(value, dict):
        return {
            key: (
                _REDACTED_VALUE
                if _is_sensitive_key(key)
                else _redact_parameters(item, _depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_parameters(item, _depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Entries are dumped as JSON (log file, ha_report_issue); keep odd objects
    # from breaking that.
    return repr(value)


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
        # Only the file is gated: the in-memory ring buffer never touches disk
        # and ha_report_issue reads it, so it keeps working when the file is off.
        self._disk_enabled = True

        if log_file_path:
            self.log_file_path = Path(log_file_path)
        else:
            # Defer to the shared resolver so logs follow the same precedence
            # as the settings-UI tool config (HA_MCP_CONFIG_DIR > /data >
            # ~/.ha-mcp > tempdir). Avoids polluting the filesystem root when
            # HOME is unset and avoids surprising users who bind-mount a
            # writable volume via HA_MCP_CONFIG_DIR but find logs missing.
            self.log_file_path = get_data_dir() / "logs" / "mcp_usage.jsonl"

        try:
            self.log_file_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as e:
            # Directory creation failed (e.g., read-only filesystem). Surface
            # the reason instead of silently dropping every log — operators
            # otherwise see an empty mcp_usage.jsonl with no clue why.
            logger.warning(
                "Usage log file disabled — could not create %s (%s: %s). "
                "Set HA_MCP_CONFIG_DIR to a writable path to enable persistence.",
                self.log_file_path.parent,
                type(e).__name__,
                e,
            )
            self._disk_enabled = False
        else:
            try:
                _make_private_dir(self.log_file_path.parent)
                _make_private(self.log_file_path)
            except OSError as e:
                logger.warning(
                    "Usage log file disabled — %s is not private (%s: %s). "
                    "Set HA_MCP_CONFIG_DIR to a directory only this user can write.",
                    self.log_file_path,
                    type(e).__name__,
                    e,
                )
                self._disk_enabled = False

        # In-memory ring buffer for fast access to recent logs
        # Thread-safe: deque is thread-safe for append/pop operations
        self._ring_buffer: deque[dict[str, Any]] = deque(maxlen=ring_buffer_size)
        self._buffer_lock = threading.Lock()

        # Thread-safe queue for disk writes
        self._log_queue: Queue[ToolUsageLog] = Queue()
        self._logger_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        if self._disk_enabled:
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
                    try:
                        self._write_log_entry(log_entry)
                    finally:
                        # Lets a caller wait until the entry is written, not
                        # only taken off the queue.
                        self._log_queue.task_done()
                else:
                    # Sleep briefly to avoid busy waiting
                    threading.Event().wait(0.1)
            except Exception as e:  # noqa: BLE001
                # Silent error handling to avoid disrupting MCP server
                print(f"Usage logger error: {e}")

    def _write_log_entry(self, log_entry: ToolUsageLog) -> None:
        """Write single log entry to disk."""
        if not self._disk_enabled:
            return
        try:
            fd = _private_opener(str(self.log_file_path), _APPEND_FLAGS)
        except OSError as e:
            if (
                not isinstance(e, UnsafeLogPathError)
                and e.errno in _TRANSIENT_OPEN_ERRNOS
            ):
                logger.debug(
                    "Usage log entry dropped, %s could not be opened (%s); "
                    "the next entry retries.",
                    self.log_file_path,
                    e,
                )
                return
            # The path was swapped after startup for something we will not or
            # cannot open (a link, a FIFO, a file of another user). Stop writing
            # instead of dropping every entry silently or retrying a path someone
            # else controls on every tool call.
            self._disk_enabled = False
            logger.warning(
                "Usage log file disabled — could not open %s privately (%s: %s).",
                self.log_file_path,
                type(e).__name__,
                e,
            )
            return
        # Until open() returns a file object, nothing owns fd; close it here.
        try:
            f = open(fd, "a", encoding="utf-8")  # noqa: SIM115 - the with below owns it
        except Exception:  # noqa: BLE001
            os.close(fd)
            return
        try:
            with f:
                json.dump(asdict(log_entry), f, ensure_ascii=False)
                f.write("\n")
        except Exception:  # noqa: BLE001
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
            # Mask secrets (alarm/lock codes, tokens, passwords) before the entry
            # reaches the ring buffer or disk. Returns a copy — never mutates the
            # caller's live kwargs dict.
            redacted_parameters = _redact_parameters(parameters)
            log_entry = ToolUsageLog(
                timestamp=datetime.now(UTC).isoformat(),
                tool_name=tool_name,
                parameters=redacted_parameters,
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

            if self._disk_enabled:
                self._log_queue.put(log_entry)
        except Exception:  # noqa: BLE001
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
    return get_usage_logger().get_recent_entries(max_entries)
