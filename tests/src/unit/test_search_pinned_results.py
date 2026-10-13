"""``ha_search_tools`` and pinned tools (issue #2576).

A pinned tool used to be left out of the search index, so a model that
searched for it was told nothing matched; a small model reads that as
"no such capability". Pinned hits now come back as name-only stubs that
point at the tool list, without taking a result slot from hidden tools.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import MagicMock

import pytest

import ha_mcp.server as server_module
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.config import _reset_global_settings
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
async def test_pinned_tool_that_matches_is_returned_as_a_stub_without_its_schema() -> (
    None
):
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
async def test_hidden_hit_is_compact_next_to_a_stub() -> None:
    """A hidden hit names its params and proxy without the full schema;
    the schema comes from the ``tools=`` second hop."""
    results = await _search(
        [
            _tool("ha_get_state", "get entity state"),
            _tool("ha_get_entity", "get entity details"),
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
async def test_pinned_tool_ranked_below_the_page_adds_no_stub() -> None:
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
async def test_unmatched_query_returns_nothing_even_with_pinned_tools() -> None:
    results = await _search(
        [_tool("ha_get_state", "get entity state")],
        pinned=["ha_get_state"],
        query="zzzznothing",
    )

    assert results == []


@asynccontextmanager
async def _no_lifespan(_server: Any) -> AsyncIterator[dict[str, Any]]:
    yield {}


@pytest.fixture
def toolsearch_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[server_module.HomeAssistantSmartMCPServer]:
    """The real catalog behind the search transform, no Home Assistant.

    The client double carries no credentials, so the component probe
    returns without connecting, and the lifespan is replaced so the
    admin-token check and HACS nudge never open a socket. Pins and
    disables from the developer's environment are cleared so the ranking
    assertions see the default catalog.
    """
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    monkeypatch.setenv("PINNED_TOOLS", "")
    monkeypatch.setenv("DISABLED_TOOLS", "")
    monkeypatch.delenv("HOMEASSISTANT_URL", raising=False)
    monkeypatch.delenv("HOMEASSISTANT_TOKEN", raising=False)
    monkeypatch.setattr(server_module, "server_lifespan", _no_lifespan)
    _reset_global_settings()
    try:
        yield server_module.HomeAssistantSmartMCPServer(client=MagicMock(spec=[]))
    finally:
        _reset_global_settings()


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
