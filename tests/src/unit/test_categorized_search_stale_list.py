"""A retired tool name called through the search proxies.

Kept apart from test_categorized_search.py, which the module-size ratchet
does not let grow.
"""

from __future__ import annotations

import pytest

from ha_mcp._vendor.fastmcp.tools.base import ToolResult
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

from .test_categorized_search import _make_ctx, _make_tool, _prepopulate_cache
from .test_categorized_search import search_connection as search_connection


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
    _prepopulate_cache(
        transform,
        [
            _make_tool("ha_manage_app", destructive=True),
            _make_tool("ha_get_state", read_only=True),
        ],
    )
    ctx = _make_ctx(call_tool_return=ToolResult(structured_content={"success": True}))
    proxy = transform._make_categorized_proxy(
        proxy_name="ha_call_write_tool",
        category="write",
        description="Test write proxy",
    )

    result = await proxy.fn(name, arguments, ctx)

    (warning,) = result.structured_content["warnings"]
    assert "`ha_manage_addon`" in warning and "`ha_manage_app`" in warning
