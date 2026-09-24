"""Coordinate registry read-modify-write operations in one event loop.

Hold the resource lock from the fresh read through the write and verification.
Explicit replacements of the same resource must share it. This cannot serialize
external HA writers or other processes; see docs/registry-label-concurrency.md.
"""

import asyncio
from weakref import WeakValueDictionary

_LOCKS: WeakValueDictionary[
    tuple[asyncio.AbstractEventLoop, str, str], asyncio.Lock
] = WeakValueDictionary()


def registry_update_lock(registry: str, resource_id: str) -> asyncio.Lock:
    """Share a lock across tool/client instances, retaining only active locks."""
    key = (asyncio.get_running_loop(), registry, resource_id)
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock
