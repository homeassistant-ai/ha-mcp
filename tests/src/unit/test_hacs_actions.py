"""``ha_manage_hacs`` on Core's Marketplace: what each write action sends, what
it returns, and how a refusal, an error or a missing answer reaches the caller.

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
from homeassistant.components.marketplace.exceptions import (
    MarketplaceError,
    ReplacesBuiltInNotConfirmedError,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockUser
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp._vendor.fastmcp.server.auth.auth import AccessToken
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantConnectionError,
)
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.tools.tools_hacs import HacsTools

from ._backup_snapshot_core import Heartbeats
from ._client_spies import sent_commands
from ._command_faults import arrival, received
from ._hacs_actions_core import (
    add_connected_marketplace,
    serve_card,
    serve_files,
    serve_repository,
)
from ._hacs_marketplace import (
    CARD_ID,
    add_marketplace,
    core_install,
    install_progress_events,
    pooled_socket,
    sent_with,
    serve_removed,
    store_commands,
    store_critical,
    stored,
    suggestions,
)
from ._held_command import held_command
from ._oauth_server_core import signed_in
from ._tool_errors import rejected_call_body
from ._tools_addons_core import outwait, timers

CARD = "piitaya/lovelace-mushroom"
STORE_ONLY_ID = "401454435"
TRACKER_ID = "512345678"
TRACKER_DOMAIN = "tracker_card_sync"
YOUTUBE_ID = "555000111"
REPOSITORIES = {
    CARD_ID: stored(CARD, "plugin", installed="v4.0.0", new=True),
    STORE_ONLY_ID: stored("hif2k1/battery_sim", "integration", new=True),
    TRACKER_ID: stored(
        "someone/tracker", "integration", installed="v1.0.0", domain=TRACKER_DOMAIN
    ),
    YOUTUBE_ID: stored("someone/youtube", "integration", domain="youtube"),
}
LIST = "marketplace/repositories/list"
REPO_INFO = "marketplace/repository/info"
INSTALL = "marketplace/repository/install"
UNINSTALL = "marketplace/repository/uninstall"
REFRESH = "marketplace/repository/refresh"
ADD = "marketplace/repositories/add"
DETECT = "marketplace/repositories/detect"


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


async def _row(socket: HomeAssistantWebSocketClient, repository_id: str) -> Any:
    listed = await socket.send_command(LIST)
    return next(r for r in listed["result"] if r["id"] == repository_id)


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


class TestAcceptWarning:
    """Core keeps the first-run warning per user; the server's user accepts it
    once the person confirms they read it (#2970, #2971)."""

    async def test_a_confirmed_accept_reaches_the_marketplace(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="accept_warning", confirm=True)
        assert store_commands(spy) == ["marketplace/warning/accept"]
        assert result["data"]["warning_accepted"] is True
        info = await socket.send_command("marketplace/info")
        assert info["result"]["warning_accepted"] is True

    @pytest.mark.parametrize("confirm", [None, False])
    async def test_an_unconfirmed_accept_asks_for_the_users_word(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        confirm: bool | None,
    ) -> None:
        with sent_commands(socket) as spy:
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="accept_warning", confirm=confirm)
            )
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "confirm=True" in body["error"]["message"]
        spy.assert_not_awaited()

    async def test_an_install_before_it_names_the_accept_step(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="download", repository_id=CARD_ID)
        )
        assert body["ha_error_code"] == "warning_not_accepted"
        hints = " ".join(suggestions(body))
        # The server's own user (Supervisor for the app) is not the one who
        # accepted in the UI and may be one nobody can log in as (#2971).
        assert "action='accept_warning', confirm=True" in hints


