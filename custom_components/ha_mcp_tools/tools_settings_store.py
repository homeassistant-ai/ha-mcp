"""Serialize ordinary and backup writes to File & YAML Tools permission stores."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN

ALLOWED_PATHS_KEY = f"{DOMAIN}_allowed_paths"
EXTRA_YAML_KEYS_KEY = f"{DOMAIN}_extra_yaml_keys"
TOOLS_STORAGE_VERSION = 1
_STORE_ERROR_CODES = {
    ALLOWED_PATHS_KEY: "allowed_paths_store_unavailable",
    EXTRA_YAML_KEYS_KEY: "extra_yaml_keys_store_unavailable",
}


class ToolsStoreUnavailable(OSError):
    """A local File & YAML settings store could not be read or written."""

    def __init__(self, key: str) -> None:
        self.code = _STORE_ERROR_CODES[key]
        super().__init__("Could not persist or read settings (tools store unavailable)")


@dataclass
class ToolsStoreWrites:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0

    def bump(self) -> int:
        """Record a write under ``lock``; an import's undo checks the count."""
        self.revision += 1
        return self.revision

    @asynccontextmanager
    async def writing(self) -> AsyncIterator[None]:
        async with self.lock:
            yield
            # Count even an identical save: its author may intentionally have
            # chosen to retain the values a running import will need to undo.
            self.bump()


def store_writes(hass: HomeAssistant, key: str) -> ToolsStoreWrites:
    writes: dict[str, ToolsStoreWrites] = hass.data.setdefault(
        "ha_mcp_tools_store_writes", {}
    )
    if key not in writes:
        writes[key] = ToolsStoreWrites()
    return writes[key]


def _has_unread_store(path: Path) -> bool:
    # Core can return None for both an absent store and one it just renamed.
    # Look for the active name as well, since an empty JSON object yields None.
    try:
        with os.scandir(path.parent) as entries:
            return any(
                entry.name == path.name
                or entry.name.startswith(f"{path.name}.corrupt.")
                for entry in entries
            )
    except FileNotFoundError:
        return False


async def load_tools_setting(
    hass: HomeAssistant, key: str, field: str
) -> list[str] | None:
    """Return None only for a genuinely absent store, preserving empty lists."""
    store: Store[dict[str, list[str]]] = Store(hass, TOOLS_STORAGE_VERSION, key)
    try:
        data = await store.async_load()
        if data is None:
            if not await hass.async_add_executor_job(
                _has_unread_store, Path(store.path)
            ):
                return None
        elif isinstance(data, dict):
            values = data.get(field)
            if isinstance(values, list) and all(isinstance(v, str) for v in values):
                return values
    except (
        HomeAssistantError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        NotImplementedError,
    ) as err:
        raise ToolsStoreUnavailable(key) from err
    raise ToolsStoreUnavailable(key)


async def _save_values(
    hass: HomeAssistant, key: str, field: str, values: list[str]
) -> None:
    store: Store = Store(hass, TOOLS_STORAGE_VERSION, key)
    try:
        await store.async_save({field: values})
    except (HomeAssistantError, OSError) as err:
        raise ToolsStoreUnavailable(key) from err
    # Core logs some write errors and returns normally. A fresh Store must
    # observe the requested value before the caller may update its live cache.
    if await load_tools_setting(hass, key, field) != values:
        raise ToolsStoreUnavailable(key)


async def save_allowed_paths(hass: HomeAssistant, paths: list[str]) -> None:
    await _save_values(hass, ALLOWED_PATHS_KEY, "paths", paths)


async def save_extra_yaml_keys(hass: HomeAssistant, keys: list[str]) -> None:
    await _save_values(hass, EXTRA_YAML_KEYS_KEY, "keys", keys)
