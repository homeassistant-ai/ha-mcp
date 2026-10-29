"""The HACS tools on Home Assistant 2026.11+, where HACS is the Marketplace.

Core 2026.11 removes the ``hacs/*`` WebSocket commands and serves
``marketplace/*`` instead; the tools must pick whichever answers and keep
the store's own data (categories, repository fields, refusals) intact.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp.client.rest_client import (
    HomeAssistantCommandError,
    HomeAssistantCommandTimeout,
)
from ha_mcp.tools.hacs_backend import StoreApi, detect_store_api
from ha_mcp.tools.hacs_registration import wait_for_repo_registration
from ha_mcp.tools.tools_hacs import HacsTools

MARKETPLACE = StoreApi(
    "marketplace",
    {
        "categories": ["integration", "plugin", "template", "theme"],
        "warning_accepted": True,
        "github_connected": False,
    },
)


async def _identity_timezone(_client, data):
    return data


def _ws(*replies):
    ws = AsyncMock()
    ws.send_command = AsyncMock(side_effect=list(replies))
    return ws


def _ok(result):
    return {"success": True, "result": result}


@contextmanager
def _on_marketplace(ws):
    with (
        patch(
            "ha_mcp.tools.tools_hacs.detect_store_api",
            new=AsyncMock(return_value=MARKETPLACE),
        ),
        patch(
            "ha_mcp.client.websocket_client.get_websocket_client",
            new=AsyncMock(return_value=ws),
        ),
        patch("ha_mcp.tools.tools_hacs.add_timezone_metadata", new=_identity_timezone),
    ):
        yield


@pytest.fixture
def tools():
    return HacsTools(MagicMock())


class TestDetectStoreApi:
    async def test_core_2026_11_is_served_by_the_marketplace(self):
        ws = _ws(_ok({"categories": ["integration"], "warning_accepted": False}))
        api = await detect_store_api(ws)
        assert api.is_marketplace
        assert api.info["warning_accepted"] is False
        ws.send_command.assert_awaited_once_with("marketplace/info")

    async def test_older_core_falls_back_to_the_hacs_integration(self):
        ws = _ws(
            HomeAssistantCommandError(
                "Command failed: Unknown command.", "unknown_command"
            ),
            _ok({"categories": ["integration", "appdaemon"]}),
        )
        api = await detect_store_api(ws)
        assert not api.is_marketplace
        assert api.command("install") == "hacs/repository/download"

    async def test_unloaded_marketplace_is_not_reported_as_missing_hacs(self):
        ws = _ws(HomeAssistantCommandError("not loaded", "not_loaded"))
        with pytest.raises(ToolError) as excinfo:
            await detect_store_api(ws)
        assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
        assert "Marketplace" in str(excinfo.value)
        ws.send_command.assert_awaited_once()

    async def test_neither_store_names_both_options(self):
        unknown = HomeAssistantCommandError("Unknown command.", "unknown_command")
        ws = _ws(unknown, unknown)
        with pytest.raises(ToolError) as excinfo:
            await detect_store_api(ws)
        assert "COMPONENT_NOT_INSTALLED" in str(excinfo.value)
        assert "2026.11" in str(excinfo.value)


class TestMarketplaceCommands:
    async def test_download_installs_through_the_marketplace(self, tools):
        ws = _ws(_ok(None))
        with _on_marketplace(ws):
            await tools.ha_manage_hacs(
                action="download", repository_id="123", version="v2.0.0"
            )
        ws.send_command.assert_awaited_once()
        args, kwargs = ws.send_command.await_args
        assert args == ("marketplace/repository/install",)
        assert kwargs["repository"] == "123"
        assert kwargs["version"] == "v2.0.0"

    async def test_remove_uninstalls_through_the_marketplace(self, tools):
        ws = _ws(_ok({"installed": True, "full_name": "o/r"}), _ok(None))
        with _on_marketplace(ws):
            await tools.ha_manage_hacs(action="remove", repository_id="123")
        assert ws.send_command.await_args_list[1].args == (
            "marketplace/repository/uninstall",
        )

    async def test_unaccepted_warning_tells_the_user_where_to_accept_it(self, tools):
        ws = _ws(
            HomeAssistantCommandError(
                "Command failed: Open the Marketplace and read the warning first",
                "warning_not_accepted",
            )
        )
        with _on_marketplace(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="123")
        text = str(excinfo.value)
        assert "read the warning first" in text
        assert "first-run warning" in text
        assert "cannot accept" in text

    async def test_github_gate_is_not_mistaken_for_a_connection_failure(self, tools):
        ws = _ws(
            HomeAssistantCommandError(
                "Command failed: Connect a GitHub account to the Marketplace first",
                "github_not_connected",
            )
        )
        with _on_marketplace(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(
                action="add_repository", repository="o/r", category="integration"
            )
        text = str(excinfo.value)
        assert "CONNECTION_FAILED" not in text
        assert "Connect GitHub" in text

    async def test_category_is_checked_against_the_store_list(self, tools):
        ws = _ws()
        with _on_marketplace(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_get_hacs_info(action="search", category="appdaemon")
        text = str(excinfo.value)
        assert "VALIDATION_INVALID_PARAMETER" in text
        assert "template" in text
        ws.send_command.assert_not_awaited()

    async def test_category_case_is_the_stores_to_fold(self, tools):
        # Core lower-cases it on add; a capitalised one must not be refused.
        ws = _ws(_ok([]))
        with _on_marketplace(ws):
            await tools.ha_get_hacs_info(action="search", category="Integration")
        assert ws.send_command.await_args.kwargs["categories"] == ["integration"]

    async def test_built_in_replacement_names_the_confirmation(self, tools):
        ws = _ws(
            HomeAssistantCommandError(
                "Command failed: o/youtube replaces the built-in youtube "
                "integration, confirm that before installing it",
                "replaces_built_in",
            )
        )
        with _on_marketplace(ws), pytest.raises(ToolError) as excinfo:
            await tools.ha_manage_hacs(action="download", repository_id="123")
        text = str(excinfo.value)
        assert "confirm_replace_built_in=True" in text
        assert "Verify the target entity" not in text

    @pytest.mark.parametrize("store", [MARKETPLACE, StoreApi("hacs")])
    async def test_confirmed_replacement_reaches_only_the_marketplace(
        self, tools, store
    ):
        ws = _ws(_ok(None))
        with (
            _on_marketplace(ws),
            patch(
                "ha_mcp.tools.tools_hacs.detect_store_api",
                new=AsyncMock(return_value=store),
            ),
        ):
            await tools.ha_manage_hacs(
                action="download", repository_id="123", confirm_replace_built_in=True
            )
        kwargs = ws.send_command.await_args.kwargs
        assert ("confirm_replace_built_in" in kwargs) is store.is_marketplace

    async def test_lovelace_alias_reaches_the_store_as_plugin(self, tools):
        ws = _ws(_ok([]))
        with _on_marketplace(ws):
            await tools.ha_get_hacs_info(action="search", category="lovelace")
        assert ws.send_command.await_args.kwargs["categories"] == ["plugin"]


class TestNativeFieldsReachTheCaller:
    async def test_search_rows_and_store_status_are_the_stores_own(self, tools):
        row = {
            "id": "1",
            "name": "Card",
            "full_name": "o/card",
            "category": "plugin",
            "installed": True,
            "can_install": True,
            "local_path": "/config/www/community/card",
        }
        ws = _ws(_ok([row]))
        with _on_marketplace(ws):
            result = await tools.ha_get_hacs_info(action="search", installed_only=True)
        assert result["results"] == [row]
        assert result["store"]["api"] == "marketplace"
        assert result["store"]["github_connected"] is False

    async def test_info_keeps_every_field_and_the_readme(self, tools):
        detail = {"id": "1", "additional_info": "# README", "replaces_built_in": None}
        ws = _ws(_ok(detail))
        with _on_marketplace(ws):
            result = await tools.ha_get_hacs_info(action="info", repository_id="1")
        assert result["additional_info"] == "# README"
        assert "replaces_built_in" in result
        assert "readme_note" in result

    async def test_a_slow_releases_fetch_leaves_the_info_and_warns(self, tools):
        # The releases command reaches GitHub; timing out must not cost the
        # repository info, and the warning sits beside data, not inside it.
        ws = _ws(
            _ok({"id": "1", "full_name": "o/r", "pending_upgrade": False}),
            HomeAssistantCommandTimeout("timed out"),
        )
        with (
            patch(
                "ha_mcp.tools.tools_hacs.detect_store_api",
                new=AsyncMock(return_value=MARKETPLACE),
            ),
            patch(
                "ha_mcp.client.websocket_client.get_websocket_client",
                new=AsyncMock(return_value=ws),
            ),
            patch(
                "ha_mcp.tools.response_helpers.fetch_ha_timezone",
                new=AsyncMock(return_value=("UTC", False)),
            ),
        ):
            out = await tools.ha_get_hacs_info(
                action="info", repository_id="1", include_releases=True
            )
        assert out["data"]["full_name"] == "o/r"
        assert "warnings" not in out["data"]
        assert out["warnings"] == ["Releases unavailable: timed out"]
        assert ws.send_command.await_args_list[1].kwargs["_wait_timeout"] == 60.0

    async def test_release_notes_come_from_the_update_entity(self, tools):
        detail = {
            "id": "1",
            "pending_upgrade": True,
            "update_entity_id": "update.card_update",
        }
        releases = [{"tag": "v2", "prerelease": False}]
        ws = _ws(_ok(detail), _ok(releases), _ok("## v2\nFixes"))
        with _on_marketplace(ws):
            result = await tools.ha_get_hacs_info(
                action="info", repository_id="1", include_releases=True
            )
        assert result["github_releases"] == releases
        assert result["release_notes"] == "## v2\nFixes"
        assert ws.send_command.await_args_list[2].args == ("update/release_notes",)
        assert (
            ws.send_command.await_args_list[2].kwargs["entity_id"]
            == "update.card_update"
        )


async def test_marketplace_registration_is_one_list_lookup():
    # The Marketplace registers a repository before its add command answers,
    # so no subscription or wait is needed to find it.
    ws = AsyncMock()
    ws.send_command = AsyncMock(
        return_value=_ok([{"id": "9", "full_name": "Owner/Repo"}])
    )
    ws.subscribe_command = AsyncMock()
    repo = await wait_for_repo_registration(
        ws, "owner/repo", timeout=10.0, api=MARKETPLACE
    )
    assert repo == {"id": "9", "full_name": "Owner/Repo"}
    ws.subscribe_command.assert_not_awaited()
    ws.send_command.assert_awaited_once_with("marketplace/repositories/list")
