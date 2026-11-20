"""Unit tests for the tool-search mode's ``ha_search_tools`` (#2633).

A keyword search returns compact hits so a context-limited agent can scan
many tools cheaply; ``tools=[name]`` is the second hop that returns one
tool's full description and input schema before the agent executes it.
"""

from __future__ import annotations

from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from ._llm_api_server import (
    ServedTool,
    api_instance,
    call,
    payload,
    serve,
    tool_named,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

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
_PARAMS = "config (object, required)"


def _tool(
    name: str,
    schema: dict[str, Any],
    *,
    exposed: bool = True,
    params: str | None = _PARAMS,
) -> ServedTool:
    return ServedTool(
        name,
        description=_DESCRIPTION,
        input_schema=schema,
        exposed=exposed,
        params=params,
    )


def _catalog() -> list[ServedTool]:
    return [
        _tool("ha_config_set_automation", _FULL_SCHEMA),
        ServedTool("ha_get_state"),
        ServedTool("ha_search", pinned=True),
        ServedTool("ha_restart", exposed=False),
    ]


async def _search(
    hass: HomeAssistant, args: dict[str, Any], tools: list[ServedTool]
) -> Any:
    """``ha_search_tools`` answering ``args`` over the served ``tools``."""
    async with serve(tools) as server:
        instance = await api_instance(hass, server.port)
        return await call(hass, tool_named(instance, "ha_search_tools"), args)


async def test_hit_carries_the_summary_line_and_the_servers_params(
    hass: HomeAssistant,
) -> None:
    """The server renders the params line and stamps it on the tool; the
    component shows it rather than carrying a renderer of its own."""
    result = await _search(hass, {"query": "automation"}, _catalog())

    [hit] = result.data["results"]
    assert hit == {
        "name": "ha_config_set_automation",
        "description": "Create or update an automation, wrapped onto two lines.",
        "params": _PARAMS,
    }


async def test_hit_from_a_server_without_the_stamp_has_no_params(
    hass: HomeAssistant,
) -> None:
    result = await _search(
        hass,
        {"query": "automation"},
        [_tool("ha_config_set_automation", _FULL_SCHEMA, params=None)],
    )

    [hit] = result.data["results"]
    assert "params" not in hit
    assert hit["description"]


async def test_tools_hop_returns_the_full_schema_and_description(
    hass: HomeAssistant,
) -> None:
    result = await _search(hass, {"tools": ["ha_config_set_automation"]}, _catalog())

    [entry] = result.data["results"]
    assert entry["input_schema"] == _FULL_SCHEMA
    assert entry["description"] == _DESCRIPTION


async def test_tools_hop_accepts_a_bare_name(hass: HomeAssistant) -> None:
    """Core passes tool arguments through unvalidated; a small model's
    ``tools="name"`` must not be walked character by character."""
    result = await _search(hass, {"tools": "ha_config_set_automation"}, _catalog())

    assert [e["name"] for e in result.data["results"]] == ["ha_config_set_automation"]
    assert result.error is False


@pytest.mark.parametrize(
    "tools", [[{"name": "ha_get_state"}], {"name": "ha_get_state"}, 3]
)
async def test_tools_hop_rejects_anything_but_names(
    hass: HomeAssistant, tools: Any
) -> None:
    result = await _search(hass, {"tools": tools}, _catalog())

    assert result.error is True
    assert "list of tool names" in result.data["error"]


async def test_tools_hop_returns_the_stub_for_a_pinned_tool(
    hass: HomeAssistant,
) -> None:
    """A pinned tool is already in the agent's tool list with its schema
    (#2576), on this path as on the server."""
    result = await _search(hass, {"tools": ["ha_search"]}, _catalog())

    [entry] = result.data["results"]
    assert entry["pinned"] is True
    assert "input_schema" not in entry
    assert "ha_search" in entry["hint"]


async def test_pinned_tool_that_failed_to_mirror_stays_a_full_entry(
    hass: HomeAssistant,
) -> None:
    """A pinned tool whose schema does not convert is skipped from the
    agent's list; a stub saying "already in your tool list" would leave it
    unreachable, so search must hand out its schema for ha_call_tool."""
    # Core's converter refuses a property of a type JSON Schema does not have.
    unconvertible = {"type": "object", "properties": {"fails": {"type": "nope"}}}
    pinned = ServedTool("ha_search", pinned=True, input_schema=unconvertible)
    tools = [pinned, *(t for t in _catalog() if t.name != "ha_search")]

    async with serve(tools) as server:
        instance = await api_instance(hass, server.port)
        listed = [t.name for t in instance.tools]
        search = tool_named(instance, "ha_search_tools")
        by_query = await call(hass, search, {"query": "ha_search"})
        by_name = await call(hass, search, {"tools": ["ha_search"]})

    assert "ha_search" not in listed
    [hit] = payload(by_query)["results"]
    assert "pinned" not in hit
    [entry] = payload(by_name)["results"]
    assert entry["input_schema"] == unconvertible


async def test_keyword_hit_on_a_pinned_tool_is_the_stub(
    hass: HomeAssistant,
) -> None:
    result = await _search(hass, {"query": "ha_search"}, _catalog())

    [entry] = result.data["results"]
    assert entry == {
        "name": "ha_search",
        "pinned": True,
        "hint": "ha_search is already in your tool list — call it directly.",
    }


async def test_tools_hop_answers_in_the_order_the_agent_asked(
    hass: HomeAssistant,
) -> None:
    asked = ["ha_get_state", "ha_config_set_automation"]

    result = await _search(hass, {"tools": asked}, _catalog())

    assert [entry["name"] for entry in result.data["results"]] == asked


async def test_tools_hop_does_not_mix_in_keyword_hits(
    hass: HomeAssistant,
) -> None:
    result = await _search(
        hass,
        {"query": "automation", "tools": ["ha_get_state"]},
        _catalog(),
    )

    assert [entry["name"] for entry in result.data["results"]] == ["ha_get_state"]


async def test_tools_hop_does_not_reveal_that_a_hidden_tool_exists(
    hass: HomeAssistant,
) -> None:
    # A distinct answer for a hidden name would leak its existence, the same
    # rule ha_call_tool keeps.
    result = await _search(
        hass, {"tools": ["ha_restart", "ha_totally_made_up"]}, _catalog()
    )

    hidden, missing = result.data["results"]
    assert "input_schema" not in hidden
    assert "suggestion" in hidden

    def blank(entry: dict[str, str], name: str) -> dict[str, str]:
        return {k: v.replace(name, "<n>") for k, v in entry.items()}

    assert blank(hidden, "ha_restart") == blank(missing, "ha_totally_made_up")


async def test_tools_hop_with_no_known_name_is_an_error(
    hass: HomeAssistant,
) -> None:
    """``ha_call_tool`` answers an unknown name with an error; a lookup that
    resolved nothing must not read as a successful schema fetch."""
    nothing = await _search(hass, {"tools": ["ha_totally_made_up"]}, _catalog())
    mixed = await _search(
        hass, {"tools": ["ha_totally_made_up", "ha_get_state"]}, _catalog()
    )

    assert nothing.error is True
    assert mixed.error is False


@pytest.mark.parametrize("args", [{}, {"query": "  "}], ids=["empty", "blank"])
async def test_search_without_query_or_tools_is_an_error(
    hass: HomeAssistant, args: dict[str, Any]
) -> None:
    result = await _search(hass, args, _catalog())

    assert result.error is True
    assert "error" in result.data


async def test_prompt_tells_the_agent_to_fetch_the_schema_before_calling(
    hass: HomeAssistant,
) -> None:
    async with serve(_catalog()) as server:
        instance = await api_instance(hass, server.port)

    assert "ha_search_tools(tools=" in instance.api_prompt
