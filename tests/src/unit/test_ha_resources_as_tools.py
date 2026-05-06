"""Unit tests for the HaResourcesAsTools rename adapter."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from ha_mcp.server import HaResourcesAsTools
from ha_mcp.settings_ui import TRANSFORM_GENERATED_TOOLS


def test_transform_generated_tool_names_match_class_constants():
    """The settings UI stub keys must equal _RENAMES.values() exactly so a
    future rename can't drift the two sides apart in either direction —
    a missing stub or an orphan stub both fail this assertion."""
    expected = set(HaResourcesAsTools._RENAMES.values())
    assert set(TRANSFORM_GENERATED_TOOLS) == expected, (
        f"TRANSFORM_GENERATED_TOOLS keys {set(TRANSFORM_GENERATED_TOOLS)} "
        f"must equal HaResourcesAsTools._RENAMES.values() {expected}"
    )


@pytest.fixture
def transform():
    """A real HaResourcesAsTools wired to a fresh FastMCP server."""
    from fastmcp import FastMCP

    return HaResourcesAsTools(FastMCP("test-rename"))


class TestListTools:
    @pytest.mark.asyncio
    async def test_renames_appended_pair(self, transform):
        """FastMCP's transform appends list_resources/read_resource at the
        end; both must come back with the ha_ prefix and the unprefixed
        names must not leak through."""
        result = await transform.list_tools([])
        names = [t.name for t in result]

        assert HaResourcesAsTools.LIST_TOOL_NAME in names
        assert HaResourcesAsTools.READ_TOOL_NAME in names
        assert "list_resources" not in names
        assert "read_resource" not in names

    @pytest.mark.asyncio
    async def test_warns_when_base_contract_drifts(self, monkeypatch, caplog):
        """If FastMCP ever stops appending one of the renamed tools, log a
        warning so the regression is loud at boot."""
        from fastmcp import FastMCP
        from fastmcp.server.transforms import ResourcesAsTools

        transform = HaResourcesAsTools(FastMCP("test-drift"))

        # Real base output, then drop read_resource to simulate a regression.
        real_base_list_tools = ResourcesAsTools.list_tools

        async def partial_base(self, tools):
            full = list(await real_base_list_tools(self, tools))
            return [t for t in full if t.name != "read_resource"]

        monkeypatch.setattr(ResourcesAsTools, "list_tools", partial_base)

        with caplog.at_level(logging.WARNING, logger="ha_mcp.server"):
            await transform.list_tools([])

        warnings = [
            r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING and "HaResourcesAsTools" in r.message
        ]
        assert warnings, "Expected a warning when the base contract drifts"


class TestGetTool:
    @pytest.mark.asyncio
    async def test_returns_renamed_list_tool(self, transform):
        call_next = AsyncMock()
        result = await transform.get_tool(
            HaResourcesAsTools.LIST_TOOL_NAME, call_next
        )
        assert result is not None
        assert result.name == HaResourcesAsTools.LIST_TOOL_NAME
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_renamed_read_tool(self, transform):
        call_next = AsyncMock()
        result = await transform.get_tool(
            HaResourcesAsTools.READ_TOOL_NAME, call_next
        )
        assert result is not None
        assert result.name == HaResourcesAsTools.READ_TOOL_NAME
        call_next.assert_not_called()

    @pytest.mark.asyncio
    async def test_unprefixed_names_fall_through_to_call_next(self, transform):
        """Calls for the unprefixed FastMCP name must delegate to call_next —
        the rename is one-way; the old name is not surfaced by this
        transform."""
        call_next = AsyncMock(return_value=None)
        result = await transform.get_tool("list_resources", call_next)
        call_next.assert_awaited_once_with("list_resources", version=None)
        assert result is None

    @pytest.mark.asyncio
    async def test_unrelated_names_fall_through_to_call_next(self, transform):
        call_next = AsyncMock(return_value=None)
        await transform.get_tool("ha_search_entities", call_next)
        call_next.assert_awaited_once_with("ha_search_entities", version=None)
