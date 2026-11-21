"""
Marketplace E2E tests for ``ha_get_hacs_info`` and ``ha_manage_hacs``.

Home Assistant 2026.11 replaced HACS with its built-in Marketplace; the tools
keep their HACS names and drive Core's ``marketplace/*`` WebSocket API. The
test environment's seeded store entry is the Marketplace's.

What reaches GitHub (an install, a refresh, a version or beta change) can be
refused by a rate-limited GitHub on a shared runner, and by an unconnected one
when the session has no ``GITHUB_TOKEN`` to inject into the seeded entry; those
steps skip on that refusal's code, never on its wording. Any other failure
fails.
"""

import logging
import os

import pytest

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ...utilities.assertions import (
    MCPAssertions,
    parse_mcp_result,
    safe_call_tool,
    tool_error_to_result,
)
from ...utilities.cleanup import CleanupSteps

logger = logging.getLogger(__name__)

# The Marketplace's own name for what HA-MCP also accepts as "lovelace".
_NATIVE_CATEGORY = {"lovelace": "plugin"}

# The seed's critical row, already acknowledged.
_SEED_CRITICAL = "test/test"

# A repository the catalog does not list, so it can be added as a custom one.
_CUSTOM_REPOSITORY = "homeassistant-ai/ha-mcp-integration"


def _session_has_github_token() -> bool:
    """The session injects this token into the seeded store entry before boot."""
    return bool(os.environ.get("GITHUB_TOKEN", "").strip())


def extract_store_data(raw_result) -> dict:
    """The tool's ``data`` with its top-level ``success`` and ``warnings``."""
    parsed = parse_mcp_result(raw_result)
    if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
        data = dict(parsed["data"])
        data["success"] = parsed.get("success")
        if "warnings" in parsed:
            data["warnings"] = parsed["warnings"]
        return data
    return parsed


async def store_call(mcp_client, tool_name: str, params: dict) -> dict:
    """Call a store tool; a ToolError comes back as its structured body."""
    try:
        return extract_store_data(await mcp_client.call_tool(tool_name, params))
    except ToolError as exc:
        return tool_error_to_result(exc)


def skip_on_github(data: dict) -> None:
    """Skip a GitHub-bound step that GitHub refused for the runner's account."""
    code = data.get("ha_error_code")
    if code == "github_rate_limited" or (
        code == "github_not_connected" and not _session_has_github_token()
    ):
        pytest.skip(f"GitHub is not usable here: {code}")


async def search(mcp_client, **params) -> dict:
    data = await store_call(
        mcp_client, "ha_get_hacs_info", {"action": "search", **params}
    )
    assert data.get("success"), f"Marketplace search failed: {data.get('error')}"
    return data


async def accept_warning(mcp_client) -> None:
    """Accept the first-run warning for the server's own user (per user in Core)."""
    async with MCPAssertions(mcp_client) as mcp:
        await mcp.call_tool_success(
            "ha_manage_hacs", {"action": "accept_warning", "confirm": True}
        )


def critical_rows(data: dict) -> dict[str, dict]:
    return {row["repository"]: row for row in data["data"]["results"]}


@pytest.mark.hacs
class TestSearch:
    async def test_list_installed(self, mcp_client):
        data = await search(mcp_client, installed_only=True, max_results=100)

        assert data["installed_only"] is True
        assert data["category_filter"] is None
        assert all(repo["installed"] for repo in data["results"])
        assert data["count"] == len(data["results"])
        assert "api" not in data["store"]
        assert "categories" in data["store"]

    @pytest.mark.parametrize("category", ["integration", "lovelace", "theme"])
    async def test_a_category_returns_only_its_rows(self, mcp_client, category):
        data = await search(mcp_client, category=category, max_results=20)

        assert data["category_filter"] == category
        native = _NATIVE_CATEGORY.get(category, category)
        assert data["results"], data
        assert all(repo["category"] == native for repo in data["results"])

    async def test_a_query_returns_the_stores_rows_in_pages(self, mcp_client):
        data = await search(mcp_client, query="mushroom card", max_results=5)

        assert data["query"] == "mushroom card"
        assert data["results"], data
        assert data["count"] == len(data["results"]) <= 5
        assert data["has_more"] == (data["total_matches"] > data["count"])
        # The rows are the store's own: HA-MCP adds no relevance score.
        assert all("score" not in repo for repo in data["results"])

    async def test_a_query_nothing_matches_is_an_empty_page(self, mcp_client):
        data = await search(mcp_client, query="xyznonexistent12345abcdef")

        assert data["total_matches"] == 0
        assert data["results"] == []
        assert data["has_more"] is False

    @pytest.mark.skipif(
        not _session_has_github_token(), reason="GITHUB_TOKEN is not set"
    )
    async def test_the_sessions_github_token_connects_the_marketplace(self, mcp_client):
        """Without it every GitHub-bound test here would skip as not connected."""
        data = await search(mcp_client, max_results=1)

        assert data["store"]["github_connected"] is True, data["store"]


