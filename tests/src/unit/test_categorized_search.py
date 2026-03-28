"""Unit tests for CategorizedSearchTransform.

Tests the categorization logic, transform_tools output, get_tool resolution,
proxy category validation, dispatch execution, and SearchKeywordsTransform.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ha_mcp.transforms.categorized_search import (
    DEFAULT_PINNED_TOOLS,
    CategorizedSearchTransform,
    SearchKeywordsTransform,
    _categorize_tool,
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
        tool = _make_tool("ha_config_remove_area_or_floor", destructive=True, idempotent=True)
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
        tools = [_make_tool("ha_config_set_automation", destructive=True, description="Set")]
        results = await transform._render_results(tools)
        assert "ha_call_write_tool" in results[0]["execute_via"]
        assert "ha_config_set_automation" in results[0]["execute_via"]

    @pytest.mark.anyio
    async def test_delete_tool_execute_via(self, transform):
        tools = [_make_tool("ha_config_remove_area_or_floor", destructive=True, description="Remove")]
        results = await transform._render_results(tools)
        assert "ha_call_delete_tool" in results[0]["execute_via"]
        assert "ha_config_remove_area_or_floor" in results[0]["execute_via"]

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
            _make_tool("ha_config_set_automation", destructive=True, description="Set auto"),
            _make_tool("ha_config_remove_area_or_floor", destructive=True, description="Remove area"),
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
        assert "ha_config_remove_area_or_floor" not in names

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
        assert "ha_restart" in DEFAULT_PINNED_TOOLS
        assert "ha_get_overview" in DEFAULT_PINNED_TOOLS
        assert "ha_backup_create" in DEFAULT_PINNED_TOOLS
        assert "ha_backup_restore" in DEFAULT_PINNED_TOOLS
        assert "ha_report_issue" in DEFAULT_PINNED_TOOLS
        assert "ha_reload_core" in DEFAULT_PINNED_TOOLS

    def test_is_immutable_tuple(self):
        assert isinstance(DEFAULT_PINNED_TOOLS, tuple)


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
        _prepopulate_cache(t, [
            _make_tool("ha_get_state", read_only=True),
            _make_tool("ha_search_entities", read_only=True),
            _make_tool("ha_config_set_automation", destructive=True),
            _make_tool("ha_call_service", destructive=True),
            _make_tool("ha_config_remove_area_or_floor", destructive=True),
        ])
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
        result = await fn("ha_config_remove_area_or_floor", {"area_id": "garage"}, ctx)
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
            await fn("ha_config_remove_area_or_floor", {}, ctx)
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
        _prepopulate_cache(t, [
            _make_tool("ha_get_state", read_only=True),
            _make_tool("ha_config_set_automation", destructive=True),
            _make_tool("ha_config_remove_area_or_floor", destructive=True),
        ])
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
            _make_tool("ha_config_remove_area_or_floor", destructive=True),
        ]
        with patch.object(transform, "get_tool_catalog", new_callable=AsyncMock, return_value=tools):
            await transform._rebuild_category_cache(None)

        assert "ha_get_state" in transform._read_tools
        assert "ha_list_areas" in transform._read_tools
        assert "ha_config_set_automation" in transform._write_tools
        assert "ha_config_remove_area_or_floor" in transform._delete_tools

    @pytest.mark.anyio
    async def test_cache_updates_on_catalog_change(self):
        """Cache rebuilds when catalog hash changes."""
        transform = CategorizedSearchTransform(max_results=5)
        tools_v1 = [_make_tool("ha_get_state", read_only=True)]
        tools_v2 = [
            _make_tool("ha_get_state", read_only=True),
            _make_tool("ha_new_write", destructive=True),
        ]
        with patch.object(transform, "get_tool_catalog", new_callable=AsyncMock, return_value=tools_v1):
            await transform._rebuild_category_cache(None)
        assert "ha_new_write" not in transform._write_tools

        with patch.object(transform, "get_tool_catalog", new_callable=AsyncMock, return_value=tools_v2):
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
            keywords={"ha_search_entities": "find lookup discover"}
        )
        tool = _make_tool("ha_search_entities", read_only=True, description="Search entities.")
        result = await transform.list_tools([tool])
        assert len(result) == 1
        assert result[0].description.startswith("Search entities.")
        assert "find lookup discover" in result[0].description

    @pytest.mark.anyio
    async def test_overrides_replace_description(self):
        """Overrides completely replace the description."""
        transform = SearchKeywordsTransform(
            overrides={"ha_deep_search": "Narrowed description."}
        )
        tool = _make_tool("ha_deep_search", read_only=True, description="Original broad description.")
        result = await transform.list_tools([tool])
        assert result[0].description == "Narrowed description."

    @pytest.mark.anyio
    async def test_override_takes_priority_over_keywords(self):
        """When both override and keywords exist, override wins."""
        transform = SearchKeywordsTransform(
            keywords={"ha_deep_search": "extra keywords"},
            overrides={"ha_deep_search": "Override wins."},
        )
        tool = _make_tool("ha_deep_search", read_only=True, description="Original.")
        result = await transform.list_tools([tool])
        assert result[0].description == "Override wins."
        assert "extra keywords" not in result[0].description

    @pytest.mark.anyio
    async def test_no_match_leaves_description_unchanged(self):
        """Tools not in keywords or overrides are unchanged."""
        transform = SearchKeywordsTransform(
            keywords={"ha_other_tool": "some keywords"}
        )
        tool = _make_tool("ha_get_state", read_only=True, description="Get state.")
        result = await transform.list_tools([tool])
        assert result[0].description == "Get state."

    @pytest.mark.anyio
    async def test_get_tool_enriches(self):
        """get_tool also applies enrichment."""
        transform = SearchKeywordsTransform(
            keywords={"ha_get_state": "status check"}
        )
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
