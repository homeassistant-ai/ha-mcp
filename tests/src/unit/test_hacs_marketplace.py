"""``ha_get_hacs_info`` on Core's Marketplace: what each read action sends,
what it returns, and how the store's refusals reach the caller.

The Marketplace is Core's own, set up on a real ``hass`` (``_hacs_marketplace``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.marketplace.enums import DisabledReason, MarketplaceStage
from homeassistant.core import HomeAssistant
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.test_util.aiohttp import AiohttpClientMocker

from ha_mcp._vendor.fastmcp.exceptions import ToolError
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
from ._hacs_actions_core import add_connected_marketplace, serve_card
from ._hacs_marketplace import (
    CARD_ID,
    MAIN,
    MAIN_ID,
    MIRROR_ID,
    add_marketplace,
    pooled_socket,
    sent_with,
    serve_removed,
    set_up_marketplace,
    store_commands,
    store_critical,
    stored,
    suggestions,
)
from ._held_command import held_command
from ._tool_errors import error_body, rejected_call_body
from ._tools_addons_core import outwait, timers

CARD = "piitaya/lovelace-mushroom"
YOUTUBE_ID = "555000111"
REPOSITORIES = {
    CARD_ID: stored(CARD, "plugin", installed="v4.0.0"),
    MAIN_ID: stored(MAIN, "integration"),
    MIRROR_ID: stored(
        "homeassistant-ai/ha-mcp-integration",
        "integration",
        installed="v9.1.0",
        domain="ha_mcp_tools",
    ),
    YOUTUBE_ID: stored("someone/youtube", "integration", domain="youtube"),
}
INFO = "marketplace/info"
LIST = "marketplace/repositories/list"
REPO_INFO = "marketplace/repository/info"
RELEASES = "marketplace/repository/releases"


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


async def _rows(socket: HomeAssistantWebSocketClient) -> list[dict[str, Any]]:
    """The store's own list, as Core sends it."""
    listed = await socket.send_command(LIST)
    rows: list[dict[str, Any]] = listed["result"]
    return rows


