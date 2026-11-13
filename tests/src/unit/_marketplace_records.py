"""This integration's store records, held by the store managers (#2993).

Core sets its Marketplace up from records in Core's storage
(``_hacs_marketplace``), so what the component's store-records check and fix
read and write is the Marketplace's own state. HACS is not part of Core and
cannot be installed here: it is a config entry of a ``mock_integration``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from homeassistant.components.marketplace.base import MarketplaceManager
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.setup import async_setup_component
from pytest_homeassistant.common import (
    MockConfigEntry,
    MockModule,
    mock_config_flow,
    mock_integration,
    mock_platform,
)
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
    store_repositories,
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


class HacsConfigFlow(ConfigFlow):
    """The flow handler Core reads a HACS entry's version from before setup."""

    VERSION = 1
    MINOR_VERSION = 1


class Hacs:
    """A HACS config entry whose setup publishes ``manager`` in ``hass.data``,
    as HACS does, and is not ready while there is none."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.manager: MarketplaceManager | None = None
        mock_integration(
            hass,
            MockModule(
                "hacs",
                async_setup_entry=self.setup_entry,
                async_unload_entry=self.unload_entry,
            ),
        )
        # Core imports an integration's config flow before setting up its entry.
        mock_platform(hass, "hacs.config_flow")
        # The flow's version: an entry of another version would be migrated.
        self.entry = MockConfigEntry(
            domain="hacs",
            version=HacsConfigFlow.VERSION,
            minor_version=HacsConfigFlow.MINOR_VERSION,
        )
        self.entry.add_to_hass(hass)

    # Core refuses a boolean from a built-in integration's entry methods.
    async def setup_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        if self.manager is None:
            raise ConfigEntryNotReady("HACS has not started yet")
        hass.data["hacs"] = self.manager

    async def unload_entry(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        hass.data.pop("hacs", None)

    async def restore(
        self,
        hass: HomeAssistant,
        hass_storage: dict[str, Any],
        *,
        main: bool | None = None,
        mirror: bool | None = None,
    ) -> MarketplaceManager:
        """HACS's manager, restored from the records given.

        Core's Marketplace manager plays it: Core took that shape over from
        HACS, and HACS's own cannot be installed here.
        """
        store_repositories(hass_storage, own_records(main=main, mirror=mirror))
        # The manager reads the dashboards' resource mode.
        assert await async_setup_component(hass, "lovelace", {})
        manager = MarketplaceManager(hass, self.entry)
        assert await manager.data.restore()
        self.manager = manager
        return manager


@contextmanager
def hacs_store(hass: HomeAssistant) -> Iterator[Hacs]:
    """HACS, with its flow handler registered until the context ends."""
    with mock_config_flow("hacs", HacsConfigFlow):
        yield Hacs(hass)


def stored_record(hass_storage: dict[str, Any], full_name: str) -> dict[str, Any]:
    """The record the Marketplace last saved for one repository."""
    record: dict[str, Any] = hass_storage[REPOSITORIES_KEY]["data"][
        REPOSITORY_IDS[full_name]
    ]
    return record