@pytest.mark.usefixtures("accepted")
class TestDownload:
    async def test_no_version_installs_the_latest(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, core_install() as install:
            result = await tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID
            )

        assert store_commands(spy) == [INSTALL]
        assert [s["repository"] for s in sent_with(spy, INSTALL)] == [CARD_ID]
        assert install.await_args is not None
        assert install.await_args.kwargs["ref"] is None
        assert result["data"]["version"] == "latest"

    async def test_a_version_reaches_cores_install(self, tools: HacsTools) -> None:
        with core_install() as install:
            await tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID, version="v2.0.0"
            )
        assert install.await_args is not None
        assert install.await_args.kwargs["ref"] == "v2.0.0"

    async def test_an_owner_repo_name_installs_the_listed_repository(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, core_install():
            result = await tools.ha_manage_hacs(action="download", repository_id=CARD)

        assert store_commands(spy) == [LIST, INSTALL]
        assert [s["repository"] for s in sent_with(spy, INSTALL)] == [CARD_ID]
        assert result["data"]["repository"] == CARD

    async def test_a_built_in_replacement_names_the_confirmation(
        self, tools: HacsTools
    ) -> None:
        refusal = ReplacesBuiltInNotConfirmedError("someone/youtube", "youtube")
        with core_install(side_effect=refusal):
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="download", repository_id=YOUTUBE_ID)
            )
        assert body["ha_error_code"] == "replaces_built_in"
        assert body["translation_key"] == "replaces_built_in_not_confirmed"
        assert any("confirm_replace_built_in=True" in s for s in suggestions(body))

    async def test_a_confirmed_replacement_reaches_the_marketplace(
        self, tools: HacsTools
    ) -> None:
        with core_install() as install:
            await tools.ha_manage_hacs(
                action="download",
                repository_id=YOUTUBE_ID,
                confirm_replace_built_in=True,
            )
        assert install.await_args is not None
        assert install.await_args.kwargs["confirm_replace_built_in"] is True

    async def test_a_refused_install_keeps_the_stores_answer(
        self, tools: HacsTools
    ) -> None:
        failure = MarketplaceError(
            translation_domain=MARKETPLACE,
            translation_key="github_failed",
            translation_placeholders={"error": "502 Bad Gateway"},
        )
        with core_install(side_effect=failure):
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="download", repository_id=CARD_ID)
            )
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert "502 Bad Gateway" in body["error"]["message"]
        assert body["ha_error_code"] == "error"
        assert body["translation_key"] == "github_failed"
        assert body["translation_domain"] == MARKETPLACE
        assert body["translation_placeholders"]["error"] == "502 Bad Gateway"
        assert body["command"] == INSTALL
        # A store refusal points at the store, not at a service call.
        assert any("ha_get_hacs_info" in s for s in suggestions(body))

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
        body = await rejected_call_body(call)
        assert body["timeout_seconds"] == 60.0
        assert any("may still have completed" in s for s in suggestions(body))


@pytest.mark.usefixtures("accepted")
class TestRemove:
    async def test_an_installed_repository_is_uninstalled_in_one_command(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)

        assert store_commands(spy) == [UNINSTALL]
        assert (await _row(socket, CARD_ID))["installed"] is False
        # Deleting the files does not unload a loaded integration.
        assert "restart" in result["data"]["note"]

    async def test_a_repository_never_installed_is_cores_refusal(
        self, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="remove", repository_id=STORE_ONLY_ID)
        )
        assert body["ha_error_code"] == "repository_not_installed"
        assert any("installed_only=True" in s for s in suggestions(body))

    async def test_an_integration_still_set_up_names_its_entries(
        self, hass: HomeAssistant, tools: HacsTools
    ) -> None:
        MockConfigEntry(domain=TRACKER_DOMAIN).add_to_hass(hass)
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="remove", repository_id=TRACKER_ID)
        )
        assert body["ha_error_code"] == "repository_in_use"
        assert any("ha_get_integration" in s for s in suggestions(body))

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
        body = await rejected_call_body(call)
        assert body["timeout_seconds"] == 60.0
        assert any("may still have completed" in s for s in suggestions(body))


