"""Unit tests for the tool-search mode's ``ha_search_tools`` (#2633).

A keyword search returns compact hits so a context-limited agent can scan
many tools cheaply; ``tools=[name]`` is the second hop that returns one
tool's full description and input schema before the agent executes it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ._embedded_stubs import install

install()

import ha_mcp.server as server_module  # noqa: E402
from custom_components.ha_mcp_tools import llm_api  # noqa: E402
from custom_components.ha_mcp_tools.const import EXPOSURE_TOOL_SEARCH  # noqa: E402
from custom_components.ha_mcp_tools.llm_api_search import (  # noqa: E402
    HaMcpSearchTool,
    compact_params,
)
from ha_mcp.transforms.categorized_search import _compact_params  # noqa: E402

from ._llm_api_helpers import (  # noqa: E402
    fake_session,
    make_api,
    make_hass,
    tool_entry,
)
from .test_llm_tool_metadata import _CoreToolResult  # noqa: E402
from .test_search_pinned_results import (  # noqa: E402
    toolsearch_server as toolsearch_server,
)

_FULL_SCHEMA = {
    "type": "object",
    "properties": {
        "config": {
            "type": "object",
            "description": "Automation config",
            "properties": {"alias": {"type": "string", "default": "New"}},
        }
    },
    "required": ["config"],
}
_DESCRIPTION = (
    "Create or update an automation,\nwrapped onto two lines.\n\n"
    "Guidance the agent needs once it calls the tool."
)


def _tool(name: str, schema: dict[str, Any], *, exposed: bool = True) -> Any:
    entry = tool_entry(name, exposed=exposed, description=_DESCRIPTION)
    entry.inputSchema = schema
    return entry


def _catalog() -> list[SimpleNamespace]:
    return [
        _tool("ha_config_set_automation", _FULL_SCHEMA),
        tool_entry("ha_get_state"),
        tool_entry("ha_search", pinned=True),
        tool_entry("ha_restart", exposed=False),
    ]


async def _instance(monkeypatch: pytest.MonkeyPatch, tools: list[Any]) -> Any:
    fake_session(monkeypatch, tools=tools)
    return await make_api(
        make_hass(), mode=EXPOSURE_TOOL_SEARCH
    ).async_get_api_instance(llm_api.llm.LLMContext())


async def _search(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any], tools: list[Any]
) -> Any:
    monkeypatch.setattr(llm_api.llm, "ToolResult", _CoreToolResult, raising=False)
    instance = await _instance(monkeypatch, tools)
    search = next(t for t in instance.tools if t.name == "ha_search_tools")
    return await search.async_call(
        make_hass(),
        llm_api.llm.ToolInput("ha_search_tools", args),
        llm_api.llm.LLMContext(),
    )


@pytest.mark.parametrize(
    ("properties", "required", "params"),
    [
        (
            {"helper_type": {"type": "string", "enum": ["input_boolean", "timer"]}},
            ["helper_type"],
            "helper_type (input_boolean|timer, required)",
        ),
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
        (
            {"entity_ids": {"type": "array", "items": {"type": "string"}}},
            [],
            "entity_ids (string[])",
        ),
        ({"config": {"$ref": "#/$defs/Config"}}, [], "config (object)"),
        (
            {"height": {"anyOf": [{"type": "integer"}, {"const": "auto"}]}},
            [],
            "height (integer|auto)",
        ),
        ({"limit": {"type": ["integer", "null"]}}, [], "limit (integer?)"),
        # Non-string literals are JSON, the spelling the model will send.
        ({"flag": {"const": True}}, [], "flag (true)"),
        (
            {"name": {"type": "string"}, "limit": {"type": "integer"}},
            [],
            "name (string); limit (integer)",
        ),
        ({}, [], "none"),
    ],
    ids=[
        "enum-required",
        "nullable",
        "enum-branch",
        "array",
        "ref",
        "literal-next-to-type",
        "list-valued-type",
        "json-literal",
        "two",
        "none",
    ],
)
async def test_search_hit_tells_the_agent_each_param_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    properties: dict[str, Any],
    required: list[str],
    params: str,
) -> None:
    schema = {"type": "object", "properties": properties, "required": required}

    result = await _search(
        monkeypatch, {"query": "automation"}, [_tool("ha_widget", schema)]
    )

    assert [hit["params"] for hit in result.data["results"]] == [params]


async def test_component_renders_params_exactly_as_the_server_does(
    toolsearch_server: server_module.HomeAssistantSmartMCPServer,
) -> None:
    """The component cannot import the server's renderer, so it carries a
    copy; the two must agree on every schema in the real catalog."""
    tools = await toolsearch_server.mcp.local_provider._list_tools()
    assert tools
    for tool in tools:
        assert compact_params(tool.parameters) == _compact_params(tool.parameters), (
            tool.name
        )


def test_tools_parameter_is_advertised_as_optional_names() -> None:
    """The model only learns about the second hop from the tool's schema."""
    assert HaMcpSearchTool.parameters({"tools": ["ha_get_state"]}) == {
        "tools": ["ha_get_state"]
    }
    assert HaMcpSearchTool.parameters({"query": "lights"}) == {"query": "lights"}


