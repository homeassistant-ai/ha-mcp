"""``ha_manage_hacs(action="download")`` forwards Core's install progress as
MCP progress: the numbers the panel's install dialog shows while a repository
installs. Progress is a nicety; whatever goes wrong with it leaves the install
alone and is logged.

The Marketplace is Core's own (``_hacs_marketplace``); GitHub answers where a
test serves it (``_hacs_actions_core``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.marketplace.const import DOMAIN as MARKETPLACE
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.tools import hacs_install_progress
from ha_mcp.tools.tools_hacs import HacsTools

from ._backup_snapshot_core import Heartbeats
from ._command_faults import arrival
from ._hacs_actions_core import serve_card, serve_files, serve_repository
from ._hacs_marketplace import (
    CARD_ID,
    add_marketplace,
    core_install,
    install_progress_events,
    pooled_socket,
    stored,
    suggestions,
)
from ._held_command import held_command
from ._tool_errors import rejected_call_body
from ._tools_addons_core import outwait, timers

CARD = "piitaya/lovelace-mushroom"
OTHER_CARD = "someone/other-card"
OTHER_CARD_ID = "600700800"
# Core lists only the categories it has active, and it activates themes only
# with the frontend's theme service, which the unit tests' frontend lacks.
UNLISTED_THEME = "someone/unlisted-theme"
UNLISTED_THEME_ID = "600700900"
INSTALL = "marketplace/repository/install"
LIST = "marketplace/repositories/list"
PROGRESS_LOGGER = hacs_install_progress.__name__


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> HacsTools:
    return HacsTools(core_client)


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


class HeldFile:
    """GitHub's answer for the other card's file, held until released."""

    def __init__(self) -> None:
        self.downloading = asyncio.Event()
        self.release = asyncio.Event()

    async def answer(
        self, method: str, url: Any, data: Any
    ) -> AiohttpClientMockResponse:
        self.downloading.set()
        await self.release.wait()
        return AiohttpClientMockResponse(method, url, text="other card")


@pytest.fixture
async def other_card_file() -> AsyncIterator[HeldFile]:
    held = HeldFile()
    yield held
    held.release.set()


@pytest.fixture
async def installable(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    socket: HomeAssistantWebSocketClient,
    other_card_file: HeldFile,
) -> AsyncIterator[MockConfigEntry]:
    """The Marketplace, its warning accepted, GitHub serving the card, a card
    the list leaves out, and another card whose file it holds
    (``other_card_file``)."""
    serve_card(aioclient_mock, CARD, int(CARD_ID))
    serve_files(aioclient_mock, CARD)
    serve_card(aioclient_mock, OTHER_CARD, int(OTHER_CARD_ID))
    serve_files(aioclient_mock, OTHER_CARD, side_effect=other_card_file.answer)
    serve_repository(
        aioclient_mock,
        UNLISTED_THEME,
        int(UNLISTED_THEME_ID),
        ("themes/unlisted.yaml",),
    )
    serve_files(aioclient_mock, UNLISTED_THEME)
    entry = add_marketplace(
        hass,
        aioclient_mock,
        hass_storage,
        {
            CARD_ID: stored(CARD, "plugin"),
            OTHER_CARD_ID: stored(OTHER_CARD, "plugin"),
            UNLISTED_THEME_ID: stored(UNLISTED_THEME, "theme"),
        },
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    await socket.send_command("marketplace/warning/accept")
    yield entry
    other_card_file.release.set()
    await hass.config_entries.async_unload(entry.entry_id)


def _progress_of(events: list[dict[str, Any]], full_name: str) -> list[int]:
    """The numbers Core dispatched for ``full_name``; False ends an install."""
    return [
        e["progress"]
        for e in events
        if e["repository"] == full_name and e["progress"] is not False
    ]


def _logged_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == PROGRESS_LOGGER and r.levelno == logging.WARNING
    ]


async def test_cores_progress_of_the_install_reaches_the_caller(
    installable: MockConfigEntry,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    heartbeats = Heartbeats()
    async with install_progress_events(socket) as dispatched:
        await tools.ha_manage_hacs(
            action="download", repository_id=CARD_ID, ctx=heartbeats.context
        )

    assert _progress_of(dispatched, CARD)
    assert [p for p, _ in heartbeats.reports] == _progress_of(dispatched, CARD)


async def test_another_repositorys_progress_is_not_forwarded(
    hass: HomeAssistant,
    installable: MockConfigEntry,
    other_card_file: HeldFile,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """Core installs one repository at a time, and one already installing
    when the call starts reports on the same signal while the call's own
    install waits for it."""
    heartbeats = Heartbeats()
    async with install_progress_events(socket) as dispatched:
        other = asyncio.create_task(
            socket.send_command(INSTALL, repository=OTHER_CARD_ID)
        )
        await other_card_file.downloading.wait()
        with arrival(hass, INSTALL) as arrived:
            call = asyncio.create_task(
                tools.ha_manage_hacs(
                    action="download",
                    repository_id=CARD_ID,
                    ctx=heartbeats.context,
                )
            )
            waiting = asyncio.create_task(arrived.wait())
            await asyncio.wait({waiting, call}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
        other_card_file.release.set()
        await asyncio.gather(call, other)

    assert _progress_of(dispatched, OTHER_CARD)
    assert [p for p, _ in heartbeats.reports] == _progress_of(dispatched, CARD)


async def test_an_install_goes_ahead_when_its_progress_cannot_be_followed(
    installable: MockConfigEntry,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    heartbeats = Heartbeats()
    with (
        core_install() as install,
        patch.object(
            socket,
            "subscribe_command",
            AsyncMock(side_effect=HomeAssistantConnectionError("lost")),
        ) as unanswered,
    ):
        result = await tools.ha_manage_hacs(
            action="download", repository_id=CARD_ID, ctx=heartbeats.context
        )

    assert unanswered.await_count == 1
    assert install.await_count == 1
    assert result["data"]["repository_id"] == CARD_ID
    assert heartbeats.reports == []
    # Logged where an operator sees it, not at debug.
    assert any("subscription failed" in w for w in _logged_warnings(caplog))


async def test_progress_that_stops_on_a_lost_list_read_is_logged(
    hass: HomeAssistant,
    installable: MockConfigEntry,
    other_card_file: HeldFile,
    tools: HacsTools,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A repository given by id has its name read from the list on its first
    progress; a read that gets no answer ends the forwarding, which the
    install's answer must not hide from the log."""
    heartbeats = Heartbeats()
    before = timers(hass)
    async with held_command(hass, LIST) as listing:
        call = asyncio.create_task(
            tools.ha_manage_hacs(
                action="download",
                repository_id=OTHER_CARD_ID,
                ctx=heartbeats.context,
            )
        )
        await listing.reached_by(call)
        await outwait(
            hass,
            DEFAULT_COMMAND_WAIT_TIMEOUT - 1,
            DEFAULT_COMMAND_WAIT_TIMEOUT,
            before,
        )
        other_card_file.release.set()
        result = await call

    assert result["data"]["repository_id"] == OTHER_CARD_ID
    assert heartbeats.reports == []
    assert any("stopped" in w for w in _logged_warnings(caplog))


