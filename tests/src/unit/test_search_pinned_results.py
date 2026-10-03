"""``ha_search_tools`` and pinned tools (issue #2576).

A pinned tool used to be left out of the search index, so a model that
searched for it was told nothing matched; a small model reads that as
"no such capability". Pinned hits now come back as name-only stubs that
point at the tool list, without taking a result slot from hidden tools.
"""

from __future__ import annotations

from typing import Any

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform


def _tool(name: str, description: str) -> Tool:
    async def noop() -> str:
        return "ok"

    return Tool.from_function(
        fn=noop,
        name=name,
        description=description,
        annotations=ToolAnnotations(read_only_hint=True),
    )


async def _search(
    tools: list[Tool], pinned: list[str], query: str, max_results: int = 5
) -> list[dict[str, Any]]:
    mcp = FastMCP("pinned-search")
    for tool in tools:
        mcp.add_tool(tool)
    mcp.add_transform(
        CategorizedSearchTransform(max_results=max_results, always_visible=pinned)
    )
    async with Client(mcp) as client:
        result = await client.call_tool("ha_search_tools", {"query": query})
    return list(result.data)


@pytest.mark.anyio
async def test_pinned_tool_that_matches_is_returned_as_a_stub_without_its_schema():
    """Searching for a pinned tool must not answer "nothing matched"; the
    stub repeats no schema because the client already lists the tool."""
    results = await _search(
        [
            _tool("ha_get_state", "get entity state"),
            _tool("ha_config_set_scene", "create scene"),
        ],
        pinned=["ha_get_state"],
        query="entity state",
    )

    by_name = {entry["name"]: entry for entry in results}
    stub = by_name["ha_get_state"]
    assert stub["pinned"] is True
    assert "inputSchema" not in stub
    assert "description" not in stub
    assert "ha_get_state" in stub["execute_via"]
    assert "directly" in stub["execute_via"]


@pytest.mark.anyio
async def test_hidden_hit_keeps_its_full_definition_next_to_a_stub():
    results = await _search(
        [
            _tool("ha_get_state", "get entity state"),
            _tool("ha_get_entity", "get entity details"),
        ],
        pinned=["ha_get_state"],
        query="get entity",
    )

    by_name = {entry["name"]: entry for entry in results}
    assert "inputSchema" in by_name["ha_get_entity"]
    assert "pinned" not in by_name["ha_get_entity"]
    assert "ha_call_read_tool" in by_name["ha_get_entity"]["execute_via"]


@pytest.mark.anyio
async def test_pinned_stub_does_not_take_a_result_slot_from_hidden_tools():
    """``max_results`` counts hidden tools only; a stub rides along."""
    results = await _search(
        [
            _tool("pinned_tool", "alpha beta alpha beta"),
            _tool("hidden_one", "alpha beta"),
            _tool("hidden_two", "alpha beta"),
            _tool("hidden_three", "alpha beta"),
        ],
        pinned=["pinned_tool"],
        query="alpha beta",
        max_results=2,
    )

    names = [entry["name"] for entry in results]
    assert "pinned_tool" in names
    assert len([n for n in names if n.startswith("hidden_")]) == 2


@pytest.mark.anyio
async def test_pinned_tool_ranked_below_the_page_adds_no_stub():
    """A weak match on a pinned tool must not stub every page."""
    results = await _search(
        [
            _tool("pinned_tool", "beta only"),
            _tool("hidden_one", "alpha beta"),
            _tool("hidden_two", "alpha beta"),
        ],
        pinned=["pinned_tool"],
        query="alpha beta",
        max_results=2,
    )

    assert [entry["name"] for entry in results] == ["hidden_one", "hidden_two"]


@pytest.mark.anyio
async def test_unmatched_query_returns_nothing_even_with_pinned_tools():
    results = await _search(
        [_tool("ha_get_state", "get entity state")],
        pinned=["ha_get_state"],
        query="zzzznothing",
    )

    assert results == []


@pytest.fixture
def toolsearch_server(monkeypatch: pytest.MonkeyPatch):
    """The real catalog behind the search transform, no Home Assistant."""
    monkeypatch.setenv("HOMEASSISTANT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOMEASSISTANT_TOKEN", "unused")
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    from ha_mcp.config import _reset_global_settings
    from ha_mcp.server import HomeAssistantSmartMCPServer

    _reset_global_settings()
    try:
        yield HomeAssistantSmartMCPServer()
    finally:
        _reset_global_settings()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "query",
    [
        "which lights are on",
        "get light states",
        "light state",
        "get light status",
    ],
)
async def test_entity_state_queries_rank_ha_get_state_first(
    toolsearch_server, query: str
):
    """The queries from #2576 must lead with ``ha_get_state``. BM25 has no
    stemming, so without its keyword boost "lights" matched the group
    tools and not the state tool."""
    async with Client(toolsearch_server.mcp) as client:
        result = await client.call_tool("ha_search_tools", {"query": query})

    hidden = [entry["name"] for entry in result.data if not entry.get("pinned")]
    assert hidden[0] == "ha_get_state", hidden
