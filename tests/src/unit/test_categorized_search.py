"""Unit tests for CategorizedSearchTransform.

Tests the categorization logic, transform_tools output, get_tool resolution,
proxy category validation, dispatch execution, and SearchKeywordsTransform.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ha_mcp.read_only import READ_ONLY_EXEMPT_TOOLS
from ha_mcp.transforms.categorized_search import (
    DEFAULT_PINNED_TOOLS,
    CategorizedSearchTransform,
    SearchKeywordsTransform,
    _categorize_tool,
    _is_manage_tool,
)


@pytest.fixture
def read_only_on(monkeypatch):
    monkeypatch.setattr(
        "ha_mcp.config.get_global_settings",
        lambda: SimpleNamespace(read_only_mode=True),
    )


def _make_tool(
    name: str,
    *,
    read_only: bool = False,
    destructive: bool = False,
    idempotent: bool = False,
    description: str = "",
) -> Tool:
    """Create a minimal Tool for testing."""

    async def noop() -> str:
        return "ok"

    annotations = ToolAnnotations(
        readOnlyHint=read_only,
        destructiveHint=destructive,
        idempotentHint=idempotent,
    )
    return Tool.from_function(
        fn=noop, name=name, description=description, annotations=annotations
    )


# ---------------------------------------------------------------------------
# _categorize_tool
# ---------------------------------------------------------------------------


class TestCategorizeTool:
    """Tests for the _categorize_tool helper function."""

    def test_read_only_tool(self):
        tool = _make_tool("ha_get_state", read_only=True)
        assert _categorize_tool(tool) == "read"

    def test_destructive_delete_tool(self):
        tool = _make_tool("ha_remove_area_or_floor", destructive=True, idempotent=True)
        assert _categorize_tool(tool) == "delete"

    def test_destructive_delete_pattern(self):
        tool = _make_tool("ha_delete_zone", destructive=True, idempotent=True)
        assert _categorize_tool(tool) == "delete"

    def test_destructive_write_tool(self):
        tool = _make_tool("ha_config_set_automation", destructive=True)
        assert _categorize_tool(tool) == "write"

    def test_no_annotations(self):
        """Tool without annotations defaults to write."""

        async def noop() -> str:
            return "ok"

        tool = Tool.from_function(fn=noop, name="ha_some_tool")
        assert _categorize_tool(tool) == "write"

    def test_name_pattern_without_destructive_hint_is_write(self):
        """A tool with _remove_ in name but no destructiveHint is NOT delete."""
        tool = _make_tool("ha_remove_something", destructive=False)
        assert _categorize_tool(tool) == "write"

    def test_read_only_beats_name_pattern(self):
        """readOnlyHint takes precedence even if name contains _delete_."""
        tool = _make_tool("ha_get_delete_history", read_only=True)
        assert _categorize_tool(tool) == "read"


class TestManageToolConvention:
    """Cross-category proxy admission keys on the ``manage`` naming convention."""

    def test_manage_names_match(self):
        assert _is_manage_tool("ha_manage_backup")
        assert _is_manage_tool("ha_dev_manage_server")
        assert not _is_manage_tool("ha_config_set_automation")
        assert not _is_manage_tool("ha_manager")

    def test_every_read_only_exemption_is_a_manage_tool(self):
        """The read route in search results is advertised for manage tools
        the read-only table knows. A table entry that is not a manage tool
        would be admitted on the read proxy yet never advertised there."""
        for name in READ_ONLY_EXEMPT_TOOLS:
            assert _is_manage_tool(name), name


# ---------------------------------------------------------------------------
# CategorizedSearchTransform._render_results (execute_via hints)
# ---------------------------------------------------------------------------


class TestRenderResults:
    """Tests for _render_results with execute_via hints."""

    @pytest.fixture
    def transform(self):
        return CategorizedSearchTransform(max_results=5)

    @pytest.mark.anyio
    async def test_read_tool_execute_via(self, transform):
        tools = [_make_tool("ha_get_state", read_only=True, description="Get state")]
        results = await transform._render_results(tools)
        assert len(results) == 1
        assert "execute_via" in results[0]
        assert "ha_call_read_tool" in results[0]["execute_via"]
        assert "ha_get_state" in results[0]["execute_via"]

    @pytest.mark.anyio
    async def test_write_tool_execute_via(self, transform):
        tools = [
            _make_tool("ha_config_set_automation", destructive=True, description="Set")
        ]
        results = await transform._render_results(tools)
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

        results = await transform._render_results(tools)

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

        results = await transform._render_results(tools)

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

        results = await transform._render_results(tools)

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
        results = await transform._render_results(tools)
        assert "ha_call_delete_tool" in results[0]["execute_via"]
        assert "ha_remove_area_or_floor" in results[0]["execute_via"]

    @pytest.mark.anyio
    async def test_preserves_standard_fields(self, transform):
        """Should preserve name, description, annotations, inputSchema."""
        tools = [_make_tool("ha_get_state", read_only=True, description="Get state")]
        results = await transform._render_results(tools)
        assert results[0]["name"] == "ha_get_state"
        assert "description" in results[0]
        assert "inputSchema" in results[0]

    @pytest.mark.anyio
    async def test_multiple_tools(self, transform):
        tools = [
            _make_tool("ha_get_state", read_only=True, description="Read"),
            _make_tool("ha_config_set_helper", destructive=True, description="Write"),
            _make_tool("ha_config_delete_zone", destructive=True, description="Delete"),
        ]
        results = await transform._render_results(tools)
        assert len(results) == 3
        assert "ha_call_read_tool" in results[0]["execute_via"]
        assert "ha_call_write_tool" in results[1]["execute_via"]
        assert "ha_call_delete_tool" in results[2]["execute_via"]


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
        assert search.annotations.readOnlyHint is True

    @pytest.mark.anyio
    async def test_read_proxy_is_read_only(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        proxy = next(t for t in result if t.name == "ha_call_read_tool")
        assert proxy.annotations is not None
        assert proxy.annotations.readOnlyHint is True

    @pytest.mark.anyio
    async def test_write_proxy_is_destructive(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        proxy = next(t for t in result if t.name == "ha_call_write_tool")
        assert proxy.annotations is not None
        assert proxy.annotations.destructiveHint is True

    @pytest.mark.anyio
    async def test_delete_proxy_is_destructive(self, transform, sample_tools):
        result = await transform.transform_tools(sample_tools)
        proxy = next(t for t in result if t.name == "ha_call_delete_tool")
        assert proxy.annotations is not None
        assert proxy.annotations.destructiveHint is True


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


def _prepopulate_cache(transform, tools):
    """Pre-populate category cache and mock get_tool_catalog so rebuild is a no-op."""
    for tool in tools:
        cat = _categorize_tool(tool)
        if cat == "read":
            transform._read_tools.add(tool.name)
        elif cat == "delete":
            transform._delete_tools.add(tool.name)
        else:
            transform._write_tools.add(tool.name)
    # Set the real hash AND mock get_tool_catalog so the hash check can proceed
    transform._last_catalog_hash = CategorizedSearchTransform._catalog_hash(tools)
    transform.get_tool_catalog = AsyncMock(return_value=tools)


def _make_ctx(call_tool_return: Any = "tool_result"):
    """Create a mock Context with fastmcp.call_tool."""
    ctx = MagicMock()
    ctx.fastmcp.call_tool = AsyncMock(return_value=call_tool_return)
    return ctx


class TestCategorizedCallDispatch:
    """Tests for the categorized_call closure — the core dispatch function."""

    @pytest.fixture
    def transform(self):
        t = CategorizedSearchTransform(max_results=5)
        _prepopulate_cache(
            t,
            [
                _make_tool("ha_get_state", read_only=True),
                _make_tool("ha_search", read_only=True),
                _make_tool("ha_config_set_automation", destructive=True),
                _make_tool("ha_call_service", destructive=True),
                _make_tool("ha_remove_area_or_floor", destructive=True),
            ],
        )
        return t

    def _get_proxy_fn(self, transform, category):
        """Get the callable fn from a proxy Tool."""
        annotations_map = {
            "read": ToolAnnotations(readOnlyHint=True),
            "write": ToolAnnotations(destructiveHint=True),
            "delete": ToolAnnotations(destructiveHint=True),
        }
        proxy = transform._make_categorized_proxy(
            proxy_name=f"ha_call_{category}_tool",
            category=category,
            annotations=annotations_map[category],
            description=f"Test {category} proxy",
        )
        return proxy.fn

    @pytest.mark.anyio
    async def test_read_proxy_happy_path(self, transform):
        """Correct read tool via read proxy succeeds."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = self._get_proxy_fn(transform, "read")
        result = await fn("ha_get_state", {"entity_id": "light.kitchen"}, ctx)
        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.anyio
    async def test_read_proxy_admits_a_mixed_tools_read_action(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """A write-categorised tool's read actions stay reachable on the read
        proxy (#2329). Category membership is unchanged; the call is admitted
        because the read-only predicate approves it."""
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        assert "ha_manage_blueprints" not in transform._read_tools
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "read")

        result = await fn("ha_manage_blueprints", {"action": "list"}, ctx)

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_blueprints", {"action": "list"}
        )

    @pytest.mark.anyio
    async def test_read_proxy_refuses_a_mixed_tools_write_action(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """Admission is per call, not per tool: a state-changing action on the
        same tool is still refused on the read proxy."""
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")

        with pytest.raises(ToolError) as exc_info:
            await fn(
                "ha_manage_blueprints",
                {"action": "delete", "path": "user/motion.yaml", "confirm": True},
                ctx,
            )
        body = json.loads(str(exc_info.value))
        error = body["error"]
        assert error["code"] == "VALIDATION_INVALID_PARAMETER"
        assert body["correct_proxy"] == "ha_call_write_tool"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_read_proxy_refuses_a_manage_tool_outside_the_read_only_table(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """No exemption entry means no read predicate, so the read proxy fails
        closed for every call — even one whose action sounds harmless."""
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_hacs", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")

        with pytest.raises(ToolError) as exc_info:
            await fn("ha_manage_hacs", {"action": "update_information"}, ctx)
        body = json.loads(str(exc_info.value))
        assert body["correct_proxy"] == "ha_call_write_tool"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_read_proxy_reports_an_exempt_tool_missing_from_the_catalog(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The read-only table alone does not admit a call: the tool must be
        in the live catalog, or the caller gets not-found rather than a
        dispatch into FastMCP's unknown-tool error."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")

        with pytest.raises(ToolError) as exc_info:
            await fn("ha_manage_blueprints", {"action": "list"}, ctx)
        body = json.loads(str(exc_info.value))
        error = body["error"]
        assert error["code"] == "RESOURCE_NOT_FOUND"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_write_proxy_runs_every_action_of_a_manage_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The write proxy is the manage tool's home category and admits all
        of it, delete actions included: those are not statically enumerable
        (ha_manage_radio and ha_manage_updates take a free-form action), so
        the proxy does not pretend to carve them out."""
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "write")

        for arguments in (
            {"action": "list"},
            {"action": "import", "url": "https://example.invalid/bp.yaml"},
            {"action": "delete", "path": "user/motion.yaml", "confirm": True},
        ):
            assert await fn("ha_manage_blueprints", arguments, ctx) == {"success": True}
        assert ctx.fastmcp.call_tool.await_count == 3

    @pytest.mark.anyio
    async def test_delete_proxy_runs_every_action_of_a_manage_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The delete proxy is the most gated one, so it runs the whole
        manage tool — reads included — with no per-call check."""
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_manage_hacs", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "delete")

        for name, arguments in (
            ("ha_manage_blueprints", {"action": "list"}),
            ("ha_manage_blueprints", {"action": "import", "url": "https://x.invalid"}),
            (
                "ha_manage_blueprints",
                {"action": "delete", "path": "x", "confirm": True},
            ),
            ("ha_manage_hacs", {"action": "remove", "repository": "a/b"}),
        ):
            assert await fn(name, arguments, ctx) == {"success": True}
        assert ctx.fastmcp.call_tool.await_count == 4

    @pytest.mark.anyio
    async def test_delete_proxy_refuses_a_plain_write_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """Only manage tools cross categories — an ordinary write tool on
        the delete proxy is still the wrong category."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "delete")

        with pytest.raises(ToolError) as exc_info:
            await fn("ha_config_set_automation", {"config": {}}, ctx)
        body = json.loads(str(exc_info.value))
        assert body["correct_proxy"] == "ha_call_write_tool"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_read_proxy_refuses_a_plain_write_tool(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The admission covers mixed tools only — a tool with no read surface
        is refused as before."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")

        with pytest.raises(ToolError):
            await fn("ha_config_set_automation", {"config": {}}, ctx)
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_proxy_adapts_a_retired_blueprint_call(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """A stale catalog calling ha_get_blueprint reaches the consolidated
        tool with the action its old signature never carried."""
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "write")

        await fn("ha_get_blueprint", {"domain": "script"}, ctx)

        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_blueprints", {"action": "list", "domain": "script"}
        )

    @pytest.mark.anyio
    async def test_proxy_dispatches_a_renamed_tool_under_its_current_name(
        self, transform
    ):
        """A stale catalog can name a tool that has since been renamed.

        The alias middleware would rewrite a direct call, but a proxied one is
        checked against the live category set first and would be rejected as
        the wrong category before the re-dispatch ever reaches the middleware.
        """
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_app", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "write")

        result = await fn("ha_manage_addon", {"slug": "core_ssh"}, ctx)

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_app", {"slug": "core_ssh"}
        )

    @pytest.mark.anyio
    async def test_write_proxy_happy_path(self, transform):
        """Correct write tool via write proxy succeeds."""
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "write")
        result = await fn("ha_config_set_automation", {"config": {}}, ctx)
        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_config_set_automation", {"config": {}}
        )

    @pytest.mark.anyio
    async def test_delete_proxy_happy_path(self, transform):
        """Correct delete tool via delete proxy succeeds."""
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "delete")
        result = await fn(
            "ha_remove_area_or_floor", {"kind": "area", "id": "garage"}, ctx
        )
        assert result == {"success": True}

    @pytest.mark.anyio
    async def test_wrong_category_rejected_write_via_read(self, transform):
        """Write tool via read proxy is rejected with correct proxy suggestion."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_config_set_automation", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_call_write_tool" in error["error"]["message"]
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_wrong_category_rejected_read_via_write(self, transform):
        """Read tool via write proxy is rejected."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "write")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_call_read_tool" in error["error"]["message"]

    @pytest.mark.anyio
    async def test_wrong_category_rejected_delete_via_read(self, transform):
        """Delete tool via read proxy is rejected."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_remove_area_or_floor", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert "ha_call_delete_tool" in error["error"]["message"]

    @pytest.mark.anyio
    async def test_unknown_tool_returns_not_found(self, transform):
        """Tool not in any category returns RESOURCE_NOT_FOUND."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_nonexistent_tool", {}, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
        assert "ha_nonexistent_tool" in error["error"]["message"]

    @pytest.mark.anyio
    async def test_none_arguments_defaults_to_empty(self, transform):
        """Calling with arguments=None still works."""
        ctx = _make_ctx(call_tool_return="ok")
        fn = self._get_proxy_fn(transform, "read")
        result = await fn("ha_get_state", None, ctx)
        assert result == "ok"
        ctx.fastmcp.call_tool.assert_called_once_with("ha_get_state", None)


# ---------------------------------------------------------------------------
# Double-unwrap detection
# ---------------------------------------------------------------------------


class TestDoubleUnwrap:
    """Tests for double-wrapped proxy call detection and unwrapping."""

    @pytest.fixture
    def transform(self):
        t = CategorizedSearchTransform(max_results=5)
        _prepopulate_cache(
            t,
            [
                _make_tool("ha_get_state", read_only=True),
                _make_tool("ha_config_set_automation", destructive=True),
                _make_tool("ha_remove_area_or_floor", destructive=True),
            ],
        )
        return t

    def _get_proxy_fn(self, transform, category):
        annotations_map = {
            "read": ToolAnnotations(readOnlyHint=True),
            "write": ToolAnnotations(destructiveHint=True),
            "delete": ToolAnnotations(destructiveHint=True),
        }
        proxy = transform._make_categorized_proxy(
            proxy_name=f"ha_call_{category}_tool",
            category=category,
            annotations=annotations_map[category],
            description=f"Test {category} proxy",
        )
        return proxy.fn

    @pytest.mark.anyio
    async def test_double_wrapped_read_unwraps_correctly(self, transform):
        """Double-wrapped read tool via read proxy unwraps and succeeds."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = self._get_proxy_fn(transform, "read")
        # LLM accidentally nests: ha_call_read_tool(name="ha_call_read_tool",
        #   arguments={"name": "ha_get_state", "arguments": {"entity_id": "x"}})
        result = await fn(
            "ha_call_read_tool",
            {"name": "ha_get_state", "arguments": {"entity_id": "x"}},
            ctx,
        )
        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "x"}
        )

    @pytest.mark.anyio
    async def test_double_wrapped_renamed_tool_unwraps_under_its_current_name(
        self, transform
    ):
        """The envelope's inner name comes from the same stale catalog.

        The recovery block tests it against the live category sets, so an
        unresolved retired name fails the unwrap and the call dies with a
        wrong-category error naming the proxy — the alias covering one envelope
        shape and not the other.
        """
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_app", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "write")

        result = await fn(
            "ha_call_write_tool",
            {"name": "ha_manage_addon", "arguments": {"slug": "core_ssh"}},
            ctx,
        )

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_app", {"slug": "core_ssh"}
        )

    @pytest.mark.anyio
    async def test_double_wrapped_consolidated_tool_gets_its_action(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The envelope carries the retired signature too (#2329).

        A nested `ha_get_blueprint` resolves to `ha_manage_blueprints`, which
        dispatches on `action` — so the unwrap has to adapt the inner
        arguments, not just the inner name, or the call arrives actionless.
        """
        _prepopulate_cache(
            transform,
            [
                _make_tool("ha_manage_blueprints", destructive=True),
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        ctx = _make_ctx(call_tool_return={"success": True})
        fn = self._get_proxy_fn(transform, "write")

        result = await fn(
            "ha_call_write_tool",
            {"name": "ha_get_blueprint", "arguments": {"domain": "script"}},
            ctx,
        )

        assert result == {"success": True}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_manage_blueprints", {"action": "list", "domain": "script"}
        )

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("nested", "code"),
        [
            # A bare string is first tried as JSON, exactly as the outer
            # payload is, so it classifies as invalid JSON rather than as a
            # non-object.
            ("x", "VALIDATION_INVALID_JSON"),
            ([1, 2], "VALIDATION_INVALID_PARAMETER"),
            (7, "VALIDATION_INVALID_PARAMETER"),
        ],
    )
    async def test_double_wrapped_non_object_arguments_refused(
        self, transform: CategorizedSearchTransform, nested: Any, code: str
    ) -> None:
        """A scalar or list nested payload is refused with the same structured
        error the outer payload gets — never a bare ValueError out of the
        retired-name adapter's dict()."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")

        with pytest.raises(ToolError) as exc:
            await fn(
                "ha_call_read_tool",
                {"name": "ha_get_state", "arguments": nested},
                ctx,
            )

        payload = json.loads(str(exc.value))
        assert payload["error"]["code"] == code
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_double_wrapped_json_string_arguments_are_parsed(
        self, transform: CategorizedSearchTransform
    ) -> None:
        """The nested payload gets the same JSON-string tolerance as the outer
        one, so a model that serialized it twice still dispatches."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = self._get_proxy_fn(transform, "read")

        result = await fn(
            "ha_call_read_tool",
            {"name": "ha_get_state", "arguments": '{"entity_id": "light.kitchen"}'},
            ctx,
        )

        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.anyio
    async def test_double_wrapped_wrong_category_still_rejected(self, transform):
        """Double-wrapped write tool via read proxy is rejected after unwrapping."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        # LLM wraps write tool in read proxy
        with pytest.raises(ToolError) as exc_info:
            await fn(
                "ha_call_read_tool",
                {"name": "ha_config_set_automation", "arguments": {}},
                ctx,
            )
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        assert "ha_call_write_tool" in error["error"]["message"]
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_no_unwrap_when_name_not_proxy(self, transform):
        """Non-proxy name with name/arguments keys is NOT unwrapped."""
        ctx = _make_ctx(call_tool_return="ok")
        fn = self._get_proxy_fn(transform, "read")
        # A real tool called with args that happen to contain "name" and "arguments"
        result = await fn(
            "ha_get_state",
            {"name": "some_value", "arguments": "other"},
            ctx,
        )
        # Should call ha_get_state directly (not unwrap)
        assert result == "ok"
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"name": "some_value", "arguments": "other"}
        )


