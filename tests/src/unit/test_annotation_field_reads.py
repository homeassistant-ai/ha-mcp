"""Annotation reads against real FastMCP tools, not stand-ins with guessed names."""

from types import SimpleNamespace

import pytest

from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.policy.handlers import _is_write_or_destructive
from ha_mcp.read_only import is_read_safe
from ha_mcp.settings_ui._tools_meta import _get_tool_metadata


def _noop() -> None:
    return None


def _tool(name: str, **annotations: bool) -> Tool:
    return Tool.from_function(
        _noop, name=name, annotations=ToolAnnotations(**annotations)
    )


def test_read_only_tool():
    tool = _tool("ha_read", read_only_hint=True)
    assert is_read_safe(tool) is True
    assert _is_write_or_destructive(tool) is False


def test_destructive_tool():
    tool = _tool("ha_delete", destructive_hint=True)
    assert is_read_safe(tool) is False
    assert _is_write_or_destructive(tool) is True


@pytest.mark.asyncio
async def test_settings_ui_capability_badges_follow_the_annotations():
    tools = [
        _tool("ha_read", read_only_hint=True),
        _tool("ha_delete_x", destructive_hint=True),
    ]

    async def _list_tools():
        return tools

    server = SimpleNamespace(
        mcp=SimpleNamespace(local_provider=SimpleNamespace(_list_tools=_list_tools))
    )
    rows = {row["name"]: row for row in await _get_tool_metadata(server)}  # type: ignore[arg-type]
    assert rows["ha_read"]["category"] == "read"
    assert rows["ha_delete_x"]["category"] == "delete"
