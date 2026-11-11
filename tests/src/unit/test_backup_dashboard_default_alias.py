"""The dashboard snapshot resolves the tools' ``default`` alias to Core's ``lovelace`` item."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

import ha_mcp.backup_manager as bm
from ha_mcp.client.rest_client import HomeAssistantClient

_CONFIG = {"views": [{"title": "Overview"}]}


async def test_fetch_dashboard_maps_default_alias_to_lovelace(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    core_client: HomeAssistantClient,
) -> None:
    """``url_path='default'`` must capture the ``lovelace`` config, not skip the snapshot."""
    # Core migrates a stored default config to its ``lovelace`` dashboard item.
    hass_storage["lovelace"] = {
        "version": 1,
        "minor_version": 1,
        "key": "lovelace",
        "data": {"config": _CONFIG},
    }
    assert await async_setup_component(hass, "lovelace", {})

    got = await bm.fetch_dashboard(core_client, "default")

    assert got is not None
    assert got["config"] == _CONFIG
    assert got["dashboard"]["id"] == "lovelace"
    assert got["dashboard"]["url_path"] == "lovelace"
    assert got["dashboard"]["mode"] == "storage"


async def test_legacy_read_of_lovelace_leaves_core_to_pick_the_store(
    hass: HomeAssistant, core_client: HomeAssistantClient
) -> None:
    """Without the item (deleted, never migrated) Core reads its default store."""
    assert await async_setup_component(hass, "lovelace", {})
    saved = await core_client.send_websocket_message(
        {"type": "lovelace/config/save", "config": _CONFIG}
    )
    assert saved["success"] is True

    got = await bm.fetch_dashboard(core_client, "lovelace")

    assert got == {"dashboard": None, "config": _CONFIG}
