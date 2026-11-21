"""``ha_search_tools`` and pinned tools (issue #2576).

A pinned tool used to be left out of the search index, so a model that
searched for it was told nothing matched; a small model reads that as
"no such capability". Pinned hits now come back as name-only stubs that
point at the tool list, without taking a result slot from hidden tools.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import ha_mcp.server as server_module
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp.client.rest_client import HomeAssistantClient
from ha_mcp.config import reset_global_settings
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

from ._real_server import build_server
from ._search_noop_tools import noop_tool

# These tests exercise the search tools a search-mode connection gets.
pytestmark = pytest.mark.usefixtures("tool_search_default")


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
@pytest.mark.parametrize("reverse_catalog", [False, True])
async def test_cached_search_uses_current_tool_schema(reverse_catalog: bool) -> None:
    """A schema-only update must reach search even when ranking is cached."""
    original = noop_tool("ha_lookup", "look up an entity")
    original.parameters = {
        "type": "object",
        "properties": {"entity_id": {"type": "string"}},
    }
    other = noop_tool("ha_unrelated", "unrelated operation")
    mcp = FastMCP("schema-cache")
    mcp.add_tool(original)
    mcp.add_tool(other)
    mcp.add_transform(CategorizedSearchTransform())
    async with Client(mcp) as client:
        before = await client.call_tool(
            "ha_search_tools", {"query": "look up an entity"}
        )
        updated = original.model_copy(
            update={
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entity_id": {"type": "array", "items": {"type": "string"}}
                    },
                }
            }
        )
        mcp.local_provider.remove_tool("ha_lookup")
        if not reverse_catalog:
            mcp.local_provider.remove_tool("ha_unrelated")
            mcp.add_tool(updated)
            mcp.add_tool(other)
        else:
            mcp.add_tool(updated)
        after = await client.call_tool(
            "ha_search_tools", {"query": "look up an entity"}
        )
    assert before.data[0]["name"] == "ha_lookup"
    assert after.data[0]["name"] == "ha_lookup"
    assert after.data[0]["params"] != before.data[0]["params"]
    assert "[]" in after.data[0]["params"]


@pytest.mark.anyio
async def test_pinned_tool_that_matches_is_returned_as_a_stub_without_its_schema() -> (
    None
):
    """Searching for a pinned tool must not answer "nothing matched"; the
    stub repeats no schema because the client already lists the tool."""
    results = await _search(
        [
            noop_tool("ha_get_state", "get entity state"),
            noop_tool("ha_config_set_scene", "create scene"),
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
async def test_hidden_hit_is_compact_next_to_a_stub() -> None:
    """A hidden hit names its params and proxy without the full schema;
    the schema comes from the ``tools=`` second hop."""
    results = await _search(
        [
            noop_tool("ha_get_state", "get entity state"),
            noop_tool("ha_get_entity", "get entity details"),
        ],
        pinned=["ha_get_state"],
        query="get entity",
    )

    hit = {entry["name"]: entry for entry in results}["ha_get_entity"]
    assert "inputSchema" not in hit
    assert "pinned" not in hit
    assert hit["description"] == "get entity details"
    assert "params" in hit
    assert "ha_call_read_tool" in hit["execute_via"]


@pytest.mark.anyio
async def test_pinned_stub_does_not_take_a_result_slot_from_hidden_tools() -> None:
    """``max_results`` counts hidden tools only; a stub rides along."""
    results = await _search(
        [
            noop_tool("pinned_tool", "alpha beta alpha beta"),
            noop_tool("hidden_one", "alpha beta"),
            noop_tool("hidden_two", "alpha beta"),
            noop_tool("hidden_three", "alpha beta"),
        ],
        pinned=["pinned_tool"],
        query="alpha beta",
        max_results=2,
    )

    names = [entry["name"] for entry in results]
    assert "pinned_tool" in names
    assert len([n for n in names if n.startswith("hidden_")]) == 2


@pytest.mark.anyio
async def test_pinned_tool_ranked_below_the_page_adds_no_stub() -> None:
    """A weak match on a pinned tool must not stub every page."""
    results = await _search(
        [
            noop_tool("pinned_tool", "beta only"),
            noop_tool("hidden_one", "alpha beta"),
            noop_tool("hidden_two", "alpha beta"),
        ],
        pinned=["pinned_tool"],
        query="alpha beta",
        max_results=2,
    )

    assert [entry["name"] for entry in results] == ["hidden_one", "hidden_two"]


@pytest.mark.anyio
async def test_unmatched_query_returns_nothing_even_with_pinned_tools() -> None:
    results = await _search(
        [noop_tool("ha_get_state", "get entity state")],
        pinned=["ha_get_state"],
        query="zzzznothing",
    )

    assert results == []


@pytest.fixture
def toolsearch_server(
    monkeypatch: pytest.MonkeyPatch, core_client: HomeAssistantClient
) -> Iterator[server_module.HomeAssistantSmartMCPServer]:
    """Search the real catalog backed by Core, with fresh default pins."""
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    monkeypatch.setenv("PINNED_TOOLS", "")
    monkeypatch.setenv("DISABLED_TOOLS", "")
    try:
        yield build_server(monkeypatch, core_client)
    finally:
        reset_global_settings()


async def _hidden_hits(
    server: server_module.HomeAssistantSmartMCPServer, query: str
) -> list[str]:
    async with Client(server.mcp) as client:
        result = await client.call_tool("ha_search_tools", {"query": query})
    return [entry["name"] for entry in result.data if not entry.get("pinned")]


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
    toolsearch_server: server_module.HomeAssistantSmartMCPServer, query: str
) -> None:
    """The queries from #2576 must lead with ``ha_get_state``. BM25 has no
    stemming, so without its keyword boost "lights" matched the group
    tools and not the state tool."""
    hidden = await _hidden_hits(toolsearch_server, query)
    assert hidden[0] == "ha_get_state", hidden


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["turn off lights", "switch off all lights"])
async def test_control_queries_do_not_lead_with_the_state_reader(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer, query: str
) -> None:
    """Boosting ``ha_get_state`` with control verbs made it the top hit
    for commands that change state; a small model then reads instead of
    acting."""
    hidden = await _hidden_hits(toolsearch_server, query)
    assert hidden[0] != "ha_get_state", hidden
