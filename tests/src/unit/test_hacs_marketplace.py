"""The HACS tools on Home Assistant 2026.11+, where HACS is the Marketplace.

Core 2026.11 removes the ``hacs/*`` WebSocket commands and serves
``marketplace/*`` instead; the tools must pick whichever answers and keep
the store's own data (categories, repository fields, refusals) intact.

The Marketplace is Core's own, set up on a real ``hass`` (``_hacs_marketplace``);
the HACS integration of older releases is a mock integration.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.marketplace.exceptions import (
    ReplacesBuiltInNotConfirmedError,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.client.websocket_client import HomeAssistantWebSocketClient
from ha_mcp.tools.hacs_backend import StoreApi, detect_store_api
from ha_mcp.tools.hacs_registration import wait_for_repo_registration
from ha_mcp.tools.tools_hacs import HacsTools

from ._hacs_marketplace import (
    CARD_ID,
    MAIN,
    MAIN_ID,
    MIRROR_ID,
    HacsIntegration,
    command_types,
    core_install,
    install_hacs,
    pooled_socket,
    sent_commands,
    sent_with,
    set_up_marketplace,
    stored,
)

YOUTUBE_ID = "555000111"
REPOSITORIES = {
    CARD_ID: stored("piitaya/lovelace-mushroom", "plugin", installed="v4.0.0"),
    MAIN_ID: stored(MAIN, "integration"),
    MIRROR_ID: stored(
        "homeassistant-ai/ha-mcp-integration",
        "integration",
        installed="v9.1.0",
        domain="ha_mcp_tools",
    ),
    YOUTUBE_ID: stored("someone/youtube", "integration", domain="youtube"),
}


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """The Marketplace installs into the configuration directory."""
    return hass_tmp_config_dir


@pytest.fixture
async def socket(core_client: HomeAssistantClient) -> HomeAssistantWebSocketClient:
    return await pooled_socket(core_client)


@pytest.fixture
def tools(core_client: HomeAssistantClient) -> HacsTools:
    return HacsTools(core_client)


@pytest.fixture
async def marketplace(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
) -> AsyncIterator[MockConfigEntry]:
    entry = await set_up_marketplace(hass, aioclient_mock, hass_storage, REPOSITORIES)
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
def hacs(hass: HomeAssistant, core_client: HomeAssistantClient) -> HacsIntegration:
    store = HacsIntegration(
        repositories=[
            {"id": CARD_ID, "full_name": "piitaya/lovelace-mushroom"},
        ]
    )
    install_hacs(hass, store)
    return store


@pytest.fixture
async def accepted(marketplace: MockConfigEntry, tools: HacsTools) -> None:
    """The server's user accepted the Marketplace's first-run warning."""
    await tools.ha_manage_hacs(action="accept_warning", confirm=True)


def _error_text(exc_info: pytest.ExceptionInfo[ToolError]) -> str:
    return str(exc_info.value)


class TestDetectStoreApi:
    async def test_core_2026_11_is_served_by_the_marketplace(
        self, marketplace: MockConfigEntry, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy:
            api = await detect_store_api(socket)
        assert api.is_marketplace
        assert api.info["warning_accepted"] is False
        spy.assert_awaited_once_with("marketplace/info")

    async def test_older_core_falls_back_to_the_hacs_integration(
        self, hacs: HacsIntegration, socket: HomeAssistantWebSocketClient
    ) -> None:
        api = await detect_store_api(socket)
        assert not api.is_marketplace
        assert api.info["categories"] == hacs.categories

    async def test_unloaded_marketplace_is_not_reported_as_missing_hacs(
        self,
        hass: HomeAssistant,
        marketplace: MockConfigEntry,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        assert await hass.config_entries.async_unload(marketplace.entry_id)
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await detect_store_api(socket)
        assert "COMPONENT_NOT_INSTALLED" in _error_text(excinfo)
        assert "Marketplace" in _error_text(excinfo)
        spy.assert_awaited_once()

    async def test_neither_store_names_both_options(
        self, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await detect_store_api(socket)
        assert "COMPONENT_NOT_INSTALLED" in _error_text(excinfo)
        assert "2026.11" in _error_text(excinfo)
        assert command_types(spy) == ["marketplace/info", "hacs/info"]


@pytest.mark.usefixtures("accepted")
class TestMarketplaceCommands:
    async def test_download_installs_through_the_marketplace(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, core_install() as install:
            result = await tools.ha_manage_hacs(
                action="download", repository_id=CARD_ID, version="v2.0.0"
            )
        assert result["success"] is True
        [sent] = sent_with(spy, "marketplace/repository/install")
        assert sent["repository"] == CARD_ID
        # Core read the version the tool sent.
        install.assert_awaited_once()
        assert install.await_args is not None
        assert install.await_args.kwargs["ref"] == "v2.0.0"

    async def test_remove_uninstalls_through_the_marketplace(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="remove", repository_id=CARD_ID)
        assert result["success"] is True
        assert [
            s["repository"] for s in sent_with(spy, "marketplace/repository/uninstall")
        ] == [CARD_ID]
        info = await socket.send_command(
            "marketplace/repository/info", repository_id=CARD_ID
        )
        assert info["result"]["installed"] is False

    async def test_github_gate_is_not_mistaken_for_a_connection_failure(
        self, tools: HacsTools
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="add_repository", repository="o/r", category="integration"
            )
        text = _error_text(excinfo)
        assert "github_not_connected" in text
        assert "CONNECTION_FAILED" not in text
        assert "Connect GitHub" in text

    async def test_category_is_checked_against_the_store_list(
        self, tools: HacsTools, socket: HomeAssistantWebSocketClient
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await tools.ha_get_hacs_info(action="search", category="appdaemon")
        text = _error_text(excinfo)
        assert "VALIDATION_INVALID_PARAMETER" in text
        info = await socket.send_command("marketplace/info")
        assert all(category in text for category in info["result"]["categories"])
        assert command_types(spy) == ["marketplace/info"]

    @pytest.mark.parametrize(
        ("category", "native"),
        [
            # Core lower-cases it on add; a capitalised one must not be refused.
            ("Integration", "integration"),
            # HA-MCP's older name for dashboard cards.
            ("lovelace", "plugin"),
        ],
    )
    async def test_category_reaches_the_store_in_its_own_name(
        self,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        category: str,
        native: str,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_get_hacs_info(action="search", category=category)
        [sent] = sent_with(spy, "marketplace/repositories/list")
        assert sent["categories"] == [native]
        assert {row["category"] for row in result["data"]["results"]} <= {native}

    async def test_built_in_replacement_names_the_confirmation(
        self, tools: HacsTools
    ) -> None:
        refusal = ReplacesBuiltInNotConfirmedError("someone/youtube", "youtube")
        with core_install(side_effect=refusal), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id=YOUTUBE_ID)
        text = _error_text(excinfo)
        assert "replaces_built_in" in text
        assert "confirm_replace_built_in=True" in text
        assert "Verify the target entity" not in text

    async def test_confirmed_replacement_reaches_the_marketplace(
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


async def test_unaccepted_warning_tells_the_user_where_to_accept_it(
    marketplace: MockConfigEntry, tools: HacsTools
) -> None:
    with pytest.raises(ToolError) as excinfo:
        await tools.ha_manage_hacs(action="download", repository_id=CARD_ID)
    text = _error_text(excinfo)
    assert "warning_not_accepted" in text
    assert "first-run warning" in text
    # The server's own user (Supervisor for the app) is not the one who
    # accepted in the UI and may be one nobody can log in as (#2971).
    assert "action='accept_warning'" in text
    assert "confirm=True" in text


async def test_hacs_is_not_sent_the_built_in_confirmation(
    hacs: HacsIntegration, tools: HacsTools
) -> None:
    """HACS has no such gate, and its schema would refuse the field."""
    result = await tools.ha_manage_hacs(
        action="download", repository_id=CARD_ID, confirm_replace_built_in=True
    )
    assert result["success"] is True
    assert hacs.commands()[-1] == "hacs/repository/download"
    assert "confirm_replace_built_in" not in hacs.received[-1]


class TestAcceptWarning:
    """Core 2026.11 keeps the first-run warning per user; the server's user
    accepts it once the person confirms they read it (#2970, #2971)."""

    async def test_confirmed_accept_reaches_the_marketplace(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="accept_warning", confirm=True)
        assert command_types(spy) == ["marketplace/info", "marketplace/warning/accept"]
        assert result["success"] is True
        assert result["warning_accepted"] is True
        assert (await detect_store_api(socket)).info["warning_accepted"] is True

    @pytest.mark.parametrize("confirm", [None, False])
    async def test_unconfirmed_accept_asks_for_the_users_word(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        confirm: bool | None,
    ) -> None:
        with sent_commands(socket) as spy, pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="accept_warning", confirm=confirm)
        text = _error_text(excinfo)
        assert "VALIDATION" in text
        assert "confirm=True" in text
        spy.assert_not_awaited()
        assert (await detect_store_api(socket)).info["warning_accepted"] is False

    async def test_already_accepted_sends_nothing(
        self,
        accepted: None,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_manage_hacs(action="accept_warning", confirm=True)
        assert command_types(spy) == ["marketplace/info"]
        assert result["success"] is True
        assert result["warning_accepted"] is True

    async def test_hacs_has_no_warning_to_accept(
        self, hacs: HacsIntegration, tools: HacsTools
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="accept_warning", confirm=True)
        assert "Marketplace" in _error_text(excinfo)
        assert hacs.commands() == ["hacs/info"]

    async def test_repository_parameters_do_not_apply(self, tools: HacsTools) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="accept_warning", confirm=True, repository_id="123"
            )
        assert "repository_id" in _error_text(excinfo)

    async def test_confirm_does_not_apply_to_other_actions(
        self, tools: HacsTools
    ) -> None:
        with pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="download", repository_id="123", confirm=True
            )
        assert "confirm" in _error_text(excinfo)


