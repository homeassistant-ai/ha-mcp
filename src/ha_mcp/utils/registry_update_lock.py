"""Coordinate registry read-modify-write operations in one event loop.

Hold the resource lock from the fresh read through the write and verification.
Explicit replacements of the same resource must share it. This cannot serialize
external HA writers or other processes; see docs/registry-label-concurrency.md.
"""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
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


@asynccontextmanager
async def registry_update_locks(
    registry: str,
    *resource_ids: str | None,
    lock: Callable[[str, str], asyncio.Lock] | None = None,
) -> AsyncIterator[None]:
    """Hold the lock of each given resource, in a stable order; None is skipped.

    A rename changes the lock key, so both IDs are held.
    """
    get_lock = lock or registry_update_lock
    async with AsyncExitStack() as locks:
        for resource_id in sorted({r for r in resource_ids if r is not None}):
            await locks.enter_async_context(get_lock(registry, resource_id))
        yield
