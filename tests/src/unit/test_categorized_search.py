"""Categorized tool search via public transform and tool interfaces."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.transforms.categorized_search import (
    DEFAULT_PINNED_TOOLS,
    CategorizedSearchTransform,
    SearchKeywordsTransform,
    categorize_capability,
)

from ._categorized_search_helpers import (
    _make_tool,
    render_search_results,
)
from ._categorized_search_helpers import read_only_on as read_only_on
from ._search_mode_connection import search_connection as search_connection


class TestCategorizeTool:
    """Tests for the _categorize_tool helper function."""

    def test_read_only_tool(self):
        tool = _make_tool("ha_get_state", read_only=True)
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "read"
        )

    def test_destructive_delete_tool(self):
        tool = _make_tool("ha_remove_area_or_floor", destructive=True, idempotent=True)
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "delete"
        )

    def test_destructive_delete_pattern(self):
        tool = _make_tool("ha_delete_zone", destructive=True, idempotent=True)
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "delete"
        )

    def test_destructive_write_tool(self):
        tool = _make_tool("ha_config_set_automation", destructive=True)
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "write"
        )

    def test_no_annotations(self):
        """Tool without annotations defaults to write."""

        async def noop() -> str:
            return "ok"

        tool = Tool.from_function(fn=noop, name="ha_some_tool")
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "write"
        )

    def test_name_pattern_without_destructive_hint_is_write(self):
        """A tool with _remove_ in name but no destructiveHint is NOT delete."""
        tool = _make_tool("ha_remove_something", destructive=False)
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "write"
        )

    def test_read_only_beats_name_pattern(self):
        """readOnlyHint takes precedence even if name contains _delete_."""
        tool = _make_tool("ha_get_delete_history", read_only=True)
        assert (
            categorize_capability(
                tool.name,
                read_only=bool(tool.annotations and tool.annotations.read_only_hint),
                destructive=bool(
                    tool.annotations and tool.annotations.destructive_hint
                ),
            )
            == "read"
        )


class TestRenderResults:
    """Tests for _render_results with execute_via hints."""

    @pytest.fixture
    def transform(self):
        return CategorizedSearchTransform(max_results=5)

    @pytest.mark.anyio
    async def test_read_tool_execute_via(self, transform):
        tools = [_make_tool("ha_get_state", read_only=True, description="Get state")]
        results = await render_search_results(transform, tools)
        assert len(results) == 1
        assert "execute_via" in results[0]
        assert "ha_call_read_tool" in results[0]["execute_via"]
        assert "ha_get_state" in results[0]["execute_via"]

    @pytest.mark.anyio
    async def test_write_tool_execute_via(self, transform):
        tools = [
            _make_tool("ha_config_set_automation", destructive=True, description="Set")
        ]
        results = await render_search_results(transform, tools)
        assert "ha_call_write_tool" in results[0]["execute_via"]
        assert "ha_call_read_tool" not in results[0]["execute_via"]
        assert "ha_call_delete_tool" not in results[0]["execute_via"]
        assert "ha_config_set_automation" in results[0]["execute_via"]

    @pytest.mark.anyio
    async def test_manage_tool_with_read_actions_execute_via_every_proxy(
        self, transform
    ):
        """A manage tool the read-only table knows advertises all three
        proxies, read first, each labelled with the actions it is for."""
        tools = [
            _make_tool("ha_manage_updates", destructive=True, description="Manage")
        ]

        results = await render_search_results(transform, tools)

        assert results[0]["execute_via"] == (
            "Read actions: "
            'client.ha_call_read_tool(name="ha_manage_updates", arguments={...}) '
            'or ha_call_read_tool(name="ha_manage_updates", arguments={...}); '
            "write actions: "
            'client.ha_call_write_tool(name="ha_manage_updates", arguments={...}) '
            'or ha_call_write_tool(name="ha_manage_updates", arguments={...}); '
            "delete actions: "
            'client.ha_call_delete_tool(name="ha_manage_updates", arguments={...}) '
            'or ha_call_delete_tool(name="ha_manage_updates", arguments={...})'
        )

    @pytest.mark.anyio
    async def test_read_only_mode_hints_only_the_read_route(
        self, transform, read_only_on
    ):
        """With the destructive proxies unlisted, the hint is the plain
        read form — no dead-end write or delete route."""
        tools = [
            _make_tool("ha_manage_updates", destructive=True, description="Manage")
        ]

        results = await render_search_results(transform, tools)

        assert results[0]["execute_via"] == (
            'client.ha_call_read_tool(name="ha_manage_updates", arguments={...}) '
            'or ha_call_read_tool(name="ha_manage_updates", arguments={...})'
        )

    @pytest.mark.anyio
    async def test_manage_tool_without_read_actions_execute_via_write_and_delete(
        self, transform
    ):
        """A manage tool outside the read-only table has no read route to
        advertise — the read proxy would refuse every call to it."""
        tools = [_make_tool("ha_manage_hacs", destructive=True, description="HACS")]

        results = await render_search_results(transform, tools)

        assert results[0]["execute_via"] == (
            "Write actions: "
            'client.ha_call_write_tool(name="ha_manage_hacs", arguments={...}) '
            'or ha_call_write_tool(name="ha_manage_hacs", arguments={...}); '
            "delete actions: "
            'client.ha_call_delete_tool(name="ha_manage_hacs", arguments={...}) '
            'or ha_call_delete_tool(name="ha_manage_hacs", arguments={...})'
        )

    @pytest.mark.anyio
    async def test_delete_tool_execute_via(self, transform):
        tools = [
            _make_tool(
                "ha_remove_area_or_floor", destructive=True, description="Remove"
            )
        ]
        results = await render_search_results(transform, tools)
        assert "ha_call_delete_tool" in results[0]["execute_via"]
        assert "ha_remove_area_or_floor" in results[0]["execute_via"]

    @pytest.mark.anyio
    async def test_hit_is_compact(self, transform):
        """A hit carries name, description, params and proxy, no schema."""
        tools = [_make_tool("ha_get_state", read_only=True, description="Get state")]
        results = await render_search_results(transform, tools)
        assert set(results[0]) == {"name", "description", "params", "execute_via"}
        assert results[0]["description"] == "Get state"
        assert "inputSchema" not in results[0]

    @pytest.mark.anyio
    async def test_multiple_tools(self, transform):
        tools = [
            _make_tool("ha_get_state", read_only=True, description="Read"),
            _make_tool("ha_config_set_helper", destructive=True, description="Write"),
            _make_tool("ha_config_delete_zone", destructive=True, description="Delete"),
        ]
        results = await render_search_results(transform, tools)
        assert len(results) == 3
        hints = {result["name"]: result["execute_via"] for result in results}
        assert "ha_call_read_tool" in hints["ha_get_state"]
        assert "ha_call_write_tool" in hints["ha_config_set_helper"]
        assert "ha_call_delete_tool" in hints["ha_config_delete_zone"]


# ---------------------------------------------------------------------------
# CategorizedSearchTransform.transform_tools
# ---------------------------------------------------------------------------


class TestTransformTools:
    """Tests for the transform_tools method."""

    @pytest.fixture
    def transform(self):
        return CategorizedSearchTransform(
            max_results=5,
            always_visible=["ha_get_overview", "ha_restart"],
        )

    @pytest.fixture
    def sample_tools(self):
        return [
            _make_tool("ha_get_overview", read_only=True, description="Overview"),
            _make_tool("ha_restart", destructive=True, description="Restart"),
            _make_tool("ha_get_state", read_only=True, description="Get state"),
            _make_tool(
                "ha_config_set_automation", destructive=True, description="Set auto"
            ),
            _make_tool(
                "ha_remove_area_or_floor", destructive=True, description="Remove area"
            ),
        ]

    @pytest.mark.anyio
    async def test_returns_pinned_plus_synthetic(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        names = [t.name for t in result]

        # Pinned tools
        assert "ha_get_overview" in names
        assert "ha_restart" in names
        # Synthetic tools
        assert "ha_search_tools" in names
        assert "ha_call_read_tool" in names
        assert "ha_call_write_tool" in names
        assert "ha_call_delete_tool" in names
        # Hidden tools should NOT be in the list
        assert "ha_get_state" not in names
        assert "ha_config_set_automation" not in names
        assert "ha_remove_area_or_floor" not in names

    @pytest.mark.anyio
    async def test_read_only_mode_lists_only_the_read_proxy(
        self, transform, sample_tools, read_only_on
    ):
        """Every write is blocked at call time in Read Only Mode, so the
        write and delete proxies are left out of the listing."""
        names = [t.name for t in await transform.transform_tools(sample_tools)]

        assert "ha_search_tools" in names
        assert "ha_call_read_tool" in names
        assert "ha_call_write_tool" not in names
        assert "ha_call_delete_tool" not in names

    @pytest.mark.anyio
    async def test_total_count(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        # 2 pinned + 4 synthetic (search + 3 proxies)
        assert len(result) == 6

    @pytest.mark.anyio
    async def test_search_tool_is_read_only(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        search = next(t for t in result if t.name == "ha_search_tools")
        assert search.annotations is not None
        assert search.annotations.read_only_hint is True

    @pytest.mark.anyio
    async def test_read_proxy_is_read_only(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        proxy = next(t for t in result if t.name == "ha_call_read_tool")
        assert proxy.annotations is not None
        assert proxy.annotations.read_only_hint is True

    @pytest.mark.anyio
    async def test_write_proxy_is_destructive(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        proxy = next(t for t in result if t.name == "ha_call_write_tool")
        assert proxy.annotations is not None
        assert proxy.annotations.destructive_hint is True

    @pytest.mark.anyio
    async def test_delete_proxy_is_destructive(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        proxy = next(t for t in result if t.name == "ha_call_delete_tool")
        assert proxy.annotations is not None
        assert proxy.annotations.destructive_hint is True


# ---------------------------------------------------------------------------
# CategorizedSearchTransform.get_tool
# ---------------------------------------------------------------------------


class TestGetTool:
    """Tests for the get_tool method (proxy resolution)."""

    @pytest.fixture
    def transform(self):
        return CategorizedSearchTransform(max_results=5)

    @pytest.mark.anyio
    async def test_resolves_read_proxy(self, transform):
        call_next = AsyncMock(return_value=None)
        tool = await transform.get_tool("ha_call_read_tool", call_next)
        assert tool is not None
        assert tool.name == "ha_call_read_tool"
        call_next.assert_not_called()

    @pytest.mark.anyio
    async def test_resolves_write_proxy(self, transform):
        call_next = AsyncMock(return_value=None)
        tool = await transform.get_tool("ha_call_write_tool", call_next)
        assert tool is not None
        assert tool.name == "ha_call_write_tool"
        call_next.assert_not_called()

    @pytest.mark.anyio
    async def test_resolves_delete_proxy(self, transform):
        call_next = AsyncMock(return_value=None)
        tool = await transform.get_tool("ha_call_delete_tool", call_next)
        assert tool is not None
        assert tool.name == "ha_call_delete_tool"
        call_next.assert_not_called()

    @pytest.mark.anyio
    async def test_read_only_mode_still_resolves_destructive_proxies(
        self, transform, read_only_on
    ):
        """Unlisted is not unresolvable: a client holding a stale catalog must
        reach the proxy's own structured answer, and ToolSearchHintMiddleware
        only explains a miss while tool search is off."""
        call_next = AsyncMock(return_value=None)

        for name in ("ha_call_write_tool", "ha_call_delete_tool"):
            tool = await transform.get_tool(name, call_next)
            assert tool is not None and tool.name == name
        call_next.assert_not_called()

    @pytest.mark.anyio
    async def test_resolves_search_tool(self, transform):
        call_next = AsyncMock(return_value=None)
        tool = await transform.get_tool("ha_search_tools", call_next)
        assert tool is not None
        assert tool.name == "ha_search_tools"
        call_next.assert_not_called()

    @pytest.mark.anyio
    async def test_delegates_unknown_to_call_next(self, transform):
        real_tool = _make_tool("ha_get_state", read_only=True)
        call_next = AsyncMock(return_value=real_tool)
        tool = await transform.get_tool("ha_get_state", call_next)
        assert tool is not None
        assert tool.name == "ha_get_state"
        call_next.assert_called_once()


# ---------------------------------------------------------------------------
# DEFAULT_PINNED_TOOLS
# ---------------------------------------------------------------------------


class TestDefaultPinnedTools:
    """Verify the shared pinned tools constant."""

    def test_contains_critical_tools(self):
        assert "ha_get_overview" in DEFAULT_PINNED_TOOLS
        assert "ha_manage_backup" in DEFAULT_PINNED_TOOLS
        assert "ha_report_issue" in DEFAULT_PINNED_TOOLS
        assert "ha_search" in DEFAULT_PINNED_TOOLS
        assert "ha_get_skill_guide" in DEFAULT_PINNED_TOOLS

    def test_is_immutable_tuple(self):
        assert isinstance(DEFAULT_PINNED_TOOLS, tuple)

    def test_recovery_tools_removed_from_defaults(self):
        """Regression guard for #966.

        ``ha_restart`` and ``ha_reload_core`` are operational recovery
        actions invoked at low frequency. Keeping them pinned by default
        wasted always-visible budget that the LLM rarely needed — they
        moved behind the search proxy, still reachable via
        ``ha_search_tools`` for the rare case where the user asks for
        them, and still available as an explicit user pin via the
        settings UI for installs that want them up-front.
        """
        assert "ha_restart" not in DEFAULT_PINNED_TOOLS
        assert "ha_reload_core" not in DEFAULT_PINNED_TOOLS

    def test_dangerous_tools_not_pinned(self):
        """Regression guard for #966.

        ``ha_config_set_yaml`` (arbitrary YAML config rewrite) and
        ``ha_manage_custom_tool`` (arbitrary sandboxed Python execution)
        were previously pinned so users could gate them via per-tool MCP
        permission prompts even when toolsearch hid the rest of the
        catalog. The tool security policies middleware now gates them at
        call time regardless of catalog visibility, so they should NOT be
        in the default pinned set — keeping them out of the always-visible
        list reduces the LLM's tool surface without losing the safety
        check.
        """
        assert "ha_config_set_yaml" not in DEFAULT_PINNED_TOOLS
        assert "ha_manage_custom_tool" not in DEFAULT_PINNED_TOOLS


# ---------------------------------------------------------------------------
# categorized_call dispatch (proxy execution)
# ---------------------------------------------------------------------------


class TestSearchKeywordsTransform:
    """Tests for the SearchKeywordsTransform."""

    @pytest.mark.anyio
    async def test_keywords_appended(self):
        """Keywords are appended to existing description."""
        transform = SearchKeywordsTransform(
            keywords={"ha_search": "find lookup discover"}
        )
        tool = _make_tool("ha_search", read_only=True, description="Search entities.")
        result = await transform.list_tools([tool])
        assert len(result) == 1
        assert result[0].description.startswith("Search entities.")
        assert "find lookup discover" in result[0].description

    @pytest.mark.anyio
    async def test_overrides_replace_description(self):
        """Overrides completely replace the description."""
        transform = SearchKeywordsTransform(
            overrides={"ha_search": "Narrowed description."}
        )
        tool = _make_tool(
            "ha_search", read_only=True, description="Original broad description."
        )
        result = await transform.list_tools([tool])
        assert result[0].description == "Narrowed description."

    @pytest.mark.anyio
    async def test_override_takes_priority_over_keywords(self):
        """When both override and keywords exist, override wins."""
        transform = SearchKeywordsTransform(
            keywords={"ha_search": "extra keywords"},
            overrides={"ha_search": "Override wins."},
        )
        tool = _make_tool("ha_search", read_only=True, description="Original.")
        result = await transform.list_tools([tool])
        assert result[0].description == "Override wins."
        assert "extra keywords" not in result[0].description

    @pytest.mark.anyio
    async def test_no_match_leaves_description_unchanged(self):
        """Tools not in keywords or overrides are unchanged."""
        transform = SearchKeywordsTransform(keywords={"ha_other_tool": "some keywords"})
        tool = _make_tool("ha_get_state", read_only=True, description="Get state.")
        result = await transform.list_tools([tool])
        assert result[0].description == "Get state."

    @pytest.mark.anyio
    async def test_get_tool_enriches(self):
        """get_tool also applies enrichment."""
        transform = SearchKeywordsTransform(keywords={"ha_get_state": "status check"})
        tool = _make_tool("ha_get_state", read_only=True, description="Get state.")
        call_next = AsyncMock(return_value=tool)
        result = await transform.get_tool("ha_get_state", call_next)
        assert result is not None
        assert "status check" in result.description

    @pytest.mark.anyio
    async def test_get_tool_returns_none_for_missing(self):
        """get_tool returns None when call_next returns None."""
        transform = SearchKeywordsTransform()
        call_next = AsyncMock(return_value=None)
        result = await transform.get_tool("ha_nonexistent", call_next)
        assert result is None


# ---------------------------------------------------------------------------
# HomeAssistantSmartMCPServer._apply_search_keyword_enrichment
#
# Regression coverage for #940: SearchKeywordsTransform must be applied
# unconditionally so Claude's native deferred-tool search (claude.ai, BM25)
# can locate ha-mcp tools for common natural-language queries, regardless
# of whether ENABLE_TOOL_SEARCH is set.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("search_enabled", [False, True])
async def test_keywords_are_served_in_both_catalog_modes(
    core_client: HomeAssistantClient, monkeypatch, search_enabled
):
    from ._real_server import build_server

    monkeypatch.setenv("ENABLE_TOOL_SEARCH", str(search_enabled).lower())
    monkeypatch.setenv("ENABLE_LITE_DOCSTRINGS", "false")
    server = build_server(monkeypatch, core_client)
    # Public get_tool returns the enriched hidden definition even in search mode.
    tool = await server.mcp.get_tool("ha_config_set_automation")
    assert tool is not None
    for term in ("create", "update", "modify", "edit", "new", "save"):
        assert term in tool.description.lower()


async def test_keyword_transform_failure_is_logged_not_raised(
    core_client: HomeAssistantClient, monkeypatch, caplog
):
    from ha_mcp._vendor.fastmcp import FastMCP

    from ._real_server import build_server

    original = FastMCP.add_transform

    def add_transform(server, transform):
        if isinstance(transform, SearchKeywordsTransform):
            raise RuntimeError("boom")
        return original(server, transform)

    monkeypatch.setattr(FastMCP, "add_transform", add_transform)
    with caplog.at_level("ERROR"):
        server = build_server(monkeypatch, core_client)
    assert server.mcp is not None
    assert "SearchKeywordsTransform" in caplog.text


@pytest.mark.parametrize(
    ("tool_name", "terms"),
    [
        (
            "ha_manage_energy_prefs",
            (
                "price",
                "tariff",
                "cost",
                "kwh",
                "rate",
                "peak",
                "off-peak",
                "electricity",
                "contract",
                "read",
                "get",
            ),
        ),
        (
            "ha_manage_blueprints",
            (
                "blueprint",
                "import",
                "delete",
                "substitute",
                "take-control",
                "list",
                "ha_get_blueprint",
                "ha_import_blueprint",
            ),
        ),
        ("ha_config_set_automation", ("enable", "disable", "run", "trigger")),
        ("ha_config_set_script", ("run", "start", "stop")),
        ("ha_config_set_scene", ("activate",)),
        ("ha_manage_updates", ("repair", "ignore", "dismiss")),
        ("ha_set_integration", ("log", "debug")),
    ],
)
async def test_search_vocabulary_reaches_the_served_tool(
    core_client: HomeAssistantClient, monkeypatch, tool_name, terms
):
    from ._real_server import build_server

    monkeypatch.setenv("ENABLE_LITE_DOCSTRINGS", "false")
    server = build_server(monkeypatch, core_client)
    tool = await server.mcp.get_tool(tool_name)
    assert tool is not None
    for term in terms:
        assert term in tool.description.lower()