async def test_hit_description_is_the_first_paragraph_on_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _search(monkeypatch, {"query": "automation"}, _catalog())

    [hit] = result.data["results"]
    assert hit["description"] == (
        "Create or update an automation, wrapped onto two lines."
    )


async def test_tools_hop_returns_the_full_schema_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _search(
        monkeypatch, {"tools": ["ha_config_set_automation"]}, _catalog()
    )

    [entry] = result.data["results"]
    assert entry["input_schema"] == _FULL_SCHEMA
    assert entry["description"] == _DESCRIPTION


async def test_tools_hop_accepts_a_bare_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Core passes tool arguments through unvalidated; a small model's
    ``tools="name"`` must not be walked character by character."""
    result = await _search(
        monkeypatch, {"tools": "ha_config_set_automation"}, _catalog()
    )

    assert [e["name"] for e in result.data["results"]] == ["ha_config_set_automation"]
    assert result.error is False


@pytest.mark.parametrize(
    "tools", [[{"name": "ha_get_state"}], {"name": "ha_get_state"}, 3]
)
async def test_tools_hop_rejects_anything_but_names(
    monkeypatch: pytest.MonkeyPatch, tools: Any
) -> None:
    result = await _search(monkeypatch, {"tools": tools}, _catalog())

    assert result.error is True
    assert "list of tool names" in result.data["error"]


async def test_tools_hop_returns_the_stub_for_a_pinned_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned tool is already in the agent's tool list with its schema
    (#2576), on this path as on the server."""
    result = await _search(monkeypatch, {"tools": ["ha_search"]}, _catalog())

    [entry] = result.data["results"]
    assert entry["pinned"] is True
    assert "input_schema" not in entry
    assert "ha_search" in entry["hint"]


async def test_keyword_hit_on_a_pinned_tool_is_the_stub(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _search(monkeypatch, {"query": "ha_search"}, _catalog())

    [entry] = result.data["results"]
    assert entry == {
        "name": "ha_search",
        "pinned": True,
        "hint": "ha_search is already in your tool list — call it directly.",
    }


async def test_tools_hop_answers_in_the_order_the_agent_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = ["ha_get_state", "ha_config_set_automation"]

    result = await _search(monkeypatch, {"tools": asked}, _catalog())

    assert [entry["name"] for entry in result.data["results"]] == asked


async def test_tools_hop_does_not_mix_in_keyword_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _search(
        monkeypatch, {"query": "automation", "tools": ["ha_get_state"]}, _catalog()
    )

    assert [entry["name"] for entry in result.data["results"]] == ["ha_get_state"]


async def test_tools_hop_does_not_reveal_that_a_hidden_tool_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A distinct answer for a hidden name would leak its existence, the same
    # rule ha_call_tool keeps.
    result = await _search(
        monkeypatch, {"tools": ["ha_restart", "ha_totally_made_up"]}, _catalog()
    )

    hidden, missing = result.data["results"]
    assert "input_schema" not in hidden
    assert "suggestion" in hidden

    def blank(entry: dict[str, str], name: str) -> dict[str, str]:
        return {k: v.replace(name, "<n>") for k, v in entry.items()}

    assert blank(hidden, "ha_restart") == blank(missing, "ha_totally_made_up")


async def test_tools_hop_with_no_known_name_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ha_call_tool`` answers an unknown name with an error; a lookup that
    resolved nothing must not read as a successful schema fetch."""
    nothing = await _search(monkeypatch, {"tools": ["ha_totally_made_up"]}, _catalog())
    mixed = await _search(
        monkeypatch, {"tools": ["ha_totally_made_up", "ha_get_state"]}, _catalog()
    )

    assert nothing.error is True
    assert mixed.error is False


@pytest.mark.parametrize("args", [{}, {"query": "  "}], ids=["empty", "blank"])
async def test_search_without_query_or_tools_is_an_error(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> None:
    result = await _search(monkeypatch, args, _catalog())

    assert result.error is True
    assert "error" in result.data


async def test_prompt_tells_the_agent_to_fetch_the_schema_before_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = await _instance(monkeypatch, _catalog())

    assert "ha_search_tools(tools=" in instance.api_prompt
