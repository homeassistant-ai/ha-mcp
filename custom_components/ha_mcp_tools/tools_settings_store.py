"""Serialize ordinary and backup writes to File & YAML Tools permission stores."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

ALLOWED_PATHS_KEY = f"{DOMAIN}_allowed_paths"
EXTRA_YAML_KEYS_KEY = f"{DOMAIN}_extra_yaml_keys"
TOOLS_STORAGE_VERSION = 1


@dataclass
class ToolsStoreWrites:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0

    @asynccontextmanager
    async def writing(self) -> AsyncIterator[None]:
        async with self.lock:
            yield
            # Count even an identical save: its author may intentionally have
            # chosen to retain the values a running import will need to undo.
            self.revision += 1


def store_writes(hass: HomeAssistant, key: str) -> ToolsStoreWrites:
    writes: dict[str, ToolsStoreWrites] = hass.data.setdefault(
        "ha_mcp_tools_store_writes", {}
    )
    if key not in writes:
        writes[key] = ToolsStoreWrites()
    return writes[key]


async def save_allowed_paths(hass: HomeAssistant, paths: list[str]) -> None:
    store: Store = Store(hass, TOOLS_STORAGE_VERSION, ALLOWED_PATHS_KEY)
    await store.async_save({"paths": paths})


async def save_extra_yaml_keys(hass: HomeAssistant, keys: list[str]) -> None:
    store: Store = Store(hass, TOOLS_STORAGE_VERSION, EXTRA_YAML_KEYS_KEY)
    await store.async_save({"keys": keys})
