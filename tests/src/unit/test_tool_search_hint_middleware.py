"""Unit tests for ToolSearchHintMiddleware.

Covers the stale-tool-list scenario: a client (e.g. a ChatGPT connector)
calls ha_search_tools / the ha_call_* proxies from a cached catalog while the
live server has Tool Search off, so the names no longer resolve.
"""

import json
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError

from ha_mcp.tools.tool_search_hint_middleware import ToolSearchHintMiddleware


def _make_mcp() -> FastMCP:
    """A bare server with only the hint middleware and one real tool.

    The tool-search transform is deliberately NOT installed, so the synthetic
    names (ha_search_tools / ha_call_*) are unresolved — mirroring the live
    Tool-Search-off server that a stale client keeps calling.
    """
    mcp = FastMCP("test")
    mcp.add_middleware(ToolSearchHintMiddleware())

    @mcp.tool()
    async def ha_real_tool(x: int) -> dict:
        return {"x": x}

    return mcp


def _patch_tool_search(monkeypatch, *, enabled: bool) -> None:
    monkeypatch.setattr(
        "ha_mcp.tools.tool_search_hint_middleware.get_global_settings",
        lambda: SimpleNamespace(enable_tool_search=enabled),
    )


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
async def test_stale_synthetic_tool_returns_refresh_hint(monkeypatch, name):
    """Calling a synthetic tool-search tool while Tool Search is off yields a
    structured 'stale tool list — reconnect/refresh' ToolError, not the bare
    FastMCP NotFoundError."""
    _patch_tool_search(monkeypatch, enabled=False)
    mcp = _make_mcp()

    with pytest.raises(ToolError) as exc_info:
        await mcp.call_tool(name, {"query": "x"})

    body = json.loads(str(exc_info.value))
    assert body["success"] is False
    assert body["error"]["code"] == "RESOURCE_NOT_FOUND"
    msg = body["error"]["message"]
    assert "Tool Search" in msg
    assert ("reconnect" in msg.lower()) or ("refresh" in msg.lower())
    # the counter-intuitive insight users most need: a server restart won't help
    assert "does not refresh" in msg.lower()
    # the actionable recovery guidance is the whole point of the change
    suggestions = body["error"]["suggestions"]
    assert len(suggestions) == 2
    assert any(
        ("reconnect" in s.lower()) or ("refresh" in s.lower()) for s in suggestions
    )
    # context fields are spread at the top level by create_error_response
    assert body["tool_name"] == name
    assert body["enable_tool_search"] is False


@pytest.mark.asyncio
async def test_unknown_non_synthetic_tool_still_raises_notfound(monkeypatch):
    """A genuinely unknown tool that is NOT a synthetic name keeps the original
    NotFoundError — the middleware must not mask real typos/renames."""
    _patch_tool_search(monkeypatch, enabled=False)
    mcp = _make_mcp()

    with pytest.raises(NotFoundError):
        await mcp.call_tool("ha_definitely_not_a_tool", {})


@pytest.mark.asyncio
async def test_synthetic_name_not_masked_when_tool_search_on(monkeypatch):
    """If Tool Search is ON, a NotFoundError for a synthetic name signals a
    real problem (the transform should have registered it) and must propagate
    unchanged rather than be mislabelled as a stale-cache hint."""
    _patch_tool_search(monkeypatch, enabled=True)
    mcp = _make_mcp()  # transform not installed, so the name is missing

    with pytest.raises(NotFoundError):
        await mcp.call_tool("ha_search_tools", {"query": "x"})


@pytest.mark.asyncio
async def test_deep_notfound_with_other_name_not_rewritten(monkeypatch):
    """A NotFoundError whose message names a DIFFERENT tool (e.g. one bubbling
    up from inside an executing proxy) is not a top-level miss for the called
    name, so it must propagate unchanged rather than be relabeled stale-cache —
    even when the called name is synthetic and Tool Search is off."""
    _patch_tool_search(monkeypatch, enabled=False)
    mw = ToolSearchHintMiddleware()
    context = SimpleNamespace(message=SimpleNamespace(name="ha_search_tools"))

    async def call_next(_ctx):
        raise NotFoundError("Unknown tool: 'some_inner_tool'")

    with pytest.raises(NotFoundError) as exc_info:
        await mw.on_call_tool(context, call_next)
    assert "some_inner_tool" in str(exc_info.value)


@pytest.mark.asyncio
async def test_real_tool_passes_through(monkeypatch):
    """A normal, registered tool is unaffected by the middleware."""
    _patch_tool_search(monkeypatch, enabled=False)
    mcp = _make_mcp()

    result = await mcp.call_tool("ha_real_tool", {"x": 5})
    assert result.structured_content == {"x": 5}
