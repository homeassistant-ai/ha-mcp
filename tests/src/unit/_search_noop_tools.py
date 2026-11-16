"""Catalog tools for the ``ha_search_tools`` tests: a name and a description."""

from __future__ import annotations

from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations


def noop_tool(name: str, description: str) -> Tool:
    async def noop() -> str:
        return "ok"

    return Tool.from_function(
        fn=noop,
        name=name,
        description=description,
        annotations=ToolAnnotations(read_only_hint=True),
    )
