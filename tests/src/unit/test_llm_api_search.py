"""Unit tests for the tool-search mode's ``ha_search_tools`` (#2633).

A keyword search returns compact hits so a context-limited agent can scan
many tools cheaply; ``tools=[name]`` is the second hop that returns one
tool's full input schema before the agent executes it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ._embedded_stubs import install

install()

from custom_components.ha_mcp_tools import llm_api  # noqa: E402
from custom_components.ha_mcp_tools.const import EXPOSURE_TOOL_SEARCH  # noqa: E402

from ._llm_api_helpers import (  # noqa: E402
    fake_session,
    make_api,
    make_hass,
    tool_entry,
)
from .test_llm_tool_metadata import _CoreToolResult  # noqa: E402

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


def _tool(name: str, schema: dict[str, Any], *, exposed: bool = True) -> Any:
    entry = tool_entry(name, exposed=exposed)
    entry.inputSchema = schema
    return entry


def _catalog() -> list[SimpleNamespace]:
    return [
        _tool("ha_config_set_automation", _FULL_SCHEMA),
        tool_entry("ha_get_state"),
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
            {"name": {"type": "string"}, "limit": {"type": "integer"}},
            [],
            "name (string); limit (integer)",
        ),
        ({}, [], "none"),
    ],
    ids=["enum-required", "nullable", "enum-branch", "array", "ref", "two", "none"],
)
async def test_search_hit_tells_the_agent_each_param_in_one_line(
    monkeypatch: pytest.MonkeyPatch,
    properties: dict[str, Any],
    required: list[str],
    params: str,
) -> None:
    schema = {"type": "object", "properties": properties, "required": required}

    result = await _search(
        monkeypatch, {"query": "widget"}, [_tool("ha_widget", schema)]
    )

    assert [hit["params"] for hit in result["results"]] == [params]


async def test_tools_hop_returns_the_full_schema_compact_hits_omit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _search(
        monkeypatch, {"tools": ["ha_config_set_automation"]}, _catalog()
    )

    [entry] = result["results"]
    assert entry["input_schema"] == _FULL_SCHEMA


async def test_tools_hop_answers_in_the_order_the_agent_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked = ["ha_get_state", "ha_config_set_automation"]

    result = await _search(monkeypatch, {"tools": asked}, _catalog())

    assert [entry["name"] for entry in result["results"]] == asked


async def test_tools_hop_does_not_mix_in_keyword_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = await _search(
        monkeypatch, {"query": "automation", "tools": ["ha_get_state"]}, _catalog()
    )

    assert [entry["name"] for entry in result["results"]] == ["ha_get_state"]


async def test_tools_hop_does_not_reveal_that_a_hidden_tool_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A distinct answer for a hidden name would leak its existence, the same
    # rule ha_call_tool keeps.
    result = await _search(
        monkeypatch, {"tools": ["ha_restart", "ha_totally_made_up"]}, _catalog()
    )

    hidden, missing = result["results"]
    assert "error" in hidden
    assert "input_schema" not in hidden
    assert {**hidden, "name": ""} == {**missing, "name": ""}


@pytest.mark.parametrize("args", [{}, {"query": "  "}], ids=["empty", "blank"])
async def test_search_without_query_or_tools_is_an_error(
    monkeypatch: pytest.MonkeyPatch, args: dict[str, Any]
) -> None:
    monkeypatch.setattr(llm_api.llm, "ToolResult", _CoreToolResult, raising=False)

    result = await _search(monkeypatch, args, _catalog())

    assert result.error is True
    assert "error" in result.data


async def test_prompt_tells_the_agent_to_fetch_the_schema_before_calling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = await _instance(monkeypatch, _catalog())

    assert "ha_search_tools(tools=" in instance.api_prompt