class TestWithoutTheMarketplace:
    async def test_a_core_without_the_marketplace_is_a_missing_component(
        self, tools: HacsTools
    ) -> None:
        """Core older than 2026.11 has no ``marketplace/*`` command; nothing
        falls back to the HACS integration."""
        body = await rejected_call_body(tools.ha_get_hacs_info(action="search"))
        assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert body["ha_error_code"] == "unknown_command"
        assert any("2026.11" in s for s in suggestions(body))

    async def test_an_unloaded_marketplace_keeps_cores_answer(
        self,
        hass: HomeAssistant,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        assert await hass.config_entries.async_unload(marketplace.entry_id)
        body = await rejected_call_body(tools.ha_get_hacs_info(action="search"))
        assert body["error"]["code"] == "COMPONENT_NOT_INSTALLED"
        assert body["ha_error_code"] == "not_loaded"
        assert body["translation_key"] == "not_loaded"
        with pytest.raises(HomeAssistantCommandError) as refused:
            await socket.send_command(INFO)
        assert body["error"]["message"] == str(refused.value)


class TestSearch:
    async def test_rows_and_store_status_are_the_stores_own(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_get_hacs_info(action="search", max_results=100)

        rows = await _rows(socket)
        assert sorted(result["data"]["results"], key=lambda r: r["id"]) == sorted(
            rows, key=lambda r: r["id"]
        )
        info = await socket.send_command(INFO)
        assert result["data"]["store"] == info["result"]

    async def test_rows_come_in_the_panels_order(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        """Installed first, then new, then the most starred, then by name."""
        entry = await set_up_marketplace(
            hass,
            aioclient_mock,
            hass_storage,
            {
                "1": stored("o/starred-b", "integration", stargazers_count=50),
                "2": stored("o/starred-a", "integration", stargazers_count=50),
                "3": stored("o/fresh", "integration", new=True),
                "4": stored("o/kept", "plugin", installed="v1.0.0"),
                "5": stored("o/plain", "integration", stargazers_count=3),
            },
        )
        try:
            result = await tools.ha_get_hacs_info(action="search")
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert [r["full_name"] for r in result["data"]["results"]] == [
            "o/kept",
            "o/fresh",
            "o/starred-a",
            "o/starred-b",
            "o/plain",
        ]

    async def test_every_word_of_the_query_must_match_somewhere_in_the_row(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
    ) -> None:
        entry = await set_up_marketplace(
            hass,
            aioclient_mock,
            hass_storage,
            {
                "1": stored(
                    "o/a", "plugin", description="Mushroom tiles", topics=["card"]
                ),
                "2": stored("o/b", "plugin", description="Mushroom tiles"),
                "3": stored("o/c", "plugin", authors=["@MushroomFan"], topics=["Card"]),
            },
        )
        try:
            result = await tools.ha_get_hacs_info(
                action="search", query="CARD mushroom"
            )
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        assert sorted(r["full_name"] for r in result["data"]["results"]) == [
            "o/a",
            "o/c",
        ]

    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            # Installed takes in what waits for an update or a restart.
            (["installed"], {CARD, "homeassistant-ai/ha-mcp-integration"}),
            ("new", {"o/fresh"}),
        ],
    )
    async def test_the_status_filter_follows_the_panel(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        status: list[str] | str,
        expected: set[str],
    ) -> None:
        entry = await set_up_marketplace(
            hass,
            aioclient_mock,
            hass_storage,
            {**REPOSITORIES, "9": stored("o/fresh", "integration", new=True)},
        )
        try:
            statuses = {r["full_name"]: r["status"] for r in await _rows(socket)}
            result = await tools.ha_get_hacs_info(action="search", status=status)
        finally:
            await hass.config_entries.async_unload(entry.entry_id)

        # The mirror waits for an update: its own status is not "installed".
        assert statuses["homeassistant-ai/ha-mcp-integration"] == "pending-upgrade"
        assert {r["full_name"] for r in result["data"]["results"]} == expected

    async def test_installed_only_leaves_out_what_is_not_installed(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        rows = await _rows(socket)
        installed = {r["full_name"] for r in rows if r["installed"]}
        assert installed
        assert len(installed) < len(rows)

        result = await tools.ha_get_hacs_info(
            action="search", installed_only=True, max_results=100
        )

        assert {r["full_name"] for r in result["data"]["results"]} == installed

    async def test_a_status_no_row_carries_is_named_with_the_lists_statuses(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Seen live: an underscore for Core's hyphen matched nothing and the
        empty page said nothing about why."""
        result = await tools.ha_get_hacs_info(
            action="search", status=["pending_upgrade"]
        )

        statuses = {row["status"] for row in await _rows(socket)}
        assert "pending-upgrade" in statuses
        assert result["data"]["total_matches"] == 0
        [warning] = result["warnings"]
        assert "pending_upgrade" in warning
        assert all(status in warning for status in statuses)

    @pytest.mark.parametrize(
        ("source", "everything"), [("custom", True), ("catalog", False)]
    )
    async def test_the_source_filter_reads_the_rows_custom_flag(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        source: str,
        everything: bool,
    ) -> None:
        """Offline, no row comes from the catalog: every stored one is custom."""
        rows = await _rows(socket)
        assert all(row["custom"] for row in rows)
        result = await tools.ha_get_hacs_info(
            action="search", source=source, max_results=100
        )
        assert result["data"]["total_matches"] == (len(rows) if everything else 0)

    @pytest.mark.parametrize(
        ("category", "native"),
        [
            # Core matches the list's categories as given; a capitalised one
            # must still find the store's own.
            ("Integration", "integration"),
            # HA-MCP's older name for dashboard cards.
            ("lovelace", "plugin"),
        ],
    )
    async def test_a_category_reaches_the_store_in_its_own_name(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
        category: str,
        native: str,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_get_hacs_info(action="search", category=category)
        [sent] = sent_with(spy, LIST)
        assert sent["categories"] == [native]
        assert result["data"]["results"]
        assert {row["category"] for row in result["data"]["results"]} == {native}
        assert "warnings" not in result

    async def test_a_category_the_store_lacks_is_named_with_its_categories(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_get_hacs_info(action="search", category="appdaemon")
        assert result["data"]["results"] == []
        info = await socket.send_command(INFO)
        [warning] = result["warnings"]
        assert "appdaemon" in warning
        assert all(category in warning for category in info["result"]["categories"])

    @pytest.mark.parametrize("state", ["rate_limit", "startup"])
    async def test_a_marketplace_not_running_says_its_list_may_be_incomplete(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        state: str,
    ) -> None:
        """Disabled, or still starting, Core's list may lack catalog
        repositories: no match is not "not there", and a missing one is not
        one to add."""
        manager = marketplace.runtime_data
        if state == "rate_limit":
            manager.disable(DisabledReason.RATE_LIMIT)
        else:
            manager.set_stage(MarketplaceStage.STARTUP)

        found = await tools.ha_get_hacs_info(action="search", query="catalog-only")
        missing = await rejected_call_body(
            tools.ha_get_hacs_info(action="info", repository_id="someone/catalog-only")
        )

        assert found["data"]["total_matches"] == 0
        assert any(state in w for w in found["warnings"])
        assert missing["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert state in missing["error"]["message"]
        assert not any("add_repository" in s for s in suggestions(missing))

    async def test_a_page_says_where_the_next_one_starts(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        first = (await tools.ha_get_hacs_info(action="search", max_results=3))["data"]
        last = (await tools.ha_get_hacs_info(action="search", max_results=3, offset=3))[
            "data"
        ]

        assert (first["count"], first["has_more"], first["next_offset"]) == (3, True, 3)
        assert (last["count"], last["has_more"], last["next_offset"]) == (
            1,
            False,
            None,
        )
        assert {r["id"] for r in first["results"]}.isdisjoint(
            r["id"] for r in last["results"]
        )


class TestInfo:
    async def test_a_numeric_id_is_read_without_a_lookup(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        with sent_commands(socket) as spy:
            result = await tools.ha_get_hacs_info(action="info", repository_id=CARD_ID)

        assert store_commands(spy) == [REPO_INFO]
        assert result["data"]["full_name"] == CARD

    async def test_an_owner_repo_name_is_looked_up_in_one_list(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Core takes only the id; the name is matched case-insensitively."""
        with sent_commands(socket) as spy:
            result = await tools.ha_get_hacs_info(
                action="info", repository_id=CARD.upper()
            )

        assert store_commands(spy) == [LIST, REPO_INFO]
        assert [s["repository_id"] for s in sent_with(spy, REPO_INFO)] == [CARD_ID]
        assert result["data"]["full_name"] == CARD
        # The id the writes answer with, not the name the caller passed.
        assert result["data"]["repository_id"] == CARD_ID

    async def test_the_read_waits_githubs_budget(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        """Core reads a repository it has not read since it started from
        GitHub before it answers."""
        with sent_commands(socket) as spy:
            await tools.ha_get_hacs_info(action="info", repository_id=CARD_ID)
        [sent] = sent_with(spy, REPO_INFO)
        assert sent["_wait_timeout"] > DEFAULT_COMMAND_WAIT_TIMEOUT

    async def test_an_unknown_id_keeps_the_stores_answer_as_not_found(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        body = await rejected_call_body(
            tools.ha_get_hacs_info(action="info", repository_id="999999")
        )
        with pytest.raises(HomeAssistantCommandError) as refused:
            await socket.send_command(REPO_INFO, repository_id="999999")
        assert body["error"]["message"] == str(refused.value)
        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert body["ha_error_code"] == "repository_not_found"
        assert body["translation_placeholders"] == {"repository": "999999"}

    async def test_an_unknown_name_points_at_search(
        self, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        body = await rejected_call_body(
            tools.ha_get_hacs_info(action="info", repository_id="nobody/missing")
        )
        assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert any("action='search'" in s for s in suggestions(body))

    async def test_every_field_and_the_readme_note_reach_the_caller(
        self,
        marketplace: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_get_hacs_info(action="info", repository_id=CARD_ID)

        native = await socket.send_command(REPO_INFO, repository_id=CARD_ID)
        data = result["data"]
        assert {k: data[k] for k in native["result"]} == native["result"]
        assert "readme_note" in data

    async def test_a_failed_releases_read_leaves_the_info_and_warns(
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
        assert out["data"]["full_name"] == CARD
        assert "warnings" not in out["data"]
        assert any(w.startswith("Releases unavailable: ") for w in out["warnings"])
        # GitHub is slower than a local command: the read waits longer.
        [sent] = sent_with(spy, RELEASES)
        assert sent["_wait_timeout"] > DEFAULT_COMMAND_WAIT_TIMEOUT

    async def test_an_unanswered_releases_read_warns(
        self, hass: HomeAssistant, marketplace: MockConfigEntry, tools: HacsTools
    ) -> None:
        before = timers(hass)
        async with held_command(hass, RELEASES) as releases:
            call = asyncio.create_task(
                tools.ha_get_hacs_info(
                    action="info", repository_id=CARD_ID, include_releases=True
                )
            )
            # The info read before it waits as long; it has its answer by now.
            await releases.reached_by(call)
            await outwait(hass, 59, 60, before)
            await asyncio.wait([call])
        out = call.result()
        assert out["data"]["full_name"] == CARD
        assert "github_releases" not in out["data"]
        assert "Releases unavailable: Command timeout" in out["warnings"]

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
        notes = sent_with(spy, "update/release_notes")
        assert [n["entity_id"] for n in notes] == [entity_id]


REMOVED = [
    {"repository": "gone/card", "removal_type": "blacklist", "reason": "Malware"},
    {"repository": "gone/tiles", "removal_type": "removal"},
    {"repository": "gone/sensor", "removal_type": "archived", "link": "https://x"},
]


class TestRemovedAndCritical:
    @pytest.fixture
    async def catalog(
        self,
        hass: HomeAssistant,
        core_client: HomeAssistantClient,
        aioclient_mock: AiohttpClientMocker,
        hass_storage: dict[str, Any],
    ) -> AsyncIterator[MockConfigEntry]:
        """The catalog's removed feed answers; one critical removal is stored."""
        serve_removed(aioclient_mock, REMOVED)
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
        entry = add_marketplace(hass, aioclient_mock, hass_storage, REPOSITORIES)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry
        await hass.config_entries.async_unload(entry.entry_id)

    async def test_removed_rows_are_the_stores_filtered_and_paged(
        self,
        catalog: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        native = (await socket.send_command("marketplace/repositories/removed"))[
            "result"
        ]
        assert len(native) == len(REMOVED)

        everything = await tools.ha_get_hacs_info(action="removed", max_results=100)
        matched = await tools.ha_get_hacs_info(action="removed", query="MALWARE")
        paged = await tools.ha_get_hacs_info(action="removed", max_results=2)

        assert everything["data"]["results"] == native
        assert [r["repository"] for r in matched["data"]["results"]] == ["gone/card"]
        assert (paged["data"]["count"], paged["data"]["next_offset"]) == (2, 2)

    async def test_critical_rows_are_the_stores(
        self,
        catalog: MockConfigEntry,
        tools: HacsTools,
        socket: HomeAssistantWebSocketClient,
    ) -> None:
        result = await tools.ha_get_hacs_info(action="critical")
        native = await socket.send_command("marketplace/critical/list")
        assert result["data"]["results"] == native["result"]
        assert result["data"]["count"] == 1


async def test_detect_names_the_categories_a_repository_fits(
    hass: HomeAssistant,
    core_client: HomeAssistantClient,
    aioclient_mock: AiohttpClientMocker,
    hass_storage: dict[str, Any],
    tools: HacsTools,
    socket: HomeAssistantWebSocketClient,
) -> None:
    serve_card(aioclient_mock, "owner/my-card", 777)
    entry = add_connected_marketplace(hass, aioclient_mock, hass_storage, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        await socket.send_command("marketplace/warning/accept")
        with sent_commands(socket) as spy:
            result = await tools.ha_get_hacs_info(
                action="detect", repository="https://github.com/owner/my-card"
            )
    finally:
        await hass.config_entries.async_unload(entry.entry_id)

    assert result["data"]["categories"] == ["plugin"]
    [sent] = sent_with(spy, "marketplace/repositories/detect")
    assert sent["_wait_timeout"] > DEFAULT_COMMAND_WAIT_TIMEOUT


async def test_a_lost_connection_names_the_tool_and_action(
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
    body = error_body(excinfo)
    assert body["tool"] == "ha_get_hacs_info"
    assert body["action"] == "search"
