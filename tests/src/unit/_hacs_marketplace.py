"""The stores the HACS tools talk to, on a real ``hass``.

Home Assistant 2026.11 ships the Marketplace: ``set_up_marketplace`` sets up
Core's own integration from its stored repositories, with GitHub unreachable
(Core's HTTP session answers every request with a connection error), so every
``marketplace/*`` reply is Core's. What only GitHub could make happen, an
install that downloads, is reached by replacing Core's install step and
raising Core's own exceptions from it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from homeassistant.components.marketplace.repositories.base import Repository
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockModule, mock_integration
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import (
    HomeAssistantWebSocketClient,
    websocket_manager,
)

MAIN = "homeassistant-ai/ha-mcp"
MIRROR = "homeassistant-ai/ha-mcp-integration"
MAIN_ID = "1056618941"
MIRROR_ID = "1289599380"
CARD_ID = "441028036"
REPOSITORIES_KEY = "marketplace.repositories"

# Core still starts the catalog fetch at startup (it fails offline); a stored
# fetch time keeps a later catalog answer from taking the stored record over.
_FETCHED = 1_700_000_000


def stored(
    full_name: str,
    category: str,
    *,
    installed: str | None = None,
    domain: str | None = None,
) -> dict[str, Any]:
    """A repository as the Marketplace stores it in ``marketplace.repositories``."""
    record: dict[str, Any] = {
        "full_name": full_name,
        "category": category,
        "last_fetched": _FETCHED,
    }
    if domain:
        record["domain"] = domain
    if installed:
        record |= {"installed": True, "version_installed": installed}
    return record


def _install_path(config_dir: Path, record: dict[str, Any]) -> Path | None:
    """Where an installed repository's files are, which Core checks at startup."""
    if not record.get("installed"):
        return None
    if record["category"] == "integration" and record.get("domain"):
        return config_dir / "custom_components" / record["domain"]
    if record["category"] == "plugin":
        return config_dir / "www" / "community" / record["full_name"].split("/")[1]
    return None


def add_marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    repositories: dict[str, dict[str, Any]],
) -> MockConfigEntry:
    """Core's Marketplace entry with ``repositories`` stored, not set up yet."""
    offline = aiohttp.ClientConnectionError("GitHub is not reachable in a unit test")
    for method in ("get", "post", "put", "patch", "delete"):
        getattr(aioclient_mock, method)(re.compile(".*"), exc=offline)
    store_repositories(hass_storage, repositories)
    config_dir = Path(hass.config.config_dir)
    for record in repositories.values():
        if (path := _install_path(config_dir, record)) is not None:
            path.mkdir(parents=True, exist_ok=True)
    # Core's frontend needs its web assets, which are not installed here.
    mock_integration(hass, MockModule("frontend"))
    entry = MockConfigEntry(domain="marketplace", data={})
    entry.add_to_hass(hass)
    return entry


def store_repositories(
    hass_storage: dict[str, Any], repositories: dict[str, dict[str, Any]]
) -> None:
    """Put ``repositories`` in Core's storage, where the Marketplace reads them."""
    hass_storage[REPOSITORIES_KEY] = {
        "version": 1,
        "key": REPOSITORIES_KEY,
        "data": repositories,
    }


async def set_up_marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    repositories: dict[str, dict[str, Any]],
) -> MockConfigEntry:
    """Core's Marketplace, loaded with ``repositories``, GitHub unreachable."""
    entry = add_marketplace(hass, aioclient_mock, hass_storage, repositories)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


@contextmanager
def core_install(**behaviour: Any) -> Iterator[MagicMock]:
    """Replace the Marketplace's install step, the one that downloads from GitHub.

    ``behaviour`` is ``side_effect=`` one of Core's exceptions for a refused
    install; without it the install succeeds. The Core command handler around
    it, and its reply, are Core's.
    """
    with patch.object(
        Repository, "async_install_repository", autospec=True, **behaviour
    ) as install:
        yield install


async def pooled_socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the tools send their store commands on."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def command_types(spy: AsyncMock) -> list[str]:
    return [call.args[0] for call in spy.call_args_list]


def sent_with(spy: AsyncMock, command: str) -> list[dict[str, Any]]:
    """The arguments of each ``command`` the spy saw, in order."""
    return [dict(call.kwargs) for call in spy.call_args_list if call.args[0] == command]
