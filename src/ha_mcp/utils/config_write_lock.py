"""Process-wide async lock serialising config/policy read-modify-write.

Both the web settings handlers (async) and the developer tools (which run
their file I/O in ``asyncio.to_thread`` while holding this lock on the event
loop) acquire this single lock around any load-modify-save of
``tool_config.json`` or ``tool_policy.json``, so two concurrent writers can't
read the same on-disk state and then clobber each other's update.

Lazy construction mirrors ``settings_ui._persistence._get_override_file_lock``:
``asyncio.Lock`` binds to the running loop on first ``acquire()``, so a test
fixture with its own loop isn't locked into an import-time loop. Assumes the
single-uvicorn-loop deployment every ha-mcp server uses.

Leaf at import time (stdlib-only at module scope; ``data_paths`` is imported
lazily inside ``config_file_lock`` — keep it that way) so settings_ui, policy,
and tools can all depend on it without an import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import AsyncIterator, Iterator

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_CONFIG_WRITE_LOCK: asyncio.Lock | None = None


def get_config_write_lock() -> asyncio.Lock:
    """Return the shared config/policy write lock (lazy singleton)."""
    global _CONFIG_WRITE_LOCK
    if _CONFIG_WRITE_LOCK is None:
        _CONFIG_WRITE_LOCK = asyncio.Lock()
    return _CONFIG_WRITE_LOCK


@contextlib.asynccontextmanager
async def config_write_guard() -> AsyncIterator[None]:
    """Both write locks at once: in-process (asyncio) + cross-process (file).

    The one-liner for async writers: serializes against other coroutines in
    this process AND against the sidecar / other server processes. Thread
    writers (the dev tools' ``asyncio.to_thread`` sections) hold the asyncio
    lock on the loop and take ``config_file_lock()`` inside the thread
    instead.
    """
    async with get_config_write_lock():
        with config_file_lock():
            yield


@contextlib.contextmanager
def config_file_lock() -> Iterator[None]:
    """Advisory CROSS-PROCESS lock over the config/policy write files.

    The asyncio lock above only serializes writers within one process, but
    stdio deployments run the settings-UI sidecar as a SEPARATE process from
    the MCP server: a sidecar policy PUT and a dev-tool write could both read
    the same on-disk version and silently drop each other's update. Acquire
    this (an ``flock``/``msvcrt.locking`` on a lockfile in the data dir)
    inside the asyncio-locked sections, so the version compare-and-swap also
    holds across processes.

    Best-effort by design: on filesystems or platforms where locking fails,
    a warning is logged and the write proceeds (matching the previous
    behavior) rather than bricking config saves. Never acquire twice in one
    process (the asyncio lock already prevents that; nested flock on a second
    fd would self-block).
    """
    from .data_paths import get_data_dir

    fd: int | None = None
    locked = False
    try:
        try:
            fd = os.open(
                get_data_dir() / ".config_write.lock", os.O_CREAT | os.O_RDWR, 0o600
            )
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                locked = True
        except OSError:
            logger.warning(
                "config write lockfile unavailable; proceeding without "
                "cross-process lock",
                exc_info=True,
            )
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                if locked:
                    if fcntl is not None:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            with contextlib.suppress(OSError):
                os.close(fd)
