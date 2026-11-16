"""A repository named ``owner/repo`` reaches the store as the store's own id.

``ha_manage_hacs`` and ``ha_get_hacs_info`` accept either the store's numeric
id or a GitHub ``owner/repo`` name; the name is looked up in the store's list
before the command is sent. Core 2026.11's Marketplace answers every command.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantCommandError
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.hacs_backend import HACS_API
from ha_mcp.tools.hacs_registration import wait_for_repo_registration
from ha_mcp.tools.tools_hacs import HacsTools

from ._client_spies import sent_commands
from ._hacs_marketplace import (
    CARD_ID,
    command_types,
    core_install,
    pooled_socket,
    sent_with,
    set_up_marketplace,
    stored,
)
from ._websocket_client_core import registered

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


async def test_a_refused_hacs_subscribe_falls_back_and_reports_cores_refusal(
    socket: HomeAssistantWebSocketClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Where HACS does not answer, the wait must try one list lookup instead
    of the subscription, report Core's refusal of it rather than "not found",
    and leave no subscription queue behind to collect events."""
    # A captured warning would keep the refusal, and through it the queue.
    caplog.set_level(logging.ERROR, logger="ha_mcp.tools.hacs_registration")
    with (
        registered() as seen,
        sent_commands(socket) as sent,
        patch.object(
            socket, "subscribe_command", wraps=socket.subscribe_command
        ) as subscribe,
        patch.object(
            socket, "unsubscribe_command", wraps=socket.unsubscribe_command
        ) as unsubscribe,
        pytest.raises(HomeAssistantCommandError) as refused,
    ):
        await wait_for_repo_registration(socket, CARD, timeout=10.0, api=HACS_API)

    assert [c.args[0] for c in subscribe.await_args_list] == ["hacs/subscribe"]
    assert command_types(sent) == ["hacs/repositories/list"]
    assert refused.value.code == "unknown_command"
    assert str(refused.value) == "Command failed: Unknown command."
    unsubscribe.assert_not_awaited()
    # The refusal's traceback holds the subscribe call's frame, and its queue.
    del refused
    assert len(seen.queues) == 1
    assert await seen.alive() == []
