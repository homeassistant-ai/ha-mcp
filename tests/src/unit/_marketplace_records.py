"""Core's Marketplace manager holding this integration's store records (#2993).

The manager is built by Core's own class and restored from records in Core's
storage, so what the component's store-records check and fix read and write is
the Marketplace's own state. The store's config entry is not set up (that would
reach GitHub): it carries the manager the way the Marketplace's setup leaves it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.components.marketplace.enums import RepositoryCategory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration

from custom_components.ha_mcp_tools.store_records import (
    MAIN_REPOSITORY,
    MIRROR_REPOSITORY,
)

REPOSITORIES_KEY = "marketplace.repositories"
# GitHub ids, arbitrary: the Marketplace keys its stored records by them.
REPOSITORY_IDS = {MAIN_REPOSITORY: "101", MIRROR_REPOSITORY: "102"}


def store_entry(
    hass: HomeAssistant, domain: str, state: ConfigEntryState
) -> MockConfigEntry:
    """A config entry of the store integration, which the test does not load."""
    mock_integration(
        hass, MockModule(domain, async_unload_entry=AsyncMock(return_value=True))
    )
    entry = MockConfigEntry(domain=domain, state=state)
    entry.add_to_hass(hass)
    return entry


async def build_manager(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    entry: MockConfigEntry,
    *,
    restore: bool = True,
    main: bool | None = None,
    mirror: bool | None = None,
) -> MarketplaceManager:
    """The Marketplace's manager, restored from the store records given.

    ``main`` / ``mirror`` say whether the store holds that repository as
    installed; ``None`` leaves it out of the store.
    """
    records: dict[str, Any] = {}
    for full_name, installed in ((MAIN_REPOSITORY, main), (MIRROR_REPOSITORY, mirror)):
        if installed is None:
            continue
        record: dict[str, Any] = {"full_name": full_name, "category": "integration"}
        if installed:
            record |= {
                "installed": True,
                "version_installed": "v9.1.0",
                "installed_commit": "abc",
            }
        records[REPOSITORY_IDS[full_name]] = record
    hass_storage[REPOSITORIES_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": REPOSITORIES_KEY,
        "data": records,
    }
    # The manager reads the dashboards' resource mode.
    assert await async_setup_component(hass, "lovelace", {})
    manager = MarketplaceManager(hass, entry)
    manager.common.categories.add(RepositoryCategory.INTEGRATION)
    if restore:
        assert await manager.data.restore()
    return manager


async def loaded_marketplace(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    *,
    main: bool | None = None,
    mirror: bool | None = None,
) -> MarketplaceManager:
    """The Marketplace as loaded: its manager sits on its config entry."""
    entry = store_entry(hass, "marketplace", ConfigEntryState.LOADED)
    manager = await build_manager(hass, hass_storage, entry, main=main, mirror=mirror)
    entry.runtime_data = manager
    return manager


def stored_record(hass_storage: dict[str, Any], full_name: str) -> dict[str, Any]:
    """The record the Marketplace last saved for one repository."""
    record: dict[str, Any] = hass_storage[REPOSITORIES_KEY]["data"][
        REPOSITORY_IDS[full_name]
    ]
    return record