# ---------------------------------------------------------------------------
# JSON-string arguments fallback
# ---------------------------------------------------------------------------


class TestArgumentsAsString:
    """Tolerate arguments passed as a JSON string instead of a dict.

    Small models sometimes serialize the nested `arguments` param to a JSON
    string before sending it, which FastMCP's schema validator rejects. The
    proxy accepts a string fallback, parses it, and forwards the resulting
    dict — same recovery spirit as the double-unwrap path.
    """

    @pytest.fixture
    def transform(self):
        t = CategorizedSearchTransform(max_results=5)
        _prepopulate_cache(
            t,
            [
                _make_tool("ha_get_state", read_only=True),
            ],
        )
        return t

    def _get_proxy_fn(self, transform, category):
        annotations_map = {
            "read": ToolAnnotations(readOnlyHint=True),
            "write": ToolAnnotations(destructiveHint=True),
            "delete": ToolAnnotations(destructiveHint=True),
        }
        proxy = transform._make_categorized_proxy(
            proxy_name=f"ha_call_{category}_tool",
            category=category,
            annotations=annotations_map[category],
            description=f"Test {category} proxy",
        )
        return proxy.fn

    @pytest.mark.anyio
    async def test_json_string_arguments_parsed_and_forwarded(self, transform):
        """A JSON-object string is parsed to a dict and forwarded."""
        ctx = _make_ctx(call_tool_return={"state": "on"})
        fn = self._get_proxy_fn(transform, "read")
        result = await fn("ha_get_state", '{"entity_id": "light.kitchen"}', ctx)
        assert result == {"state": "on"}
        ctx.fastmcp.call_tool.assert_called_once_with(
            "ha_get_state", {"entity_id": "light.kitchen"}
        )

    @pytest.mark.anyio
    async def test_invalid_json_string_rejected(self, transform):
        """Non-JSON string raises with INVALID_JSON and does not dispatch."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", "not valid json", ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_JSON"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_deeply_nested_json_string_rejected(self, transform):
        """A JSON string nested past the interpreter's recursion limit makes
        ``json.loads`` raise ``RecursionError``, not ``JSONDecodeError``; it
        must still come back as the structured INVALID_JSON refusal rather
        than escape as an internal error (CodeRabbit)."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", "[" * 20_000, ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_JSON"
        ctx.fastmcp.call_tool.assert_not_called()

    @pytest.mark.anyio
    async def test_json_string_not_object_rejected(self, transform):
        """JSON that parses to a non-object (e.g. array) raises a clear error."""
        ctx = _make_ctx()
        fn = self._get_proxy_fn(transform, "read")
        with pytest.raises(ToolError) as exc_info:
            await fn("ha_get_state", "[1, 2, 3]", ctx)
        error = json.loads(str(exc_info.value))
        assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"
        ctx.fastmcp.call_tool.assert_not_called()


