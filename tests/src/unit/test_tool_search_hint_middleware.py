"""Unit tests for ToolSearchHintMiddleware.

Covers the stale-tool-list scenario: a client (e.g. a ChatGPT connector)
calls ha_search_tools / the ha_call_* proxies on a full-catalog connection,
where the names do not resolve.
"""

import json
from types import SimpleNamespace

import pytest

from ha_mcp._vendor.fastmcp import FastMCP
from ha_mcp._vendor.fastmcp.exceptions import NotFoundError, ToolError
from ha_mcp.tool_catalog import catalog_endpoint
from ha_mcp.tools.tool_search_hint_middleware import ToolSearchHintMiddleware
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform


def _make_mcp(*, search_transform: bool = True) -> FastMCP:
    """A server with the hint middleware, one real tool and, by default, the
    search transform as the production server installs it."""
    mcp = FastMCP("test")
    mcp.add_middleware(ToolSearchHintMiddleware())

    @mcp.tool()
    async def ha_real_tool(x: int) -> dict:
        return {"x": x}

    if search_transform:
        mcp.add_transform(CategorizedSearchTransform())
    return mcp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        "ha_search_tools",
        "ha_call_read_tool",
        "ha_call_write_tool",
        "ha_call_delete_tool",
    ],
)
@pytest.mark.usefixtures("tool_search_default")
async def test_full_catalog_url_returns_refresh_hint_when_search_is_default(name):
    """With search as the main endpoint's default, a synthetic tool called on
    ``/catalog/full`` (through the real transform) yields a structured hint
    naming the connection's catalog, not the bare NotFoundError."""
    mcp = _make_mcp()

    with catalog_endpoint("full"), pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(name, {"query": "x"})

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    msg = body["error"]["message"]
    assert "this connection serves the full catalog" in msg
    assert "/catalog/search" in msg
    # the counter-intuitive insight users most need: a server restart won't help
    assert "does not refresh" in msg.lower()
    assert "app (add-on)" in msg
    suggestions = body["error"]["suggestions"]
    assert len(suggestions) == 2
    assert any("refresh" in s.lower() for s in suggestions)
    # context fields are spread at the top level by create_error_response
    assert body["tool_name"] == name
    assert body["catalog"] == "full"
    assert "enable_tool_search" not in body


@pytest.mark.asyncio
async def test_unknown_non_synthetic_tool_still_raises_notfound():
    """A genuinely unknown tool that is NOT a synthetic name keeps the original
    NotFoundError — the middleware must not mask real typos/renames."""
    mcp = _make_mcp()

    with catalog_endpoint("full"), pytest.raises(NotFoundError):
        await mcp.call_tool("ha_definitely_not_a_tool", {})


@pytest.mark.asyncio
async def test_synthetic_name_not_masked_on_a_search_connection():
    """On a search connection, a NotFoundError for a synthetic name signals a
    real problem (the transform should have registered it) and must propagate
    unchanged rather than be mislabelled as a stale-cache hint."""
    mcp = _make_mcp(search_transform=False)  # so the name is missing

    with catalog_endpoint("search"), pytest.raises(NotFoundError):
        await mcp.call_tool("ha_search_tools", {"query": "x"})


@pytest.mark.asyncio
async def test_deep_notfound_with_other_name_not_rewritten():
    """A NotFoundError whose message names a DIFFERENT tool (e.g. one bubbling
    up from inside an executing proxy) is not a top-level miss for the called
    name, so it must propagate unchanged rather than be relabeled stale-cache —
    even when the called name is synthetic and the catalog is full."""
    mw = ToolSearchHintMiddleware()
    context = SimpleNamespace(message=SimpleNamespace(name="ha_search_tools"))

    async def call_next(_ctx):
        raise NotFoundError("Unknown tool: 'some_inner_tool'")

    with catalog_endpoint("full"), pytest.raises(NotFoundError) as exc_info:
        await mw.on_call_tool(context, call_next)
    assert "some_inner_tool" in str(exc_info.value)


@pytest.mark.asyncio
async def test_real_tool_passes_through():
    """A normal, registered tool is unaffected by the middleware."""
    mcp = _make_mcp()

    with catalog_endpoint("full"):
        result = await mcp.call_tool("ha_real_tool", {"x": 5})
    assert result.structured_content == {"x": 5}