class TestRepositorySettings:
    async def test_update_information_refreshes_with_githubs_budget(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(
                action="update_information", repository_id=CARD_ID
            )
        [sent] = sent_with(spy, REFRESH)
        assert sent["repository"] == CARD_ID
        assert sent["_wait_timeout"] > DEFAULT_COMMAND_WAIT_TIMEOUT
        assert result["data"]["repository_id"] == CARD_ID

    async def test_an_unanswered_refresh_reports_its_own_budget(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        before = timers(hass)
        async with held_command(hass, REFRESH):
            call = asyncio.create_task(
                tools.ha_manage_hacs(action="update_information", repository_id=CARD_ID)
            )
            await outwait(hass, 59, 60, before)
            await asyncio.wait([call])
        body = await rejected_call_body(call)
        assert body["timeout_seconds"] == 60.0

    async def test_a_version_is_pinned_in_the_marketplace(
        self,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(
                action="set_version", repository_id=CARD, version="main"
            )
        [sent] = sent_with(spy, "marketplace/repository/version")
        assert (sent["repository"], sent["version"]) == (CARD_ID, "main")
        info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        assert info["result"]["selected_tag"] == "main"
        assert result["data"]["selected_tag"] == "main"
        assert "warnings" not in result

    async def test_a_pin_the_marketplace_does_not_install_from_is_reported(
        self,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Seen live: Core answers a pin to a ref the repository does not have
        with success and goes on installing from another version. The result
        carries the version Core installs from, and says the pin is not it."""
        result = await tools.ha_manage_hacs(
            action="set_version", repository_id=CARD_ID, version="no-such-ref"
        )
        info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        assert info["result"]["ref"] != "no-such-ref"
        assert result["data"]["ref"] == info["result"]["ref"]
        assert "no-such-ref" in result["warnings"][0]

    async def test_beta_versions_can_be_shown(
        self,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_manage_hacs(
            action="set_beta", repository_id=CARD_ID, show_beta=True
        )
        info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        assert info["result"]["beta"] is True
        assert result["data"]["beta"] is True

    async def test_set_beta_needs_show_beta(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="set_beta", repository_id=CARD_ID)
        )
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["parameter"] == "show_beta"

    async def test_clear_new_clears_one_repository(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        assert (await _row(socket, STORE_ONLY_ID))["new"] is True
        await tools.ha_manage_hacs(action="clear_new", repository_id=STORE_ONLY_ID)
        assert (await _row(socket, STORE_ONLY_ID))["new"] is False
        assert (await _row(socket, CARD_ID))["new"] is True

    async def test_clear_new_clears_whole_categories(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        await tools.ha_manage_hacs(action="clear_new", categories=["lovelace"])
        assert (await _row(socket, CARD_ID))["new"] is False
        assert (await _row(socket, STORE_ONLY_ID))["new"] is True

    async def test_clear_new_takes_one_target(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            body = await rejected_call_body(tools.ha_manage_hacs(action="clear_new"))
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        spy.assert_not_awaited()

    async def test_ignore_reaches_the_marketplace(
        self,
        hass: HomeAssistant,
        marketplace: MockConfigEntry,
        tools: HacsTools,
    ) -> None:
        with received(hass, "marketplace/repository/ignore") as seen:
            result = await tools.ha_manage_hacs(action="ignore", repository_id=CARD)
        assert [m["repository"] for m in seen["marketplace/repository/ignore"]] == [
            CARD_ID
        ]
        assert result["data"]["repository"] == CARD

    async def test_a_custom_repository_not_installed_can_be_forgotten(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        await tools.ha_manage_hacs(
            action="remove_repository", repository_id=STORE_ONLY_ID
        )
        listed = await socket.send_command(LIST)
        assert STORE_ONLY_ID not in {r["id"] for r in listed["result"]}

    async def test_an_installed_repository_is_not_forgotten(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="remove_repository", repository_id=CARD_ID)
        )
        assert body["ha_error_code"] == "repository_installed"
        assert any("action='remove'" in s for s in suggestions(body))


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
        accepted; GitHub answers for a card and for two repositories whose
        category cannot be told: one holds a card and an integration, one
        nothing."""
        serve_card(aioclient_mock, "owner/my-card", 777)
        serve_repository(
            aioclient_mock,
            "owner/several",
            778,
            ("several.js", "custom_components/several/manifest.json"),
        )
        serve_repository(aioclient_mock, "owner/nothing", 779, ("README.md",))
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

    async def test_without_a_category_the_detected_one_is_used(
        self,
        hass: HomeAssistant,
        connected: MockConfigEntry,
        tools: HacsTools,
    ) -> None:
        with received(hass, DETECT, ADD) as seen:
            result = await tools.ha_manage_hacs(
                action="add_repository",
                repository="https://github.com/owner/my-card",
            )
        assert len(seen[DETECT]) == 1
        assert [m["category"] for m in seen[ADD]] == ["plugin"]
        assert result["data"]["repository_id"] == "777"

    @pytest.mark.parametrize(
        "repository", ["owner/several", "owner/nothing"], ids=["several", "none"]
    )
    async def test_an_undecided_category_is_asked_for(
        self,
        hass: HomeAssistant,
        connected: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        repository: str,
    ) -> None:
        """Core detects several categories or none; the panel's dialog then
        asks which one, and so does the tool, before anything is added."""
        detected = await socket.send_command(DETECT, repository=repository)
        with received(hass, ADD) as seen:
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="add_repository", repository=repository)
            )
        assert len(detected["result"]["categories"]) != 1
        assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["parameter"] == "category"
        assert body["detected"] == detected["result"]["categories"]
        assert seen[ADD] == []

    async def test_a_listed_repository_points_at_search(
        self, connected: MockConfigEntry, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(
                action="add_repository", repository=CARD, category="plugin"
            )
        )
        assert body["ha_error_code"] == "repository_exists"
        assert any("action='search'" in s for s in suggestions(body))

    @pytest.mark.parametrize(
        ("arguments", "translation_key", "hint"),
        [
            (
                {"repository": "noslash", "category": "plugin"},
                "invalid_repository",
                "owner/repo",
            ),
            (
                {"repository": "owner/my-card", "category": "appdaemon"},
                "invalid_category",
                "action='detect'",
            ),
        ],
        ids=["repository", "category"],
    )
    async def test_a_refused_input_says_what_core_takes(
        self,
        connected: MockConfigEntry,
        tools: HacsTools,
        arguments: dict[str, str],
        translation_key: str,
        hint: str,
    ) -> None:
        """Seen live: Core gives both refusals one code, and the suggestion
        pointed at a repository's info, which neither has."""
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="add_repository", **arguments)
        )
        assert body["translation_key"] == translation_key
        assert any(hint in s for s in suggestions(body))

    async def test_an_app_repository_points_at_the_app_tool(
        self, connected: MockConfigEntry, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(
                action="add_repository",
                repository="hassio-addons/repository",
                category="integration",
            )
        )
        assert body["ha_error_code"] == "app_repository"
        assert any("ha_manage_app" in s for s in suggestions(body))

    async def test_a_repository_github_does_not_have_points_at_system_health(
        self,
        connected: MockConfigEntry,
        aioclient_mock: AiohttpClientMocker,
        tools: HacsTools,
    ) -> None:
        aioclient_mock.get(
            "https://api.github.com/repos/owner/gone",
            status=404,
            json={"message": "Not Found"},
        )
        body = await rejected_call_body(
            tools.ha_manage_hacs(
                action="add_repository", repository="owner/gone", category="plugin"
            )
        )
        assert body["translation_key"] == "github_failed"
        assert any("ha_get_system_health" in s for s in suggestions(body))

    async def test_a_github_account_is_connected_through_the_marketplace(
        self, accepted: None, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(
                action="add_repository", repository="o/r", category="integration"
            )
        )
        assert body["ha_error_code"] == "github_not_connected"
        assert any("connect_github" in s for s in suggestions(body))


