"""A HACS install that Core's Marketplace adopts keeps HA-MCP's way out (#2993).

Home Assistant 2026.11 hands the ``hacs`` config entry and the ``hacs.*``
storage files to its built-in Marketplace at the first start. HACS installs
can hold both HA-MCP listings as installed, and the Marketplace then refuses
to update either. ``hacs_install/`` is what HACS wrote (the E2E seed's files,
cut to the one installed record); the two HA-MCP records are added to it in
HACS's record shape. Core reads the entry from storage and adopts it as a
start does; the test asserts on what Core left, then on the component's
repair and the download reroute.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.repairs import repairs_flow_manager
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import STORAGE_DIR
from homeassistant.setup import async_setup_component
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from custom_components.ha_mcp_tools.const import DOMAIN, ISSUE_STORE_RECORDS_CONFLICT
from custom_components.ha_mcp_tools.store_records import (
    MAIN_REPOSITORY,
    MIRROR_REPOSITORY,
)
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.tools.tools_hacs import HacsTools

from ._config_flow_core import add_tools_entry
from ._hacs_marketplace import core_install, pooled_socket, prepare_marketplace
from ._marketplace_records import own_records, stored_record, unload_marketplace

HACS_INSTALL = Path(__file__).parent / "hacs_install"


def _leave_hacs_install(config_dir: Path) -> dict[str, Any]:
    """Write HACS's storage files, with both HA-MCP listings installed, into
    the config directory; returns Core's stored config entries."""
    storage = config_dir / STORAGE_DIR
    storage.mkdir(exist_ok=True)
    for name in ("hacs.hacs", "hacs.critical"):
        shutil.copyfile(HACS_INSTALL / name, storage / name)
    repositories = json.loads((HACS_INSTALL / "hacs.repositories").read_text())
    repositories["data"] |= own_records(main=True, mirror=True)
    (storage / "hacs.repositories").write_text(json.dumps(repositories))
    entries: dict[str, Any] = json.loads(
        (HACS_INSTALL / "core.config_entries").read_text()
    )
    return entries


@pytest.fixture
async def adopted(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[None]:
    """Home Assistant started on the HACS install, adopted by the Marketplace."""
    entries = await hass.async_add_executor_job(
        _leave_hacs_install, Path(hass.config.config_dir)
    )
    hass_storage[entries["key"]] = entries
    [hacs_entry] = entries["data"]["entries"]
    prepare_marketplace(hass, aioclient_mock)

    # A start reads the config entries from storage; the test's manager never did.
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    [entry] = hass.config_entries.async_entries("marketplace")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.entry_id == hacs_entry["entry_id"]
    for full_name in (MAIN_REPOSITORY, MIRROR_REPOSITORY):
        assert stored_record(hass_storage, full_name)["installed"] is True
    yield
    await unload_marketplace(hass)


async def _fix_the_repair(hass: HomeAssistant) -> dict[str, Any]:
    """Open the repair's fix flow and confirm it, as the Repairs dashboard does."""
    assert await async_setup_component(hass, "repairs", {})
    flows = repairs_flow_manager(hass)
    assert flows is not None
    opened = await flows.async_init(
        DOMAIN, context={"issue_id": ISSUE_STORE_RECORDS_CONFLICT}
    )
    return dict(await flows.async_configure(opened["flow_id"], {}))


@pytest.mark.usefixtures("adopted")
async def test_both_listings_from_hacs_get_the_repair_that_keeps_the_main_one(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    issue_registry: ir.IssueRegistry,
) -> None:
    """Without the repair the Marketplace refuses to update either listing."""
    await add_tools_entry(hass, loaded=True)
    assert issue_registry.async_get_issue(DOMAIN, ISSUE_STORE_RECORDS_CONFLICT)

    assert (await _fix_the_repair(hass))["type"] == "create_entry"

    assert stored_record(hass_storage, MAIN_REPOSITORY)["installed"] is True
    assert "installed" not in stored_record(hass_storage, MIRROR_REPOSITORY)


@pytest.mark.usefixtures("adopted")
async def test_a_download_of_the_forgotten_mirror_goes_to_the_main_listing(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Installing the mirror again would bring the duplicate back."""
    await add_tools_entry(hass, loaded=True)
    await _fix_the_repair(hass)
    socket = await pooled_socket(core_client)
    await socket.send_command("marketplace/warning/accept")

    with core_install() as install:
        result = await HacsTools(core_client).ha_manage_hacs(
            action="download", repository_id=MIRROR_REPOSITORY
        )

    assert install.await_args is not None
    assert install.await_args.args[0].data.full_name == MAIN_REPOSITORY
    [note] = result["warnings"]
    assert "rerouted" in note
    assert MAIN_REPOSITORY in note