class TestNativeFieldsReachTheCaller:
    async def test_search_rows_and_store_status_are_the_stores_own(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_get_hacs_info(action="search", installed_only=True)

        listed = await socket.send_command("marketplace/repositories/list")
        installed = [row for row in listed["result"] if row["installed"]]
        assert installed
        assert sorted(result["data"]["results"], key=lambda r: r["id"]) == sorted(
            installed, key=lambda r: r["id"]
        )
        store = result["data"]["store"]
        assert store["api"] == "marketplace"
        info = await socket.send_command("marketplace/info")
        assert store["github_connected"] is info["result"]["github_connected"]

    async def test_info_keeps_every_field_and_the_readme_note(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_get_hacs_info(action="info", repository_id=CARD_ID)

        native = await socket.send_command(
            "marketplace/repository/info", repository_id=CARD_ID
        )
        data = result["data"]
        assert set(native["result"]) <= set(data)
        assert data["additional_info"] == native["result"]["additional_info"]
        assert "replaces_built_in" in data
        assert "readme_note" in data

    async def test_a_failed_releases_fetch_leaves_the_info_and_warns(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """The releases command reaches GitHub; its failure must not cost the
        repository info, and the warning sits beside data, not inside it."""
        with sent_commands(socket) as spy:
            out = await tools.ha_get_hacs_info(
                action="info", repository_id=CARD_ID, include_releases=True
            )
        assert out["data"]["full_name"] == "piitaya/lovelace-mushroom"
        assert "warnings" not in out["data"]
        assert any(w.startswith("Releases unavailable: ") for w in out["warnings"])
        assert "marketplace/repository/releases" in command_types(spy)

    async def test_release_notes_come_from_the_update_entity(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """An installed repository with an upgrade asks Core's update entity
        for the notes, the way the update dialog shows them."""
        with sent_commands(socket) as spy:
            out = await tools.ha_get_hacs_info(
                action="info", repository_id=MIRROR_ID, include_releases=True
            )
        entity_id = out["data"]["update_entity_id"]
        assert out["data"]["pending_upgrade"] is True
        assert entity_id is not None
        notes = [c for c in spy.call_args_list if c.args[0] == "update/release_notes"]
        assert [c.kwargs["entity_id"] for c in notes] == [entity_id]


async def test_marketplace_registration_is_one_list_lookup(
    marketplace: MockConfigEntry, socket: HomeAssistantWebSocketClient
) -> None:
    """The Marketplace registers a repository before its add command answers,
    so no subscription or wait is needed to find it."""
    with (
        sent_commands(socket) as spy,
        patch.object(
            socket, "subscribe_command", wraps=socket.subscribe_command
        ) as subscribe,
    ):
        repo = await wait_for_repo_registration(
            socket, MAIN.upper(), timeout=10.0, api=StoreApi("marketplace")
        )
    assert repo is not None
    assert repo["id"] == MAIN_ID
    assert repo["full_name"] == MAIN
    subscribe.assert_not_awaited()
    spy.assert_awaited_once_with("marketplace/repositories/list")
