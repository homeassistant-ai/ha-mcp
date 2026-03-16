"""Unit tests for CategorizedSearchTransform.

Tests the categorization logic, transform_tools output, get_tool resolution,
and proxy category validation.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ha_mcp.transforms.categorized_search import (
    DEFAULT_PINNED_TOOLS,
    CategorizedSearchTransform,
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
        tool = _make_tool("ha_config_remove_area", destructive=True, idempotent=True)
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
        tools = [_make_tool("ha_config_remove_area", destructive=True, description="Remove")]
        results = await transform._render_results(tools)
        assert "ha_call_delete_tool" in results[0]["execute_via"]
        assert "ha_config_remove_area" in results[0]["execute_via"]

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
            _make_tool("ha_config_remove_area", destructive=True, description="Remove area"),
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
        assert "ha_config_remove_area" not in names

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
