"""A repository named ``owner/repo`` reaches the store as the store's own id.

``ha_manage_hacs`` and ``ha_get_hacs_info`` accept either the store's numeric
id or a GitHub ``owner/repo`` name; the name is looked up in the store's list
before the command is sent. Core 2026.11's Marketplace answers every command.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.tools_hacs import HacsTools

from ._client_spies import sent_commands
from ._hacs_marketplace import (
    CARD_ID,
    core_install,
    pooled_socket,
    sent_with,
    set_up_marketplace,
    stored,
)

CARD = "piitaya/lovelace-mushroom"


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.fixture
async def tools(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[HacsTools]:
    """The HACS tools on Core's Marketplace, its first-run warning accepted."""
    entry: MockConfigEntry = await set_up_marketplace(
        hass, aioclient_mock, hass_storage, {CARD_ID: stored(CARD, "plugin")}
    )
    tools = HacsTools(core_client)
    await tools.ha_manage_hacs(action="accept_warning", confirm=True)
    yield tools
    await hass.config_entries.async_unload(entry.entry_id)


async def test_an_owner_repo_name_installs_the_repository_it_names(
    tools: HacsTools, socket: HomeAssistantWebSocketClient
) -> None:
    """A download by ``owner/repo`` must install that repository, and say so by
    the name the store lists it under."""
    listed = await socket.send_command("marketplace/repositories/list")
    [row] = [r for r in listed["result"] if r["full_name"] == CARD]

    with sent_commands(socket) as spy, core_install() as install:
        result = await tools.ha_manage_hacs(action="download", repository_id=CARD)

    assert sent_with(spy, "marketplace/repositories/list")
    assert [
        s["repository"] for s in sent_with(spy, "marketplace/repository/install")
    ] == [CARD_ID]
    install.assert_awaited_once()
    assert result["data"]["repository_id"] == CARD_ID
    assert result["data"]["repository"] == row["name"]


async def test_a_numeric_id_skips_the_store_list(
    tools: HacsTools, socket: HomeAssistantWebSocketClient
) -> None:
    """A numeric id is already the store's: the tool must not pull the whole
    store list, which runs to megabytes on a busy install, to look it up."""
    with sent_commands(socket) as spy:
        result = await tools.ha_get_hacs_info(action="info", repository_id=CARD_ID)

    assert sent_with(spy, "marketplace/repositories/list") == []
    assert [
        s["repository_id"] for s in sent_with(spy, "marketplace/repository/info")
    ] == [CARD_ID]
    assert result["data"]["full_name"] == CARD
