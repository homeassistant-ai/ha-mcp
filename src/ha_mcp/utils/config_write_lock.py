"""Event-loop and file locks serialising config/policy read-modify-write.

The web settings handlers (async) and the developer tools (which run their
file I/O in ``asyncio.to_thread`` while holding the loop's lock) take these
locks around any load-modify-save of ``tool_config.json``,
``tool_policy.json`` or ``feature_flags.json``, and the settings import takes
the file lock around every file it replaces, so two concurrent writers can't
read the same on-disk state and then clobber each other's update.

An embedded server reload can start a new worker loop in the same process,
so each running loop has its own asyncio lock. The file lock serializes
writers across loops, threads and processes.

Leaf at import time apart from the dependency-light ``runtime_config`` module;
``data_paths`` is imported lazily inside ``config_file_lock``. Keep those
dependencies free of settings and tool modules so their callers do not form an
import cycle.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from ..runtime_config import file_write_lock

_CONFIG_WRITE_LOCKS: WeakValueDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    WeakValueDictionary()
)


def get_config_write_lock() -> asyncio.Lock:
    """Share the lock with other writers on the running event loop."""
    loop = asyncio.get_running_loop()
    lock = _CONFIG_WRITE_LOCKS.get(loop)
    if lock is None:
        # Owners and waiters keep the lock alive. A plain dict would keep every
        # retired loop as a key, and weak keys would not drop a contended lock,
        # which references its loop.
        lock = asyncio.Lock()
        _CONFIG_WRITE_LOCKS[loop] = lock
    return lock


@contextlib.asynccontextmanager
async def config_write_guard() -> AsyncIterator[None]:
    """Both write locks at once: in-process (asyncio) + cross-process (file).

    The one-liner for async writers: serializes against other coroutines on
    this loop AND, through the file lock, against other loops, threads and
    processes such as the sidecar. Thread
    writers (the dev tools' ``asyncio.to_thread`` sections) hold the asyncio
    lock on the loop and take ``config_file_lock()`` inside the thread
    instead.

    The file lock is acquired and released in a worker thread: ``flock`` is a
    blocking syscall, and taking it on the event loop would stall every
    connected client whenever another process holds the lock (or the data dir
    sits on a filesystem where locking is slow). Cross-thread enter/exit is
    safe — flock and msvcrt locks belong to the open file description, not
    the acquiring thread.
    """
    async with get_config_write_lock():
        file_lock = config_file_lock()
        await asyncio.to_thread(file_lock.__enter__)
        try:
            yield
        finally:
            await asyncio.to_thread(file_lock.__exit__, None, None, None)


@contextlib.contextmanager
def config_file_lock(data_dir: Path | None = None) -> Iterator[None]:
    """Advisory CROSS-PROCESS lock over the config/policy write files.

    ``data_dir`` overrides the lockfile directory (default: the live data
    dir) — pass it when operating on an explicit directory, as the policy
    migration does, so the lock lives beside the files it guards.

    The asyncio lock above only serializes writers on one event loop, but
    stdio deployments run the settings-UI sidecar as a SEPARATE process from
    the MCP server: a sidecar policy PUT and a dev-tool write could both read
    the same on-disk version and silently drop each other's update. Acquire
    this (an ``flock``/``msvcrt.locking`` on a lockfile in the data dir)
    inside the asyncio-locked sections, so the version compare-and-swap also
    holds across processes.

    Best-effort by design: on filesystems or platforms where locking fails,
    a warning is logged and the write proceeds (matching the previous
    behavior) rather than bricking config saves. Never nest it in one thread:
    a second fd would block on the first. Separate threads or loops contend
    on separate fds, which is the point.
    """
    from .data_paths import get_data_dir

    lock_dir = data_dir if data_dir is not None else get_data_dir()
    with file_write_lock(lock_dir / ".config_write.lock"):
        yield


def run_with_file_lock(fn: Callable[..., Any], /, *args: Any) -> Any:
    """Run ``fn(*args)`` holding the cross-process config file lock.

    Thread-side companion of ``config_write_guard()`` and the single entry
    point every load-modify-save uses from inside ``asyncio.to_thread``:
    callers hold the loop's asyncio lock and never hold the file lock in the
    worker thread already.
    """
    with config_file_lock():
        return fn(*args)