@pytest.mark.hacs
class TestReads:
    async def test_an_unknown_name_is_not_found(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_get_hacs_info",
                {"action": "info", "repository_id": "nonexistent/repo12345"},
            )
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND", data

    async def test_an_unknown_id_keeps_cores_code(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_get_hacs_info", {"action": "info", "repository_id": "999999999"}
            )
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND", data
        assert data["ha_error_code"] == "repository_not_found", data

    async def test_info_of_a_listed_repository(self, mcp_client):
        found = await search(mcp_client, max_results=1)
        assert found["results"], found
        repo = found["results"][0]

        info = await store_call(
            mcp_client,
            "ha_get_hacs_info",
            {"action": "info", "repository_id": repo["full_name"]},
        )
        assert info.get("success"), info
        assert info["id"] == repo["id"]
        assert "readme_note" in info

    async def test_removed_and_critical_lists(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            removed = await mcp.call_tool_success(
                "ha_get_hacs_info", {"action": "removed", "max_results": 5}
            )
            critical = await mcp.call_tool_success(
                "ha_get_hacs_info", {"action": "critical"}
            )

        assert removed["data"]["count"] <= 5
        assert critical["data"]["count"] == len(critical["data"]["results"])
        assert critical_rows(critical)[_SEED_CRITICAL]["acknowledged"] is True

    async def test_the_download_source_form_names_cores_sources(self, mcp_client):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_success(
                "ha_get_hacs_info", {"action": "download_source"}
            )

        fields = data["data"]["options_schema"]["fields"]
        profile = next(f for f in fields if f["name"] == "profile")
        assert "default" in profile["options"], profile
        assert profile["current"] in profile["options"], profile


@pytest.mark.hacs
class TestEntryFlows:
    """Each flow is opened and ended unchanged: its next step would contact
    GitHub or switch the download source."""

    @pytest.mark.parametrize("action", ["connect_github", "set_download_source"])
    async def test_a_flow_opens_and_ends_on_abort(self, mcp_client, action):
        async with MCPAssertions(mcp_client) as mcp:
            opened = await mcp.call_tool_success("ha_manage_hacs", {"action": action})
            ended = await mcp.call_tool_success(
                "ha_manage_hacs",
                {"action": action, "flow_id": opened["data"]["flow_id"], "abort": True},
            )

        assert ended["data"]["aborted"] is True, ended


@pytest.mark.hacs
@pytest.mark.slow
class TestWrites:
    async def test_an_install_waits_for_the_warning_of_the_servers_user(
        self, mcp_client
    ):
        """Core keeps the first-run warning per user: the refusal names the
        confirmed accept action, which accepts it for the server's own user
        (Supervisor's on the in-app lane, which nobody can log in as)."""
        found = await search(mcp_client, max_results=1)
        # Core has no command to take an acceptance back.
        if found["store"]["warning_accepted"]:
            pytest.skip("The server's user already accepted the Marketplace warning")
        assert found["results"], found
        async with MCPAssertions(mcp_client) as mcp:
            refused = await mcp.call_tool_failure(
                "ha_manage_hacs",
                {"action": "download", "repository_id": str(found["results"][0]["id"])},
            )
            assert refused["ha_error_code"] == "warning_not_accepted", refused
            assert "accept_warning" in str(refused), refused
        await accept_warning(mcp_client)
        readback = await search(mcp_client, max_results=1)
        assert readback["store"]["warning_accepted"] is True

    async def test_a_repository_never_installed_is_cores_refusal(self, mcp_client):
        found = await search(mcp_client, query="mushroom", max_results=5)
        target = next((r for r in found["results"] if not r["installed"]), None)
        if target is None:
            pytest.skip("No repository that is not installed")
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_manage_hacs", {"action": "remove", "repository_id": target["id"]}
            )
        assert data["ha_error_code"] == "repository_not_installed", data

    @pytest.mark.parametrize("action", ["download", "remove"])
    async def test_an_unknown_name_is_not_found(self, mcp_client, action):
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_manage_hacs",
                {"action": action, "repository_id": "nonexistent/fake-repo-12345"},
            )
        assert data["error"]["code"] == "RESOURCE_NOT_FOUND", data

    async def test_an_unreadable_repository_is_cores_refusal(self, mcp_client):
        await accept_warning(mcp_client)
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_manage_hacs",
                {
                    "action": "add_repository",
                    "repository": "invalid-format-no-slash",
                    "category": "integration",
                },
            )
        skip_on_github(data)
        assert data["translation_key"] == "invalid_repository", data

    async def test_detecting_home_assistant_itself_is_cores_refusal(self, mcp_client):
        await accept_warning(mcp_client)
        async with MCPAssertions(mcp_client) as mcp:
            data = await mcp.call_tool_failure(
                "ha_get_hacs_info",
                {"action": "detect", "repository": "home-assistant/core"},
            )
        skip_on_github(data)
        assert data["ha_error_code"] == "core_repository", data

    async def test_acknowledging_a_critical_repository_keeps_it_acknowledged(
        self, mcp_client
    ):
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_success(
                "ha_manage_hacs",
                {"action": "acknowledge_critical", "repository": _SEED_CRITICAL},
            )
            critical = await mcp.call_tool_success(
                "ha_get_hacs_info", {"action": "critical"}
            )

        assert critical_rows(critical)[_SEED_CRITICAL]["acknowledged"] is True

    async def test_download_then_remove_roundtrip(self, mcp_client):
        """After the remove the repository no longer reports installed."""
        found = await search(mcp_client, category="theme", max_results=5)
        target = next((r for r in found["results"] if not r["installed"]), None)
        if target is None:
            pytest.skip("No theme that is not installed")
        repo_id = str(target["id"])
        await accept_warning(mcp_client)

        download = await store_call(
            mcp_client,
            "ha_manage_hacs",
            {"action": "download", "repository_id": repo_id},
        )
        skip_on_github(download)
        assert download.get("success"), download
        removed = False
        try:
            async with MCPAssertions(mcp_client) as mcp:
                await mcp.call_tool_success(
                    "ha_manage_hacs", {"action": "remove", "repository_id": repo_id}
                )
                removed = True
                info = await mcp.call_tool_success(
                    "ha_get_hacs_info", {"action": "info", "repository_id": repo_id}
                )
            assert info["data"]["installed"] is False
        finally:
            with CleanupSteps() as cleanup:
                if not removed:
                    with cleanup.step():
                        await safe_call_tool(
                            mcp_client,
                            "ha_manage_hacs",
                            {"action": "remove", "repository_id": repo_id},
                        )

    async def test_beta_versions_are_shown_and_hidden(self, mcp_client):
        found = await search(mcp_client, max_results=1)
        assert found["results"], found
        repo_id = str(found["results"][0]["id"])
        await accept_warning(mcp_client)

        shown = await store_call(
            mcp_client,
            "ha_manage_hacs",
            {"action": "set_beta", "repository_id": repo_id, "show_beta": True},
        )
        skip_on_github(shown)
        assert shown.get("success"), shown
        try:
            async with MCPAssertions(mcp_client) as mcp:
                info = await mcp.call_tool_success(
                    "ha_get_hacs_info", {"action": "info", "repository_id": repo_id}
                )
            assert info["data"]["beta"] is True
        finally:
            with CleanupSteps() as cleanup, cleanup.step():
                await MCPAssertions(mcp_client).call_tool_success(
                    "ha_manage_hacs",
                    {
                        "action": "set_beta",
                        "repository_id": repo_id,
                        "show_beta": False,
                    },
                )

    async def test_a_custom_repository_is_added_read_at_its_branch_and_forgotten(
        self, mcp_client
    ):
        """The integration's own repository is not in the catalog: it is added
        as a custom one, and selecting its default branch installs nothing."""
        await accept_warning(mcp_client)
        added = await store_call(
            mcp_client,
            "ha_manage_hacs",
            {
                "action": "add_repository",
                "repository": _CUSTOM_REPOSITORY,
                "category": "integration",
            },
        )
        skip_on_github(added)
        assert added.get("success"), added
        repo_id = added["repository_id"]
        assert repo_id, added
        forgotten = False
        try:
            async with MCPAssertions(mcp_client) as mcp:
                info = await mcp.call_tool_success(
                    "ha_get_hacs_info", {"action": "info", "repository_id": repo_id}
                )
            branch = info["data"]["default_branch"]
            selected = await store_call(
                mcp_client,
                "ha_manage_hacs",
                {"action": "set_version", "repository_id": repo_id, "version": branch},
            )
            skip_on_github(selected)
            assert selected.get("success"), selected
            # Core clears the selection for the default branch.
            assert selected["selected_tag"] is None, selected
            assert "warnings" not in selected, selected
            async with MCPAssertions(mcp_client) as mcp:
                info = await mcp.call_tool_success(
                    "ha_get_hacs_info", {"action": "info", "repository_id": repo_id}
                )
                assert info["data"]["installed"] is False
                await mcp.call_tool_success(
                    "ha_manage_hacs",
                    {"action": "remove_repository", "repository_id": repo_id},
                )
                forgotten = True
                again = await mcp.call_tool_failure(
                    "ha_manage_hacs",
                    {"action": "remove_repository", "repository_id": repo_id},
                )
            assert again["error"]["code"] == "RESOURCE_NOT_FOUND", again
        finally:
            with CleanupSteps() as cleanup:
                if not forgotten:
                    with cleanup.step():
                        await safe_call_tool(
                            mcp_client,
                            "ha_manage_hacs",
                            {"action": "remove_repository", "repository_id": repo_id},
                        )

    async def test_saving_the_download_source_in_use_keeps_it(self, mcp_client):
        """Only the source the read shows as current is saved: another one
        would switch where the session's later installs come from."""

        def profile(read: dict) -> dict:
            fields = read["data"]["options_schema"]["fields"]
            return next(f for f in fields if f["name"] == "profile")

        async with MCPAssertions(mcp_client) as mcp:
            before = await mcp.call_tool_success(
                "ha_get_hacs_info", {"action": "download_source"}
            )
            current = profile(before)["current"]
            await mcp.call_tool_success(
                "ha_manage_hacs",
                {"action": "set_download_source", "config": {"profile": current}},
            )
            after = await mcp.call_tool_success(
                "ha_get_hacs_info", {"action": "download_source"}
            )

        assert profile(after)["current"] == current, after

    async def test_clear_new_clears_the_flag(self, mcp_client):
        found = await search(mcp_client, status=["new"], max_results=1)
        if not found["results"]:
            pytest.skip("No repository is new")
        repo_id = str(found["results"][0]["id"])
        async with MCPAssertions(mcp_client) as mcp:
            await mcp.call_tool_success(
                "ha_manage_hacs", {"action": "clear_new", "repository_id": repo_id}
            )
        after = await search(mcp_client, status=["new"], max_results=100)
        assert repo_id not in {str(r["id"]) for r in after["results"]}


@pytest.mark.hacs
async def test_update_information_refreshes_a_repository(mcp_client):
    found = await search(mcp_client, max_results=1)
    assert found["results"], found
    repo_id = str(found["results"][0]["id"])

    data = await store_call(
        mcp_client,
        "ha_manage_hacs",
        {"action": "update_information", "repository_id": repo_id},
    )
    skip_on_github(data)
    assert data.get("success") is True, data
    assert data["repository_id"] == repo_id