# ---------------------------------------------------------------------------
# _rebuild_category_cache
# ---------------------------------------------------------------------------


class TestRebuildCategoryCache:
    """Tests for the _rebuild_category_cache method."""

    @pytest.mark.anyio
    async def test_populates_all_three_sets(self):
        """Cache correctly populates read, write, and delete sets."""
        transform = CategorizedSearchTransform(max_results=5)
        tools = [
            _make_tool("ha_get_state", read_only=True),
            _make_tool("ha_list_areas", read_only=True),
            _make_tool("ha_config_set_automation", destructive=True),
            _make_tool("ha_remove_area_or_floor", destructive=True),
        ]
        with patch.object(
            transform, "get_tool_catalog", new_callable=AsyncMock, return_value=tools
        ):
            await transform._rebuild_category_cache(None)

        assert "ha_get_state" in transform._read_tools
        assert "ha_list_areas" in transform._read_tools
        assert "ha_config_set_automation" in transform._write_tools
        assert "ha_remove_area_or_floor" in transform._delete_tools

    @pytest.mark.anyio
    async def test_cache_updates_on_catalog_change(self):
        """Cache rebuilds when catalog hash changes."""
        transform = CategorizedSearchTransform(max_results=5)
        tools_v1 = [_make_tool("ha_get_state", read_only=True)]
        tools_v2 = [
            _make_tool("ha_get_state", read_only=True),
            _make_tool("ha_new_write", destructive=True),
        ]
        with patch.object(
            transform, "get_tool_catalog", new_callable=AsyncMock, return_value=tools_v1
        ):
            await transform._rebuild_category_cache(None)
        assert "ha_new_write" not in transform._write_tools

        with patch.object(
            transform, "get_tool_catalog", new_callable=AsyncMock, return_value=tools_v2
        ):
            await transform._rebuild_category_cache(None)
        assert "ha_new_write" in transform._write_tools

    @pytest.mark.anyio
    async def test_cache_no_op_when_unchanged(self):
        """Cache skips rebuild when catalog hash is unchanged."""
        transform = CategorizedSearchTransform(max_results=5)
        tools = [_make_tool("ha_get_state", read_only=True)]
        mock_catalog = AsyncMock(return_value=tools)
        with patch.object(transform, "get_tool_catalog", mock_catalog):
            await transform._rebuild_category_cache(None)
            await transform._rebuild_category_cache(None)
        # get_tool_catalog called twice (hash check), but sets only built once
        assert mock_catalog.call_count == 2
        assert "ha_get_state" in transform._read_tools


