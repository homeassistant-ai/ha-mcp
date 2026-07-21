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

Leaf module (only stdlib imports) so settings_ui, policy, and tools can all
depend on it without an import cycle.
"""

from __future__ import annotations

import asyncio

_CONFIG_WRITE_LOCK: asyncio.Lock | None = None


def get_config_write_lock() -> asyncio.Lock:
    """Return the shared config/policy write lock (lazy singleton)."""
    global _CONFIG_WRITE_LOCK
    if _CONFIG_WRITE_LOCK is None:
        _CONFIG_WRITE_LOCK = asyncio.Lock()
    return _CONFIG_WRITE_LOCK
