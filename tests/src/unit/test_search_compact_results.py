"""Compact ``ha_search_tools`` hits and the ``tools=`` second hop (#2633).

A keyword hit carries a one-line ``params`` summary instead of the full
input schema, so a page of results fits a small context window. The full
definition of a chosen tool comes back from ``ha_search_tools(tools=[...])``.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest

import ha_mcp.server as server_module
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform

from .test_search_pinned_results import _tool
from .test_search_pinned_results import toolsearch_server as toolsearch_server


async def _typed(
    helper_type: Literal["input_boolean", "counter"],
    name: str,
    action: Literal["create", "update"] | None = None,
    labels: list[str] | None = None,
    height: int | Literal["auto"] = 800,
) -> str:
    return "ok"


_TYPED = Tool.from_function(
    fn=_typed,
    name="ha_typed_tool",
    description="typed helper writer",
    annotations=ToolAnnotations(read_only_hint=True),
)
_PINNED = _tool("ha_get_state", "get entity state")


async def _call(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    mcp = FastMCP("compact-search")
    for tool in (_TYPED, _PINNED, _tool("ha_get_entity", "get entity details")):
        mcp.add_tool(tool)
    mcp.add_transform(CategorizedSearchTransform(always_visible=["ha_get_state"]))
    async with Client(mcp) as client:
        result = await client.call_tool("ha_search_tools", arguments)
    return list(result.data)


async def _typed_params() -> str:
    hits = await _call({"query": "typed helper writer"})
    return next(h for h in hits if h["name"] == "ha_typed_tool")["params"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fragment",
    [
        # Enum values inline, so the model needs no second hop to pick one.
        "helper_type (input_boolean|counter, required)",
        "name (string, required)",
        # A nullable enum keeps its values and marks the null branch.
        "action (create|update?)",
        "labels (string[]?)",
        # A literal next to a plain type keeps both branches (Codex, #2670).
        "height (integer|auto)",
    ],
)
async def test_params_name_each_parameter_with_its_type(fragment: str) -> None:
    assert fragment in (await _typed_params()).split("; ")


@pytest.mark.anyio
async def test_tool_without_parameters_says_none() -> None:
    """An empty string would read as "params unknown", not "no params"."""
    hits = await _call({"query": "entity details"})
    assert next(h for h in hits if h["name"] == "ha_get_entity")["params"] == "none"


@pytest.mark.anyio
async def test_named_hidden_tool_returns_its_full_definition() -> None:
    [entry] = await _call({"tools": ["ha_typed_tool"]})
    assert entry["inputSchema"] == _TYPED.parameters
    assert "ha_call_read_tool" in entry["execute_via"]


@pytest.mark.anyio
async def test_named_pinned_tool_returns_the_stub() -> None:
    """The client already holds a pinned tool's schema."""
    [entry] = await _call({"tools": ["ha_get_state"]})
    assert entry["pinned"] is True
    assert "inputSchema" not in entry


@pytest.mark.anyio
async def test_unknown_name_returns_an_error_entry() -> None:
    [entry] = await _call({"tools": ["ha_no_such_tool"]})
    assert entry["name"] == "ha_no_such_tool"
    assert "inputSchema" not in entry
    assert entry["success"] is False
    assert entry["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "search" in entry["error"]["suggestion"].lower()


@pytest.mark.anyio
async def test_named_tools_come_back_in_the_order_given() -> None:
    names = ["ha_no_such_tool", "ha_get_state", "ha_typed_tool"]
    assert [e["name"] for e in await _call({"tools": names})] == names


@pytest.mark.anyio
async def test_query_is_ignored_when_tools_are_named() -> None:
    named = await _call({"tools": ["ha_typed_tool"]})
    assert await _call({"query": "entity details", "tools": ["ha_typed_tool"]}) == (
        named
    )


@pytest.mark.anyio
async def test_call_without_query_or_tools_is_a_validation_error() -> None:
    with pytest.raises(ToolError) as exc_info:
        await _call({})
    error = json.loads(str(exc_info.value))
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


async def _hits_and_full(
    server: server_module.HomeAssistantSmartMCPServer, query: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    async with Client(server.mcp) as client:
        result = await client.call_tool("ha_search_tools", {"query": query})
        hits = [h for h in result.data if not h.get("pinned")]
        full = await client.call_tool(
            "ha_search_tools", {"tools": [h["name"] for h in hits]}
        )
    return hits, list(full.data)


@pytest.mark.anyio
async def test_compact_page_is_smaller_than_the_full_definitions(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer,
) -> None:
    """The point of #2633: a page of hits costs less context than the
    schemas it summarises, on the real catalog."""
    hits, full = await _hits_and_full(toolsearch_server, "create helper")
    assert hits
    assert len(json.dumps(hits)) < len(json.dumps(full))


@pytest.mark.anyio
async def test_full_definition_hop_matches_the_compact_hit(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer,
) -> None:
    """On the real catalog, naming a hit returns the schema the compact
    entry omitted, under the same name and proxy hint."""
    hits, full = await _hits_and_full(toolsearch_server, "energy dashboard preferences")
    by_name = {entry["name"]: entry for entry in full}
    for hit in hits:
        entry = by_name[hit["name"]]
        assert "properties" in entry["inputSchema"], hit["name"]
        assert entry["execute_via"] == hit["execute_via"], hit["name"]


@pytest.mark.anyio
async def test_real_helper_tool_lists_its_helper_types_inline(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer,
) -> None:
    hits, _full = await _hits_and_full(toolsearch_server, "create helper")
    params = next(h for h in hits if h["name"] == "ha_config_set_helper")["params"]
    helper_type = next(p for p in params.split("; ") if p.startswith("helper_type ("))
    assert "input_boolean|" in helper_type