# ---------------------------------------------------------------------------
# SearchKeywordsTransform
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


class TestApplySearchKeywordEnrichment:
    """Tests for the always-on keyword enrichment hook on the server class."""

    def _make_server_stub(self, *, enable_tool_search: bool) -> MagicMock:
        """Minimal stub exposing only the attributes the method touches."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = MagicMock()
        stub._SEARCH_KEYWORDS = HomeAssistantSmartMCPServer._SEARCH_KEYWORDS
        stub._SEARCH_DESCRIPTION_OVERRIDES = (
            HomeAssistantSmartMCPServer._SEARCH_DESCRIPTION_OVERRIDES
        )
        stub.settings = MagicMock(enable_tool_search=enable_tool_search)
        stub.mcp = MagicMock()
        return stub

    def test_applies_keywords_when_tool_search_disabled(self):
        """Keywords go on even when ENABLE_TOOL_SEARCH is false (#940)."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_tool_search=False)
        HomeAssistantSmartMCPServer._apply_search_keyword_enrichment(stub)

        stub.mcp.add_transform.assert_called_once()
        transform = stub.mcp.add_transform.call_args.args[0]
        assert isinstance(transform, SearchKeywordsTransform)
        assert transform._keywords == stub._SEARCH_KEYWORDS
        # Overrides are gated behind enable_tool_search; flag is off so none
        assert transform._overrides == {}

    def test_applies_keywords_and_overrides_when_tool_search_enabled(self):
        """With categorized search on, both keywords and overrides apply."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_tool_search=True)
        HomeAssistantSmartMCPServer._apply_search_keyword_enrichment(stub)

        stub.mcp.add_transform.assert_called_once()
        transform = stub.mcp.add_transform.call_args.args[0]
        assert isinstance(transform, SearchKeywordsTransform)
        assert transform._keywords == stub._SEARCH_KEYWORDS
        assert transform._overrides == stub._SEARCH_DESCRIPTION_OVERRIDES

    def test_transform_failure_is_logged_not_raised(self, caplog):
        """Enrichment failures must not break server startup."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        stub = self._make_server_stub(enable_tool_search=False)
        stub.mcp.add_transform.side_effect = RuntimeError("boom")
        with caplog.at_level("ERROR"):
            HomeAssistantSmartMCPServer._apply_search_keyword_enrichment(stub)
        assert any("SearchKeywordsTransform" in rec.message for rec in caplog.records)

    @pytest.mark.anyio
    async def test_canonical_keywords_end_to_end_for_940_tools(self):
        """The specific tools in #940 actually get enriched descriptions."""
        from ha_mcp.server import HomeAssistantSmartMCPServer

        keywords = HomeAssistantSmartMCPServer._SEARCH_KEYWORDS
        # These are the tools named in the #940 reproduction
        for tool_name in (
            "ha_config_set_automation",
            "ha_config_set_script",
            "ha_config_set_helper",
            "ha_search",
            "ha_manage_app",
        ):
            assert tool_name in keywords, f"{tool_name} missing from _SEARCH_KEYWORDS"

        transform = SearchKeywordsTransform(keywords=keywords)
        tool = _make_tool(
            "ha_config_set_automation",
            destructive=True,
            description="Create or update a Home Assistant automation.",
        )
        enriched = (await transform.list_tools([tool]))[0]
        assert enriched.description.startswith(
            "Create or update a Home Assistant automation."
        )
        for term in ("create", "update", "modify", "edit", "new", "save"):
            assert term in enriched.description.lower()

    @pytest.mark.anyio
    async def test_energy_prefs_keywords_cover_tariff_vocabulary(self):
        """Energy-tariff queries must reach ha_manage_energy_prefs (#2322).

        The Energy Dashboard's contract prices live in ``.storage/energy``,
        never in entity state, so an agent that only knows the tool's own
        wording ("cost tariffs") searches the state machine instead. The
        boost adds the vocabulary agents actually query with, plus read
        verbs to offset the write-only "Manage ..." title.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        keywords = HomeAssistantSmartMCPServer._SEARCH_KEYWORDS
        assert "ha_manage_energy_prefs" in keywords

        transform = SearchKeywordsTransform(keywords=keywords)
        tool = _make_tool(
            "ha_manage_energy_prefs",
            destructive=True,
            description="Manage the Home Assistant Energy Dashboard preferences.",
        )
        enriched = (await transform.list_tools([tool]))[0]

        assert enriched.description.startswith(
            "Manage the Home Assistant Energy Dashboard preferences."
        )
        for term in (
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
        ):
            assert term in enriched.description.lower(), f"{term!r} missing"

    @pytest.mark.anyio
    async def test_blueprint_keywords_route_the_retired_tool_names(self):
        """Blueprint queries and the pre-#2329 names must reach ha_manage_blueprints.

        The consolidation removed ha_get_blueprint and ha_import_blueprint, so
        an agent working from the older catalog searches names that no longer
        exist. The boost carries both, plus the verbs the merged tool gained
        ("delete", "substitute") that its own description does not lead with.
        """
        from ha_mcp.server import HomeAssistantSmartMCPServer

        keywords = HomeAssistantSmartMCPServer._SEARCH_KEYWORDS
        assert "ha_manage_blueprints" in keywords

        transform = SearchKeywordsTransform(keywords=keywords)
        tool = _make_tool(
            "ha_manage_blueprints",
            destructive=True,
            description="Manage Home Assistant blueprints.",
        )
        enriched = (await transform.list_tools([tool]))[0]

        assert enriched.description.startswith("Manage Home Assistant blueprints.")
        for term in (
            "blueprint",
            "blueprints",
            "import",
            "delete",
            "substitute",
            "take-control",
            "list",
            "ha_get_blueprint",
            "ha_import_blueprint",
        ):
            assert term in enriched.description.lower(), f"{term!r} missing"
