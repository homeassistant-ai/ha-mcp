"""The stores the HACS tools talk to, on a real ``hass``.

Home Assistant 2026.11 ships the Marketplace: ``set_up_marketplace`` sets up
Core's own integration from its stored repositories, with GitHub unreachable
(Core's HTTP session answers every request with a connection error), so every
``marketplace/*`` reply is Core's. What only GitHub could make happen, an
install that downloads, is reached by replacing Core's install step and
raising Core's own exceptions from it.

Older Home Assistant runs the HACS custom integration instead. It cannot be
installed here, so ``install_hacs`` registers its WebSocket commands, with
their schemas, in a mock integration; Core's WebSocket API still validates,
routes and answers for them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import probatio
from homeassistant.components import websocket_api
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

# Stored before the test, so the catalog fetch at startup is not needed.
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


async def set_up_marketplace(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    repositories: dict[str, dict[str, Any]],
) -> MockConfigEntry:
    """Core's Marketplace, loaded with ``repositories``, GitHub unreachable."""
    offline = aiohttp.ClientConnectionError("GitHub is not reachable in a unit test")
    for method in ("get", "post", "put", "patch", "delete"):
        getattr(aioclient_mock, method)(re.compile(".*"), exc=offline)
    hass_storage["marketplace.repositories"] = {
        "version": 1,
        "key": "marketplace.repositories",
        "data": repositories,
    }
    config_dir = Path(hass.config.config_dir)
    for record in repositories.values():
        if (path := _install_path(config_dir, record)) is not None:
            path.mkdir(parents=True, exist_ok=True)
    # Core's frontend needs its web assets, which are not installed here.
    mock_integration(hass, MockModule("frontend"))
    entry = MockConfigEntry(domain="marketplace", data={})
    entry.add_to_hass(hass)
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


@dataclass
class HacsIntegration:
    """What the HACS custom integration holds, and the commands it was sent."""

    repositories: list[dict[str, Any]]
    categories: list[str] = field(
        default_factory=lambda: ["integration", "plugin", "theme", "appdaemon"]
    )
    # Command type -> the message HACS refuses it with.
    refusals: dict[str, str] = field(default_factory=dict)
    received: list[dict[str, Any]] = field(default_factory=list)

    def commands(self) -> list[str]:
        return [msg["type"] for msg in self.received]


def install_hacs(hass: HomeAssistant, hacs: HacsIntegration) -> None:
    """Register the HACS integration's commands on Core, with their schemas."""
    mock_integration(hass, MockModule("hacs"))

    def register(command: str, schema: dict[Any, Any], answer: Any) -> None:
        @websocket_api.require_admin
        @websocket_api.websocket_command({probatio.Required("type"): command, **schema})
        @websocket_api.async_response
        async def handle(
            hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
        ) -> None:
            hacs.received.append(msg)
            if command in hacs.refusals:
                connection.send_error(msg["id"], "error", hacs.refusals[command])
                return
            if command == "hacs/subscribe":
                connection.subscriptions[msg["id"]] = lambda: None
            connection.send_result(msg["id"], answer(msg))

        websocket_api.async_register_command(hass, handle)

    def listed(msg: dict) -> list[dict[str, Any]]:
        if "categories" not in msg:
            return hacs.repositories
        return [r for r in hacs.repositories if r.get("category") in msg["categories"]]

    def info(msg: dict) -> dict[str, Any] | None:
        return next(
            (r for r in hacs.repositories if r["id"] == msg["repository_id"]), None
        )

    register("hacs/info", {}, lambda msg: {"categories": hacs.categories})
    register(
        "hacs/repositories/list",
        {probatio.Optional("categories"): [str]},
        listed,
    )
    register("hacs/repository/info", {probatio.Required("repository_id"): str}, info)
    register(
        "hacs/repository/download",
        {
            probatio.Required("repository"): str,
            probatio.Optional("version"): str,
        },
        lambda msg: None,
    )
    register(
        "hacs/repository/remove",
        {probatio.Required("repository"): str},
        lambda msg: None,
    )
    register("hacs/subscribe", {probatio.Required("signal"): str}, lambda msg: None)


async def pooled_socket(client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    """The pooled socket the tools send their store commands on."""
    return await websocket_manager.get_client(
        url=client.base_url, token=client.token, verify_ssl=False
    )


@contextmanager
def sent_commands(socket: HomeAssistantWebSocketClient) -> Iterator[AsyncMock]:
    """Spy on the socket's commands; Core still answers each one."""
    with patch.object(socket, "send_command", wraps=socket.send_command) as spy:
        yield spy


def command_types(spy: AsyncMock) -> list[str]:
    return [call.args[0] for call in spy.call_args_list]


def sent_with(spy: AsyncMock, command: str) -> list[dict[str, Any]]:
    """The arguments of each ``command`` the spy saw, in order."""
    return [dict(call.kwargs) for call in spy.call_args_list if call.args[0] == command]
