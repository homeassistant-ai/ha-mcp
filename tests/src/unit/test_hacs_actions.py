"""The HACS action tools on Core's Marketplace: what each action sends, what it
returns, and how a refusal, an error or a missing answer reaches the caller.

The Marketplace is Core's own (``_hacs_marketplace``). GitHub answers only
where a test serves it (``_hacs_actions_core``); everywhere else it is
unreachable, as Core's own replies then show.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.marketplace.const import DOMAIN as MARKETPLACE
from homeassistant.components.marketplace.exceptions import MarketplaceError
from homeassistant.components.marketplace.repositories.base import Repository
from homeassistant.components.marketplace.repositories.plugin import (
    PluginRepository,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockUser
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.server.auth.auth import AccessToken
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.tools.tools_hacs import HacsTools

from ._client_spies import sent_commands
from ._command_faults import received
from ._hacs_actions_core import add_connected_marketplace, serve_card
from ._hacs_marketplace import (
    CARD_ID,
    add_marketplace,
    command_types,
    core_install,
    pooled_socket,
    sent_with,
    stored,
)
from ._held_command import held_command
from ._oauth_server_core import signed_in
from ._tools_addons_core import outwait, timers

CARD = "piitaya/lovelace-mushroom"
STORE_ONLY_ID = "401454435"
TRACKER_ID = "512345678"
TRACKER_DOMAIN = "tracker_card_sync"
REPOSITORIES = {
    CARD_ID: stored(CARD, "plugin", installed="v4.0.0"),
    STORE_ONLY_ID: stored("hif2k1/battery_sim", "integration"),
    TRACKER_ID: stored(
        "someone/tracker", "integration", installed="v1.0.0", domain=TRACKER_DOMAIN
    ),
}
INFO = "marketplace/info"
REPO_INFO = "marketplace/repository/info"
INSTALL = "marketplace/repository/install"
UNINSTALL = "marketplace/repository/uninstall"
REFRESH = "marketplace/repository/refresh"
ADD = "marketplace/repositories/add"
LIST = "marketplace/repositories/list"


async def _set_up(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> HacsTools:
    return HacsTools(core_client)


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.fixture
async def marketplace(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[MockConfigEntry]:
    """The Marketplace with GitHub answering for the card alone."""
    serve_card(aioclient_mock, CARD, int(CARD_ID))
    entry = add_marketplace(hass, aioclient_mock, hass_storage, REPOSITORIES)
    yield await _set_up(hass, entry)
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def accepted(marketplace: MockConfigEntry, tools: HacsTools) -> None:
    """The server's user accepted the Marketplace's first-run warning."""
    await tools.ha_manage_hacs(action="accept_warning", confirm=True)


def _text(excinfo: pytest.ExceptionInfo[ToolError]) -> str:
    return str(excinfo.value)


def _store_commands(spy: AsyncMock) -> list[str]:
    """The ``marketplace/*`` commands the spy saw, in order."""
    return [c for c in command_types(spy) if c.startswith("marketplace/")]


def _caller(ha_token: str) -> AccessToken:
    return AccessToken(
        token="mcp-access-token",
        client_id="mcp-client",
        scopes=[],
        claims={"ha_token": ha_token},
    )


async def test_each_oauth_caller_reaches_the_store_as_themselves(
    hass: HomeAssistant,
    marketplace: MockConfigEntry,
    core_client: HomeAssistantClient,
    hass_access_token: str,
    hass_owner_user: MockUser,
) -> None:
    """One registered tool serves every OAuth user; each call must carry its
    own caller's token, or one user's acceptance would act for another."""
    owner = await hass.auth.async_create_refresh_token(hass_owner_user, "mcp-client")
    tokens = [hass_access_token, hass.auth.async_create_access_token(owner)]
    proxy = OAuthProxyClient(core_client.base_url)
    hacs = HacsTools(proxy)
    try:
        with signed_in(_caller(tokens[0])):
            await hacs.ha_manage_hacs(action="accept_warning", confirm=True)
        seen = []
        for token in tokens:
            with signed_in(_caller(token)):
                result = await hacs.ha_get_hacs_info(action="search")
            seen.append(result["data"]["store"]["warning_accepted"])
    finally:
        await proxy.close()

    assert seen == [True, False]


class TestInfo:
    async def test_a_numeric_id_is_read_without_a_lookup(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_get_hacs_info(action="info", repository_id=CARD_ID)

        assert _store_commands(spy) == [INFO, REPO_INFO]
        data = result["data"]
        assert data["repository_id"] == CARD_ID
        assert data["full_name"] == CARD
        assert data["installed"] is True

    async def test_an_unknown_repository_keeps_the_stores_answer(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_hacs_info(action="info", repository_id="999999")
        text = _text(excinfo)
        assert "The Marketplace does not know the repository with ID 999999" in text
        assert REPO_INFO in text

    async def test_a_store_answer_is_not_rewrapped_as_an_internal_error(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_get_hacs_info(action="info", repository_id="nobody/missing")
        assert "RESOURCE_NOT_FOUND" in _text(excinfo)
        assert "INTERNAL_ERROR" not in _text(excinfo)

    async def test_a_lost_connection_names_the_tool_and_action(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with (
            patch.object(
                socket,
                "send_command",
                AsyncMock(side_effect=HomeAssistantConnectionError("Connection lost")),
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_get_hacs_info(action="search")
        text = _text(excinfo)
        assert "ha_get_hacs_info" in text
        assert '"action": "search"' in text


@pytest.mark.usefixtures("accepted")
class TestDownload:
    async def test_no_version_installs_the_latest(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, core_install() as install:
            result = await tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID
            )

        assert [s["repository"] for s in sent_with(spy, INSTALL)] == [CARD_ID]
        assert install.await_args is not None
        assert install.await_args.kwargs["ref"] is None
        data = result["data"]
        assert data["version"] == "latest"
        assert "Successfully installed" in data["message"]

    async def test_an_owner_repo_path_installs_the_listed_repository(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        listed = await socket.send_command(LIST)
        [row] = [r for r in listed["result"] if r["full_name"] == CARD]
        with sent_commands(socket) as spy, core_install():
            result = await tools.ha_manage_hacs(action="download", repository_id=CARD)

        assert [s["repository"] for s in sent_with(spy, INSTALL)] == [CARD_ID]
        assert result["data"]["repository"] == row["name"]

    async def test_a_refused_install_keeps_the_stores_reason(
        self, tools: HacsTools
    ) -> None:
        failure = MarketplaceError(
            translation_domain=MARKETPLACE,
            translation_key="github_failed",
            translation_placeholders={"error": "502 Bad Gateway"},
        )
        with core_install(side_effect=failure), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id=CARD_ID)
        text = _text(excinfo)
        assert "502 Bad Gateway" in text
        assert INSTALL in text

    async def test_a_store_timeout_reports_the_installs_own_budget(
        self, tools: HacsTools
    ) -> None:
        with (
            core_install(side_effect=TimeoutError()),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_manage_hacs(action="download", repository_id=CARD_ID)
        assert f"Operation '{INSTALL}' timed out after 60.0s" in _text(excinfo)

    async def test_no_answer_says_the_install_may_still_complete(
        self, hass: HomeAssistant, tools: HacsTools
    ) -> None:
        before = timers(hass)
        with core_install():
            async with held_command(hass, INSTALL):
                call = asyncio.create_task(
                    tools.ha_manage_hacs(action="download", repository_id=CARD_ID)
                )
                await outwait(hass, 59, 60, before)
                await asyncio.wait([call])
        with pytest.raises(ToolError) as excinfo:
            call.result()
        text = _text(excinfo)
        assert f"Operation '{INSTALL}' timed out after 60.0s" in text
        assert "may still have completed" in text


class TestAddRepository:
    @pytest.fixture
    async def connected(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        tools: HacsTools,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
    ) -> AsyncIterator[MockConfigEntry]:
        """The Marketplace with a GitHub account connected and its warning
        accepted; GitHub answers for two cards, one of them archived."""
        serve_card(aioclient_mock, "owner/my-card", 777)
        serve_card(aioclient_mock, "owner/archived-card", 778, archived=True)
        entry = add_connected_marketplace(
            hass, aioclient_mock, hass_storage, REPOSITORIES
        )
        await _set_up(hass, entry)
        await tools.ha_manage_hacs(action="accept_warning", confirm=True)
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_an_added_repository_returns_the_id_it_registered_under(
        self,
        hass: HomeAssistant,
        connected: MockConfigEntry,
        tools: HacsTools,
    ) -> None:
        with received(hass, ADD) as seen:
            result = await tools.ha_manage_hacs(
                action="add_repository", repository="owner/my-card", category="lovelace"
            )

        # HA-MCP's "lovelace" reaches the store as its own "plugin".
        assert [(m["repository"], m["category"]) for m in seen[ADD]] == [
            ("owner/my-card", "plugin")
        ]
        assert result["data"]["repository_id"] == "777"

    async def test_an_add_that_never_registers_is_a_failure(
        self, connected: MockConfigEntry, tools: HacsTools
    ) -> None:
        """The store accepts an archived repository's add and registers nothing."""
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="add_repository",
                repository="owner/archived-card",
                category="lovelace",
            )
        text = _text(excinfo)
        assert "SERVICE_CALL_FAILED" in text
        assert "did not register" in text

    async def test_a_path_without_owner_is_refused_before_the_store(
        self,
        connected: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="add_repository", repository="no-slash", category="integration"
            )
        assert "VALIDATION_INVALID_PARAMETER" in _text(excinfo)
        assert "owner/repo" in _text(excinfo)
        assert _store_commands(spy) == [INFO]


@pytest.mark.usefixtures("accepted")
class TestRemove:
    async def test_the_installed_state_is_checked_before_uninstalling(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)

        assert _store_commands(spy) == [INFO, REPO_INFO, UNINSTALL]
        # The store's API is asymmetric: info takes repository_id, uninstall
        # takes repository.
        assert [s["repository_id"] for s in sent_with(spy, REPO_INFO)] == [CARD_ID]
        assert [s["repository"] for s in sent_with(spy, UNINSTALL)] == [CARD_ID]
        # Deleting the files does not unload a loaded integration.
        assert "restart" in result["data"]["note"]

    async def test_a_numeric_id_is_answered_with_the_repositorys_name(
        self, tools: HacsTools
    ) -> None:
        result = await tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)
        assert result["data"]["repository"] == CARD
        assert CARD in result["data"]["message"]

    async def test_success_sits_beside_the_timezone_metadata(
        self, tools: HacsTools
    ) -> None:
        result = await tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)
        assert result["success"] is True
        assert result["data"]["repository_id"] == CARD_ID
        assert "home_assistant_timezone" in result["metadata"]

    async def test_an_owner_repo_path_uninstalls_the_listed_repository(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy:
            await tools.ha_manage_hacs(action="remove", repository_id=CARD)
        assert [s["repository"] for s in sent_with(spy, UNINSTALL)] == [CARD_ID]

    async def test_a_blank_id_sends_nothing(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id="   ")
        assert "repository_id" in _text(excinfo)
        spy.assert_not_awaited()

    async def test_a_repository_never_downloaded_is_not_reported_removed(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id=STORE_ONLY_ID)
        assert "RESOURCE_NOT_FOUND" in _text(excinfo)
        assert "not downloaded" in _text(excinfo)
        assert UNINSTALL not in _store_commands(spy)

    async def test_a_failed_installed_check_does_not_block_the_removal(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with patch.object(
            PluginRepository,
            "update_repository",
            autospec=True,
            side_effect=TimeoutError(),
        ):
            # Core refuses the info command while its GitHub step fails.
            with pytest.raises(HomeAssistantCommandError):
                await socket.send_command(REPO_INFO, repository_id=CARD_ID)
            with sent_commands(socket) as spy:
                result = await tools.ha_manage_hacs(
                    action="remove", repository_id=CARD_ID
                )

        assert result["success"] is True
        assert _store_commands(spy)[-1] == UNINSTALL
        info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        assert info["result"]["installed"] is False

    async def test_a_refused_uninstall_keeps_the_stores_reason(
        self, hass: HomeAssistant, tools: HacsTools
    ) -> None:
        MockConfigEntry(domain=TRACKER_DOMAIN).add_to_hass(hass)
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="remove", repository_id=TRACKER_ID)
        text = _text(excinfo)
        assert "someone/tracker is still set up" in text
        assert UNINSTALL in text

    async def test_a_store_timeout_reports_the_uninstalls_own_budget(
        self, tools: HacsTools
    ) -> None:
        with (
            patch.object(
                Repository, "uninstall", autospec=True, side_effect=TimeoutError()
            ),
            pytest.raises(ToolError) as excinfo,
        ):
            await tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)
        assert f"Operation '{UNINSTALL}' timed out after 60.0s" in _text(excinfo)

    async def test_no_answer_says_the_removal_may_still_complete(
        self, hass: HomeAssistant, tools: HacsTools
    ) -> None:
        """The store finishes an uninstall it was slow to answer; a plain
        failure would invite a retry of a destructive call."""
        before = timers(hass)
        async with held_command(hass, UNINSTALL):
            call = asyncio.create_task(
                tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)
            )
            await outwait(hass, 59, 60, before)
            await asyncio.wait([call])
        with pytest.raises(ToolError) as excinfo:
            call.result()
        assert "may still have completed" in _text(excinfo)


@pytest.mark.parametrize(
    ("arguments", "foreign"),
    [
        # "Uninstall this version" is not something remove can do.
        ({"action": "remove", "repository_id": "1", "version": "v4.0.0"}, "version"),
        ({"action": "download", "repository_id": "1", "category": "theme"}, "category"),
        (
            {
                "action": "add_repository",
                "repository": "o/r",
                "category": "theme",
                "repository_id": "1",
            },
            "repository_id",
        ),
        (
            {
                "action": "update_information",
                "repository_id": "1",
                "version": "v1.0.0",
            },
            "version",
        ),
    ],
)
async def test_a_parameter_of_another_action_is_refused_not_dropped(
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    arguments: dict[str, Any],
    foreign: str,
) -> None:
    with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
        await tools.ha_manage_hacs(**arguments)
    text = _text(excinfo)
    assert "VALIDATION_INVALID_PARAMETER" in text
    assert f"'{foreign}'" in text
    assert "do not apply" in text
    spy.assert_not_awaited()


class TestUpdateInformation:
    async def test_a_numeric_id_refreshes_without_a_lookup(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(
                action="update_information", repository_id=CARD_ID
            )

        assert _store_commands(spy) == [INFO, REFRESH]
        [sent] = sent_with(spy, REFRESH)
        # Refresh takes repository, like uninstall, not info's repository_id.
        assert sent["repository"] == CARD_ID
        # The store re-fetches from GitHub first: the call waits longer.
        assert sent["_wait_timeout"] > DEFAULT_COMMAND_WAIT_TIMEOUT
        assert "Refreshed repository information" in result["data"]["message"]

    async def test_an_owner_repo_path_refreshes_the_listed_repository(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            await tools.ha_manage_hacs(action="update_information", repository_id=CARD)
        assert [s["repository"] for s in sent_with(spy, REFRESH)] == [CARD_ID]

    async def test_a_missing_id_sends_nothing(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="update_information")
        assert "repository_id" in _text(excinfo)
        spy.assert_not_awaited()

    async def test_a_failed_refresh_keeps_the_stores_reason(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """GitHub is unreachable for this repository, and Core refuses the refresh."""
        with pytest.raises(HomeAssistantCommandError) as refused:
            await socket.send_command(REFRESH, repository=STORE_ONLY_ID)
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="update_information", repository_id=STORE_ONLY_ID
            )
        text = _text(excinfo)
        assert str(refused.value) in text
        assert REFRESH in text

    async def test_no_answer_reports_the_refreshs_own_budget(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        before = timers(hass)
        async with held_command(hass, REFRESH):
            call = asyncio.create_task(
                tools.ha_manage_hacs(action="update_information", repository_id=CARD_ID)
            )
            await outwait(hass, 59, 60, before)
            await asyncio.wait([call])
        with pytest.raises(ToolError) as excinfo:
            call.result()
        assert f"Operation '{REFRESH}' timed out after 60.0s" in _text(excinfo)