OTHER_CARD = "someone/other-card"
OTHER_CARD_ID = "600700800"


def _progress_of(events: list[dict[str, Any]], full_name: str) -> list[int]:
    """The numbers Core dispatched for ``full_name``; False ends an install."""
    return [
        e["progress"]
        for e in events
        if e["repository"] == full_name and e["progress"] is not False
    ]


class TestInstallProgress:
    """Core's install progress, forwarded as MCP progress: the numbers the
    panel's install dialog shows while a repository installs."""

    @staticmethod
    async def _installable(
        hass: HomeAssistant,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        socket: HomeAssistantWebSocketClient,
        **other_card_files: Any,
    ) -> MockConfigEntry:
        """The Marketplace, GitHub serving the card and another card in full."""
        serve_card(aioclient_mock, CARD, int(CARD_ID))
        serve_files(aioclient_mock, CARD)
        serve_card(aioclient_mock, OTHER_CARD, int(OTHER_CARD_ID))
        serve_files(aioclient_mock, OTHER_CARD, **other_card_files)
        repositories = {**REPOSITORIES, OTHER_CARD_ID: stored(OTHER_CARD, "plugin")}
        entry = add_marketplace(hass, aioclient_mock, hass_storage, repositories)
        await _set_up(hass, entry)
        await socket.send_command("marketplace/warning/accept")
        return entry

    async def test_cores_progress_of_the_install_reaches_the_caller(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        entry = await self._installable(hass, aioclient_mock, hass_storage, socket)
        heartbeats = Heartbeats()
        try:
            async with install_progress_events(socket) as dispatched:
                await tools.ha_manage_hacs(
                    action="download", repository_id=CARD_ID, ctx=heartbeats.context
                )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert _progress_of(dispatched, CARD)
        assert [p for p, _ in heartbeats.reports] == _progress_of(dispatched, CARD)

    async def test_another_repositorys_progress_is_not_forwarded(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Core installs one repository at a time, and one already installing
        when the call starts reports on the same signal while the call's own
        install waits for it."""
        downloading, release = asyncio.Event(), asyncio.Event()

        async def held_file(
            method: str, url: Any, data: Any
        ) -> AiohttpClientMockResponse:
            downloading.set()
            await release.wait()
            return AiohttpClientMockResponse(method, url, text="other card")

        entry = await self._installable(
            hass, aioclient_mock, hass_storage, socket, side_effect=held_file
        )
        heartbeats = Heartbeats()
        try:
            async with install_progress_events(socket) as dispatched:
                other = asyncio.create_task(
                    socket.send_command(INSTALL, repository=OTHER_CARD_ID)
                )
                await downloading.wait()
                with arrival(hass, INSTALL) as arrived:
                    call = asyncio.create_task(
                        tools.ha_manage_hacs(
                            action="download",
                            repository_id=CARD_ID,
                            ctx=heartbeats.context,
                        )
                    )
                    waiting = asyncio.create_task(arrived.wait())
                    await asyncio.wait(
                        {waiting, call}, return_when=asyncio.FIRST_COMPLETED
                    )
                    waiting.cancel()
                release.set()
                await asyncio.gather(call, other)
        finally:
            release.set()
            await hass.config_entries.async_unload(entry.entry_id)

        assert _progress_of(dispatched, OTHER_CARD)
        assert [p for p, _ in heartbeats.reports] == _progress_of(dispatched, CARD)

    async def test_an_install_goes_ahead_when_its_progress_cannot_be_followed(
        self,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
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


async def test_acknowledging_a_critical_removal_returns_cores_list(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    serve_removed(aioclient_mock, [])
    store_critical(
        hass_storage,
        [
            {
                "repository": "bad/actor",
                "reason": "Steals tokens",
                "link": "https://example.invalid/bad",
                "acknowledged": False,
            }
        ],
    )
    entry = await _set_up(
        hass, add_marketplace(hass, aioclient_mock, hass_storage, REPOSITORIES)
    )
    try:
        result = await tools.ha_manage_hacs(
            action="acknowledge_critical", repository="bad/actor"
        )
        native = await socket.send_command("marketplace/critical/list")
    finally:
        await hass.config_entries.async_unload(entry.entry_id)

    assert result["data"]["critical"] == native["result"]
    assert native["result"][0]["acknowledged"] is True


@pytest.mark.parametrize(
    ("arguments", "foreign"),
    [
        # "Uninstall this version" is not something remove can do.
        ({"action": "remove", "repository_id": "1", "version": "v4.0.0"}, "version"),
        ({"action": "download", "repository_id": "1", "category": "theme"}, "category"),
        (
            {"action": "add_repository", "repository": "o/r", "repository_id": "1"},
            "repository_id",
        ),
        (
            {
                "action": "set_beta",
                "repository_id": "1",
                "show_beta": True,
                "confirm": True,
            },
            "confirm",
        ),
        ({"action": "connect_github", "repository": "o/r"}, "repository"),
        ({"action": "remove", "repository_id": "1", "abort": True}, "abort"),
    ],
)
async def test_a_parameter_of_another_action_is_refused_not_dropped(
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
    arguments: dict[str, Any],
    foreign: str,
) -> None:
    with sent_commands(socket) as spy:
        body = await rejected_call_body(tools.ha_manage_hacs(**arguments))
    assert body["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
    assert body["foreign_parameters"] == [foreign]
    spy.assert_not_awaited()
