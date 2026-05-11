"""Unit tests for LiteDocstringsTransform.

Covers the three behaviours that the toggle promises:

1. Disabled -> every tool passes through unchanged.
2. Enabled, name in mapping -> description replaced with the lite text.
3. Enabled, name NOT in mapping -> description unchanged.

Also exercises ``get_tool`` so the single-tool lookup path stays in sync
with ``list_tools`` (this matters because FastMCP can call either one).
"""

from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock

import pytest
from fastmcp.tools import Tool
from mcp.types import ToolAnnotations

from ha_mcp.transforms import LiteDocstringsTransform


def _make_tool(name: str, *, description: str = "") -> Tool:
    """Create a minimal Tool for testing."""

    async def noop() -> str:
        return "ok"

    return Tool.from_function(
        fn=noop,
        name=name,
        description=description,
        annotations=ToolAnnotations(readOnlyHint=True),
    )


_FULL_AUTOMATION = (
    "Retrieve Home Assistant automation configuration.\n\n"
    "Returns the complete configuration including triggers, conditions, "
    "actions, and mode settings. (... many more paragraphs ...)"
)
_LITE_AUTOMATION = (
    "Retrieve a Home Assistant automation. See "
    "ha_get_skill_home_assistant_best_practices for schema."
)


@pytest.fixture
def replacements() -> dict[str, str]:
    return {"ha_config_get_automation": _LITE_AUTOMATION}


@pytest.fixture
def tools() -> Sequence[Tool]:
    return [
        _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION),
        _make_tool("ha_get_state", description="Get a state."),
    ]


class TestListTools:
    @pytest.mark.asyncio
    async def test_disabled_passes_through(
        self, tools: Sequence[Tool], replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=False, replacements=replacements)

        result = list(await transform.list_tools(tools))

        assert [t.description for t in result] == [
            _FULL_AUTOMATION,
            "Get a state.",
        ]

    @pytest.mark.asyncio
    async def test_enabled_replaces_mapped_only(
        self, tools: Sequence[Tool], replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=True, replacements=replacements)

        result = list(await transform.list_tools(tools))

        descriptions = {t.name: t.description for t in result}
        assert descriptions["ha_config_get_automation"] == _LITE_AUTOMATION
        assert descriptions["ha_get_state"] == "Get a state."

    @pytest.mark.asyncio
    async def test_enabled_with_empty_mapping_is_noop(
        self, tools: Sequence[Tool]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=True, replacements={})

        result = list(await transform.list_tools(tools))

        assert [t.description for t in result] == [
            _FULL_AUTOMATION,
            "Get a state.",
        ]


class TestGetTool:
    @pytest.mark.asyncio
    async def test_get_tool_disabled_passes_through(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=False, replacements=replacements)
        original = _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION)
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_config_get_automation", call_next)

        assert result is not None
        assert result.description == _FULL_AUTOMATION

    @pytest.mark.asyncio
    async def test_get_tool_enabled_replaces_mapped(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=True, replacements=replacements)
        original = _make_tool("ha_config_get_automation", description=_FULL_AUTOMATION)
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_config_get_automation", call_next)

        assert result is not None
        assert result.description == _LITE_AUTOMATION

    @pytest.mark.asyncio
    async def test_get_tool_enabled_unmapped_passes_through(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=True, replacements=replacements)
        original = _make_tool("ha_get_state", description="Get a state.")
        call_next = AsyncMock(return_value=original)

        result = await transform.get_tool("ha_get_state", call_next)

        assert result is not None
        assert result.description == "Get a state."

    @pytest.mark.asyncio
    async def test_get_tool_missing_returns_none(
        self, replacements: dict[str, str]
    ) -> None:
        transform = LiteDocstringsTransform(enabled=True, replacements=replacements)
        call_next = AsyncMock(return_value=None)

        result = await transform.get_tool("ha_does_not_exist", call_next)

        assert result is None
