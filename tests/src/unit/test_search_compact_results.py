"""Compact ``ha_search_tools`` hits and the ``tools=`` second hop (#2633).

A keyword hit carries the description's first paragraph and a one-line
``params`` summary instead of the full docstring and input schema, so a
page of results fits a small context window. The full definition of a
chosen tool comes back from ``ha_search_tools(tools=[...])``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Literal

import pytest

import ha_mcp.server as server_module
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.exceptions import ToolError
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.read_only import ReadOnlyToolsTransform
from ha_mcp.transforms.categorized_search import CategorizedSearchTransform
from ha_mcp.transforms.compact_params import compact_params

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


async def _write(config: dict[str, Any]) -> str:
    return "ok"


_TYPED = Tool.from_function(
    fn=_typed,
    name="ha_typed_tool",
    description="typed helper writer",
    annotations=ToolAnnotations(read_only_hint=True),
)
_WRITE = Tool.from_function(
    fn=_write,
    name="ha_config_set_thing",
    description="set a thing",
    annotations=ToolAnnotations(destructive_hint=True),
)
_PINNED = _tool("ha_get_state", "get entity state")
_ENTITY_DESCRIPTION = (
    "Get entity details, with the\nsummary line wrapped.\n\n"
    "Guidance the agent needs only once it calls the tool.\n\n"
    "entity details attributes registry"
)


async def _call(arguments: dict[str, Any]) -> list[dict[str, Any]]:
    mcp = FastMCP("compact-search")
    for tool in (_TYPED, _WRITE, _PINNED, _tool("ha_get_entity", _ENTITY_DESCRIPTION)):
        mcp.add_tool(tool)
    mcp.add_transform(ReadOnlyToolsTransform())
    mcp.add_transform(CategorizedSearchTransform(always_visible=["ha_get_state"]))
    async with Client(mcp) as client:
        result = await client.call_tool("ha_search_tools", arguments)
    return list(result.data)


async def _error(arguments: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(ToolError) as exc_info:
        await _call(arguments)
    return json.loads(str(exc_info.value))


async def _entity_hit() -> dict[str, Any]:
    hits = await _call({"query": "entity details"})
    return next(h for h in hits if h["name"] == "ha_get_entity")


async def _typed_params() -> str:
    hits = await _call({"query": "typed helper writer"})
    return next(h for h in hits if h["name"] == "ha_typed_tool")["params"]


@pytest.fixture
def read_only_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ha_mcp.read_only.get_global_settings",
        lambda: SimpleNamespace(read_only_mode=True),
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fragment",
    [
        # Enum values inline, so the model can choose between tools from
        # the hit alone.
        "helper_type (input_boolean|counter, required)",
        "name (string, required)",
        # A nullable enum keeps its values and marks the null branch.
        "action (create|update?)",
        "labels (string[]?)",
        # A literal next to a plain type keeps both branches.
        "height (integer|auto)",
    ],
)
async def test_params_name_each_parameter_with_its_type(fragment: str) -> None:
    assert fragment in (await _typed_params()).split("; ")


@pytest.mark.parametrize(
    ("properties", "required", "params"),
    [
        (
            {"area": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
            [],
            "area (string?)",
        ),
        (
            {"mode": {"anyOf": [{"enum": ["single", "queued"]}, {"type": "null"}]}},
            [],
            "mode (single|queued?)",
        ),
        # Draft 2020-12 nullability as ``type: [..., "null"]`` is a valid form
        # an external tool can carry; it must not collapse to ``any``.
        (
            {
                "limit": {"type": ["integer", "null"]},
                "ids": {"type": ["string", "array"], "items": {"type": "string"}},
            },
            [],
            "limit (integer?); ids (string|string[])",
        ),
        (
            {"kinds": {"type": "array", "items": {"enum": ["a", "b"]}}},
            [],
            "kinds (a|b[])",
        ),
        (
            {"config": {"$ref": "#/$defs/Config"}},
            ["config"],
            "config (object, required)",
        ),
        # Non-string literals are JSON, the spelling the model will send.
        ({"flag": {"const": True}, "n": {"enum": [1, 2]}}, [], "flag (true); n (1|2)"),
        ({}, [], "none"),
    ],
    ids=[
        "nullable",
        "enum-branch",
        "list-valued-type",
        "array-of-enum",
        "ref",
        "json-literal",
        "none",
    ],
)
def test_compact_params_renders_each_schema_form(
    properties: dict[str, Any], required: list[str], params: str
) -> None:
    assert compact_params({"properties": properties, "required": required}) == params


@pytest.mark.anyio
async def test_tool_without_parameters_says_none() -> None:
    """An empty string would read as "params unknown", not "no params"."""
    assert (await _entity_hit())["params"] == "none"


@pytest.mark.anyio
async def test_hit_description_is_the_first_paragraph_on_one_line() -> None:
    """The summary line is what the model needs to pick a tool; the
    guidance paragraphs and the BM25 keyword list come with ``tools=``."""
    assert (await _entity_hit())["description"] == (
        "Get entity details, with the summary line wrapped."
    )
    [entry] = await _call({"tools": ["ha_get_entity"]})
    assert entry["description"] == _ENTITY_DESCRIPTION


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
async def test_unknown_name_alone_is_an_error() -> None:
    """A single hallucinated name is a failed call, not a successful page
    with one failed entry."""
    error = await _error({"tools": ["ha_no_such_tool"]})
    assert error["name"] == "ha_no_such_tool"
    assert error["success"] is False
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "search" in error["error"]["suggestion"].lower()


@pytest.mark.anyio
async def test_only_unknown_names_is_an_error_listing_each() -> None:
    error = await _error({"tools": ["ha_no_such_tool", "ha_nor_this"]})
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert [e["name"] for e in error["results"]] == ["ha_no_such_tool", "ha_nor_this"]


@pytest.mark.anyio
async def test_unknown_name_next_to_a_known_one_is_an_error_entry() -> None:
    missing, found = await _call({"tools": ["ha_no_such_tool", "ha_typed_tool"]})
    assert missing["success"] is False
    assert missing["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert found["inputSchema"] == _TYPED.parameters


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
    error = await _error({})
    assert error["error"]["code"] == "VALIDATION_INVALID_PARAMETER"


@pytest.mark.anyio
@pytest.mark.usefixtures("read_only_on")
async def test_read_only_mode_explains_a_hidden_write_tool() -> None:
    """The proxies answer a hidden write tool with READ_ONLY_MODE; a
    'not found' here would send the model searching for a capability it
    then concludes does not exist."""
    error = await _error({"tools": ["ha_config_set_thing"]})
    assert error["error"]["code"] == "READ_ONLY_MODE"
    assert error["read_only_mode"] is True
    assert "inputSchema" not in error

    found, hidden = await _call({"tools": ["ha_typed_tool", "ha_config_set_thing"]})
    assert found["inputSchema"] == _TYPED.parameters
    assert hidden["error"]["code"] == "READ_ONLY_MODE"


@pytest.mark.anyio
@pytest.mark.usefixtures("read_only_on")
async def test_read_only_mode_still_says_not_found_for_a_made_up_name() -> None:
    error = await _error({"tools": ["ha_no_such_tool"]})
    assert error["error"]["code"] == "RESOURCE_NOT_FOUND"


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
async def test_real_hit_carries_the_summary_not_the_docstring(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer,
) -> None:
    """On the real catalog the description was 79% of a page; a hit now
    carries the first paragraph, and the full entry the whole docstring
    with its appended search keywords."""
    hits, full = await _hits_and_full(toolsearch_server, "create helper")
    by_name = {entry["name"]: entry for entry in full}
    for hit in hits:
        assert "\n" not in hit["description"], hit["name"]
        assert by_name[hit["name"]]["description"].startswith(
            hit["description"].split(" ")[0]
        ), hit["name"]
    helper = next(h for h in hits if h["name"] == "ha_config_set_helper")
    assert "utility_meter" not in helper["description"]
    assert "utility_meter" in by_name["ha_config_set_helper"]["description"]


@pytest.mark.anyio
async def test_full_definition_hop_matches_the_compact_hit(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer,
) -> None:
    """On the real catalog, naming a hit returns the schema the compact
    entry omitted, under the same name and proxy hint — including the
    manage tool whose hint names two proxies."""
    hits, full = await _hits_and_full(toolsearch_server, "energy dashboard preferences")
    by_name = {entry["name"]: entry for entry in full}
    assert "ha_manage_energy_prefs" in by_name
    assert "; " in by_name["ha_manage_energy_prefs"]["execute_via"]
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
