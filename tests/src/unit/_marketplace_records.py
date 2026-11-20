"""This integration's store records, held by the Marketplace (#2993).

Core sets its Marketplace up from records in Core's storage
(``_hacs_marketplace``), so what the component's store-records check and fix
read and write is the Marketplace's own state.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.core import HomeAssistant
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_mcp_tools.store_records import (
    MAIN_REPOSITORY,
    MIRROR_REPOSITORY,
)

from ._hacs_marketplace import (
    MAIN_ID,
    MIRROR_ID,
    REPOSITORIES_KEY,
    set_up_marketplace,
    stored,
)

REPOSITORY_IDS = {MAIN_REPOSITORY: MAIN_ID, MIRROR_REPOSITORY: MIRROR_ID}


def own_records(
    *, main: bool | None = None, mirror: bool | None = None
) -> dict[str, dict[str, Any]]:
    """This integration's store records.

    ``main`` / ``mirror`` say whether the store holds that repository as
    installed; ``None`` leaves it out of the store.
    """
    records: dict[str, dict[str, Any]] = {}
    for full_name, installed in ((MAIN_REPOSITORY, main), (MIRROR_REPOSITORY, mirror)):
        if installed is not None:
            version = "v9.1.0" if installed else None
            records[REPOSITORY_IDS[full_name]] = stored(
                full_name, "integration", installed=version
            )
    return records


async def loaded_marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    *,
    main: bool | None = None,
    mirror: bool | None = None,
) -> MarketplaceManager:
    """The manager Core's Marketplace setup put on its config entry."""
    entry = await set_up_marketplace(
        hass, aioclient_mock, hass_storage, own_records(main=main, mirror=mirror)
    )
    manager: MarketplaceManager = entry.runtime_data
    return manager


async def unload_marketplace(hass: HomeAssistant) -> None:
    """Unload the Marketplace, whose recurring tasks would outlive the test."""
    for entry in hass.config_entries.async_loaded_entries("marketplace"):
        assert await hass.config_entries.async_unload(entry.entry_id)


def stored_record(hass_storage: dict[str, Any], full_name: str) -> dict[str, Any]:
    """The record the Marketplace last saved for one repository."""
    record: dict[str, Any] = hass_storage[REPOSITORIES_KEY]["data"][
        REPOSITORY_IDS[full_name]
    ]
    return record
