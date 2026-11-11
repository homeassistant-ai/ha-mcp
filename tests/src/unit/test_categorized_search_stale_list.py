"""A retired tool name called through the search proxies.

Kept apart from test_categorized_search.py, which the module-size ratchet
does not let grow.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.fastmcp.tools.base import ToolResult
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

from ._search_mode_connection import search_connection as search_connection


def _make_tool(
    name: str, *, read_only: bool = False, destructive: bool = False
) -> Tool:
    async def noop() -> str:
        return "ok"

    annotations = ToolAnnotations(
        read_only_hint=read_only, destructive_hint=destructive
    )
    return Tool.from_function(fn=noop, name=name, annotations=annotations)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ha_manage_addon", {"slug": "core_ssh"}),
        (
            "ha_call_write_tool",
            {"name": "ha_manage_addon", "arguments": {"slug": "core_ssh"}},
        ),
    ],
    ids=["retired_name", "double_wrapped_retired_name"],
)
async def test_proxy_call_on_a_retired_name_says_the_tool_list_is_stale(
    name: str, arguments: dict
) -> None:
    """The proxy resolves a retired name itself, so the alias middleware only
    ever sees the current one and adds no warning; a client using the search
    proxies would be served without learning its list is stale."""
    transform = CategorizedSearchTransform(max_results=5)
    transform.get_tool_catalog = AsyncMock(
        return_value=[
            _make_tool("ha_manage_app", destructive=True),
            _make_tool("ha_get_state", read_only=True),
        ]
    )
    ctx = MagicMock()
    ctx.fastmcp.call_tool = AsyncMock(
        return_value=ToolResult(structured_content={"success": True})
    )
    proxy = await transform.get_tool("ha_call_write_tool", AsyncMock())
    assert proxy is not None

    result = await proxy.fn(name, arguments, ctx)

    (warning,) = result.structured_content["warnings"]
    assert "`ha_manage_addon`" in warning and "`ha_manage_app`" in warning