async def test_a_repository_the_list_leaves_out_is_logged_not_followed(
    installable: MockConfigEntry,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    heartbeats = Heartbeats()
    result = await tools.ha_manage_hacs(
        action="download", repository_id=UNLISTED_THEME_ID, ctx=heartbeats.context
    )

    listed = await socket.send_command(LIST)
    assert UNLISTED_THEME_ID not in {r["id"] for r in listed["result"]}
    assert result["data"]["repository_id"] == UNLISTED_THEME_ID
    assert heartbeats.reports == []
    assert any(UNLISTED_THEME_ID in w for w in _logged_warnings(caplog))


async def test_a_repository_still_installing_points_at_waiting(
    installable: MockConfigEntry,
    other_card_file: HeldFile,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """Core refuses to uninstall a repository while it installs it."""
    install = asyncio.create_task(
        socket.send_command(INSTALL, repository=OTHER_CARD_ID)
    )
    await other_card_file.downloading.wait()
    body = await rejected_call_body(
        tools.ha_manage_hacs(action="remove", repository_id=OTHER_CARD_ID)
    )
    other_card_file.release.set()
    await install

    assert body["ha_error_code"] == "repository_busy"
    assert any("action='info'" in s for s in suggestions(body))


async def test_a_failed_install_releases_its_progress_subscription(
    installable: MockConfigEntry,
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    """The subscription is Core's dispatcher connection on the shared socket;
    a refused install must not leave it open."""
    failure = MarketplaceError(
        translation_domain=MARKETPLACE,
        translation_key="github_failed",
        translation_placeholders={"error": "502 Bad Gateway"},
    )
    with (
        core_install(side_effect=failure),
        patch.object(
            socket, "subscribe_command", wraps=socket.subscribe_command
        ) as subscribed,
        patch.object(
            socket, "release_subscription", wraps=socket.release_subscription
        ) as released,
    ):
        await rejected_call_body(
            tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID, ctx=Heartbeats().context
            )
        )

    [opened] = subscribed.await_args_list
    assert opened.args[0] == "marketplace/subscribe"
    assert released.await_count == 1
