"""The Marketplace the store tools talk to, on a real ``hass``.

``set_up_marketplace`` sets up Core's own integration from its stored
repositories, with GitHub unreachable (Core's HTTP session answers every
request with a connection error), so every ``marketplace/*`` reply is Core's.
A test that needs GitHub to answer, an install that downloads among them,
serves it first (``_hacs_actions_core``).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import aiohttp
from homeassistant.components.marketplace.enums import MarketplaceSignal
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
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
CRITICAL_KEY = "marketplace.critical"

# Core still starts the catalog fetch at startup (it fails offline); a stored
# fetch time keeps a later catalog answer from taking the stored record over.
_FETCHED = 1_700_000_000


def stored(
    full_name: str,
    category: str,
    *,
    installed: str | None = None,
    domain: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """A repository as the Marketplace stores it in ``marketplace.repositories``;
    ``fields`` are more of its stored keys (``new``, ``stargazers_count``, ...)."""
    record: dict[str, Any] = {
        "full_name": full_name,
        "category": category,
        "last_fetched": _FETCHED,
        **fields,
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


def prepare_marketplace(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """What the Marketplace's setup needs in a unit test: GitHub unreachable."""
    offline = aiohttp.ClientConnectionError("GitHub is not reachable in a unit test")
    for method in ("get", "post", "put", "patch", "delete"):
        getattr(aioclient_mock, method)(re.compile(".*"), exc=offline)


def add_marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    repositories: dict[str, dict[str, Any]],
) -> MockConfigEntry:
    """Core's Marketplace entry with ``repositories`` stored, not set up yet."""
    prepare_marketplace(hass, aioclient_mock)
    store_repositories(hass_storage, repositories)
    config_dir = Path(hass.config.config_dir)
    for record in repositories.values():
        if (path := _install_path(config_dir, record)) is not None:
            path.mkdir(parents=True, exist_ok=True)
    entry = MockConfigEntry(domain="marketplace", data={})
    entry.add_to_hass(hass)
    return entry


def serve_removed(aioclient_mock: AiohttpClientMocker, rows: list[Any]) -> None:
    """The catalog's feed of removed repositories answers with ``rows``.

    Call it before the Marketplace is added: ``aioclient_mock`` answers with
    the first match, and ``add_marketplace`` refuses every other request.
    """
    aioclient_mock.get(re.compile(r".*/removed/data\.json$"), json=rows)


def store_critical(hass_storage: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """The critical repositories the Marketplace saved from the catalog."""
    hass_storage[CRITICAL_KEY] = {"version": 1, "key": CRITICAL_KEY, "data": rows}


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


@asynccontextmanager
async def install_progress_events(
    socket: HomeAssistantWebSocketClient,
) -> AsyncIterator[list[dict[str, Any]]]:
    """Every install progress Core dispatched while the block ran, as
    ``marketplace/subscribe`` streams it."""
    sub_id, queue = await socket.subscribe_command(
        "marketplace/subscribe",
        signal=MarketplaceSignal.REPOSITORY_INSTALL_PROGRESS.value,
    )
    events: list[dict[str, Any]] = []
    try:
        yield events
    finally:
        # Releasing the subscription drops what is still queued.
        while not queue.empty():
            events.append(queue.get_nowait()["event"])
        await socket.release_subscription(sub_id)


async def pooled_socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the tools send their store commands on."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


def command_types(spy: AsyncMock) -> list[str]:
    return [call.args[0] for call in spy.call_args_list]


def store_commands(spy: AsyncMock) -> list[str]:
    """The ``marketplace/*`` commands the spy saw, in order."""
    return [c for c in command_types(spy) if c.startswith("marketplace/")]


def suggestions(body: dict[str, Any]) -> list[str]:
    """Every suggestion of a rejection body, one or several."""
    return body["error"].get("suggestions") or [body["error"]["suggestion"]]


def sent_with(spy: AsyncMock, command: str) -> list[dict[str, Any]]:
    """The arguments of each ``command`` the spy saw, in order."""
    return [dict(call.kwargs) for call in spy.call_args_list if call.args[0] == command]
