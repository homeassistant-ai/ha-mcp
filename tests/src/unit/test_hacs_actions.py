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

import pytest
from homeassistant.components.marketplace.const import DOMAIN as MARKETPLACE
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry, MockUser
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
)

from ha_mcp.__main__ import OAuthProxyClient
from ha_mcp._vendor.fastmcp.server.auth.auth import AccessToken
from ha_mcp.client.rest_client import (
    HomeAssistantClient,
    HomeAssistantCommandError,
)
from ha_mcp.client.websocket_client import (
    DEFAULT_COMMAND_WAIT_TIMEOUT,
    HomeAssistantWebSocketClient,
)
from ha_mcp.tools.tools_hacs import HacsTools

from ._client_spies import sent_commands
from ._command_faults import received
from ._hacs_actions_core import (
    add_connected_marketplace,
    serve_card,
    serve_files,
    serve_integration,
    serve_rate_limit,
    serve_repository,
)
from ._hacs_marketplace import (
    CARD_ID,
    add_marketplace,
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
BROKEN_CARD = "someone/broken-card"
BROKEN_CARD_ID = "566000222"
STORE_ONLY_ID = "401454435"
TRACKER_ID = "512345678"
TRACKER_DOMAIN = "tracker_card_sync"
YOUTUBE = "someone/youtube"
YOUTUBE_ID = "555000111"
REPOSITORIES = {
    CARD_ID: stored(CARD, "plugin", installed="v4.0.0", new=True),
    BROKEN_CARD_ID: stored(BROKEN_CARD, "plugin"),
    STORE_ONLY_ID: stored("hif2k1/battery_sim", "integration", new=True),
    TRACKER_ID: stored(
        "someone/tracker", "integration", installed="v1.0.0", domain=TRACKER_DOMAIN
    ),
    YOUTUBE_ID: stored(YOUTUBE, "integration", domain="youtube"),
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
    """The Marketplace with GitHub serving the card and the youtube
    integration, and a server error for the broken card's file."""
    serve_card(aioclient_mock, CARD, int(CARD_ID))
    serve_files(aioclient_mock, CARD)
    serve_integration(aioclient_mock, YOUTUBE, int(YOUTUBE_ID), "youtube")
    serve_card(aioclient_mock, BROKEN_CARD, int(BROKEN_CARD_ID))
    serve_files(aioclient_mock, BROKEN_CARD, status=502)
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
        self,
        hass: HomeAssistant,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy, received(hass, INSTALL) as seen:
            result = await tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID
            )

        assert store_commands(spy) == [INSTALL]
        [sent] = seen[INSTALL]
        assert sent["repository"] == CARD_ID
        assert "version" not in sent
        row = await _row(socket, CARD_ID)
        assert row["installed_version"] == row["available_version"]
        assert result["data"]["version"] == "latest"

    async def test_a_version_reaches_cores_install(
        self,
        hass: HomeAssistant,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with received(hass, INSTALL) as seen:
            await tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID, version="v2.0.0"
            )
        assert [m["version"] for m in seen[INSTALL]] == ["v2.0.0"]
        assert (await _row(socket, CARD_ID))["installed_version"] == "v2.0.0"

    async def test_an_owner_repo_name_installs_the_listed_repository(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="download", repository_id=CARD)

        assert store_commands(spy) == [LIST, INSTALL]
        assert [s["repository"] for s in sent_with(spy, INSTALL)] == [CARD_ID]
        assert result["data"]["repository"] == CARD

    async def test_a_built_in_replacement_names_the_confirmation(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="download", repository_id=YOUTUBE_ID)
        )
        assert body["ha_error_code"] == "replaces_built_in"
        assert body["translation_key"] == "replaces_built_in_not_confirmed"
        assert any("confirm_replace_built_in=True" in s for s in suggestions(body))
        assert (await _row(socket, YOUTUBE_ID))["installed"] is False

    async def test_a_confirmed_replacement_reaches_the_marketplace(
        self,
        hass: HomeAssistant,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with received(hass, INSTALL) as seen:
            await tools.ha_manage_hacs(
                action="download",
                repository_id=YOUTUBE_ID,
                confirm_replace_built_in=True,
            )
        assert [m["confirm_replace_built_in"] for m in seen[INSTALL]] == [True]
        assert (await _row(socket, YOUTUBE_ID))["installed"] is True

    async def test_a_refused_install_keeps_the_stores_answer(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        """GitHub answers the card's file with a server error."""
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="download", repository_id=BROKEN_CARD_ID)
        )
        with pytest.raises(HomeAssistantCommandError) as refused:
            await socket.send_command(INSTALL, repository=BROKEN_CARD_ID)

        native = refused.value
        assert body["error"]["code"] == "SERVICE_CALL_FAILED"
        assert body["error"]["message"] == str(native)
        assert body["ha_error_code"] == native.code
        assert body["translation_key"] == native.translation_key
        assert body["translation_domain"] == MARKETPLACE
        assert body["translation_placeholders"] == native.translation_placeholders
        assert body["command"] == INSTALL
        # A store refusal points at the store, not at a service call.
        assert any("ha_get_hacs_info" in s for s in suggestions(body))

    async def test_no_answer_says_the_install_may_still_complete(
        self, hass: HomeAssistant, tools: HacsTools
    ) -> None:
        before = timers(hass)
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

    async def test_githubs_rate_limit_points_at_connecting_an_account(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        serve_rate_limit(aioclient_mock, CARD)
        entry = await _set_up(
            hass, add_marketplace(hass, aioclient_mock, hass_storage, REPOSITORIES)
        )
        try:
            body = await rejected_call_body(
                tools.ha_manage_hacs(action="update_information", repository_id=CARD_ID)
            )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert body["ha_error_code"] == "github_rate_limited"
        assert any("connect_github" in s for s in suggestions(body))

    async def test_a_version_is_selected_in_the_marketplace(
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
        assert "version='main') installs it" in result["data"]["message"]
        assert "warnings" not in result

    async def test_a_ref_core_did_not_select_is_reported(
        self,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Seen live: Core answers a ref the repository does not have with
        success and reads the repository at another. The result carries the
        ref Core reads, and says it is not the one asked for."""
        result = await tools.ha_manage_hacs(
            action="set_version", repository_id=CARD_ID, version="no-such-ref"
        )
        info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        assert info["result"]["ref"] != "no-such-ref"
        assert result["data"]["ref"] == info["result"]["ref"]
        assert "no-such-ref" in result["warnings"][0]
        # The ref Core refused is not offered as one to install.
        assert "no-such-ref" not in result["data"]["message"]

    async def test_the_default_branch_clears_the_selection_without_a_warning(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Core clears selected_tag for the default branch and reads a
        repository with releases at its newest one, so the ref it answers is
        not the branch asked for; that is not a refused selection."""
        serve_repository(
            aioclient_mock,
            CARD,
            int(CARD_ID),
            ("lovelace-mushroom.js",),
            releases=("v1",),
        )
        entry = await _set_up(
            hass,
            add_marketplace(
                hass,
                aioclient_mock,
                hass_storage,
                {CARD_ID: stored(CARD, "plugin", selected_tag="v1")},
            ),
        )
        try:
            await socket.send_command("marketplace/warning/accept")
            # Core keeps no default branch in storage; a read from GitHub sets it.
            await socket.send_command(REFRESH, repository=CARD_ID)
            result = await tools.ha_manage_hacs(
                action="set_version", repository_id=CARD_ID, version="main"
            )
            info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert info["result"]["default_branch"] == "main"
        assert info["result"]["selected_tag"] is None
        assert result["data"]["selected_tag"] is None
        assert "warnings" not in result

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

    @pytest.mark.parametrize(
        ("arguments", "written"),
        [
            ({"action": "set_version", "version": "main"}, ("selected_tag", "main")),
            ({"action": "set_beta", "show_beta": True}, ("beta", True)),
        ],
        ids=["set_version", "set_beta"],
    )
    async def test_a_readback_without_an_answer_keeps_the_accepted_write(
        self,
        hass: HomeAssistant,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        arguments: dict[str, Any],
        written: tuple[str, Any],
    ) -> None:
        """Core answers the write with no body, and its repository read can
        reach GitHub; a read that gets no answer after the write was accepted
        must not report the write as failed."""
        before = timers(hass)
        async with held_command(hass, REPO_INFO) as reading:
            call = asyncio.create_task(
                tools.ha_manage_hacs(repository_id=CARD_ID, **arguments)
            )
            await reading.reached_by(call)
            await outwait(hass, 59, 60, before)
            result = await call
        info = await socket.send_command(REPO_INFO, repository_id=CARD_ID)

        field, value = written
        assert result["success"] is True
        assert info["result"][field] == value
        assert any(f"repository_id='{CARD_ID}'" in w for w in result["warnings"])

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
        result = await tools.ha_manage_hacs(action="clear_new", categories=["lovelace"])
        assert (await _row(socket, CARD_ID))["new"] is False
        assert (await _row(socket, STORE_ONLY_ID))["new"] is True
        assert "warnings" not in result

    async def test_a_category_the_store_lacks_is_named_not_claimed_cleared(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Seen live: Core clears nothing in a category it does not have and
        answers success; the tool said it cleared every repository there."""
        result = await tools.ha_manage_hacs(
            action="clear_new", categories=["appdaemon"]
        )

        info = await socket.send_command("marketplace/info")
        [warning] = result["warnings"]
        assert "appdaemon" in warning
        assert all(c in warning for c in info["result"]["categories"])
        assert "appdaemon" not in result["data"]["message"]
        assert (await _row(socket, CARD_ID))["new"] is True

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
        accepted; GitHub answers for a card, for a card renamed from
        owner/old-card, and for two repositories whose category cannot be
        told: one holds a card and an integration, one nothing."""
        serve_card(aioclient_mock, "owner/my-card", 777)
        serve_repository(
            aioclient_mock,
            "owner/new-card",
            780,
            ("new-card.js",),
            renamed_from="owner/old-card",
        )
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

    async def test_a_repository_listed_under_its_new_name_is_pointed_at_search(
        self, connected: MockConfigEntry, tools: HacsTools
    ) -> None:
        """GitHub answers a renamed repository under its new name, which Core
        lists; the old one the caller gave finds no row."""
        result = await tools.ha_manage_hacs(
            action="add_repository", repository="owner/old-card", category="plugin"
        )

        assert result["data"]["repository_id"] is None
        assert any("another name" in w for w in result["warnings"])

    async def test_a_list_read_without_an_answer_keeps_the_added_repository(
        self,
        hass: HomeAssistant,
        connected: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Core added the repository before the list read that finds its id;
        that read getting no answer must not report the add as failed."""
        before = timers(hass)
        async with held_command(hass, LIST, first_only=True) as listing:
            call = asyncio.create_task(
                tools.ha_manage_hacs(
                    action="add_repository",
                    repository="owner/my-card",
                    category="plugin",
                )
            )
            await listing.reached_by(call)
            await outwait(
                hass,
                DEFAULT_COMMAND_WAIT_TIMEOUT - 1,
                DEFAULT_COMMAND_WAIT_TIMEOUT,
                before,
            )
            result = await call

        assert result["data"]["repository_id"] is None
        assert any("search" in w for w in result["warnings"])
        assert "777" in {r["id"] for r in (await socket.send_command(LIST))["result"]}

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

    async def test_a_github_that_does_not_answer_points_at_system_health(
        self, connected: MockConfigEntry, tools: HacsTools
    ) -> None:
        """GitHub answers nothing for a repository it was not told to serve."""
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


class TestAcknowledgeCritical:
    @pytest.fixture
    async def critical(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
    ) -> AsyncIterator[MockConfigEntry]:
        """The Marketplace with one critical removal stored, not acknowledged."""
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
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_acknowledging_a_critical_removal_returns_cores_list(
        self,
        critical: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_manage_hacs(
            action="acknowledge_critical", repository="bad/actor"
        )
        native = await socket.send_command("marketplace/critical/list")

        assert result["data"]["critical"] == native["result"]
        assert native["result"][0]["acknowledged"] is True

    @pytest.mark.parametrize(
        "repository", ["nobody/never-critical", "Bad/Actor"], ids=["unknown", "case"]
    )
    async def test_a_name_core_holds_no_critical_row_for_is_not_acknowledged(
        self,
        critical: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        repository: str,
    ) -> None:
        """Seen live: Core matches the name exactly and answers its whole list
        either way, and the tool said it acknowledged the removal."""
        body = await rejected_call_body(
            tools.ha_manage_hacs(action="acknowledge_critical", repository=repository)
        )
        native = await socket.send_command("marketplace/critical/list")

        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any("action='critical'" in s for s in suggestions(body))
        assert native["result"][0]["acknowledged"] is False
