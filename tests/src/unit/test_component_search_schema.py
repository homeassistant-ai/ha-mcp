"""Capability-aware search catalogs preserve the callable legacy signature."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.experimental.transforms.code_mode import GetSchemas
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.tools.component_api import ComponentCaps
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.transforms import CategorizedSearchTransform, ComponentSearchSchemaTransform

pytestmark = pytest.mark.asyncio

_CAPABILITIES = frozenset(
    {"search", "device_registry_child_semantics", "search_unified"}
)
_HIDDEN = {"search_types", "include_config", "config_time_budget"}
_RETAINED = {
    "query",
    "domain_filter",
    "area_filter",
    "state_filter",
    "exact_match",
    "include_hidden",
    "limit",
    "offset",
    "result_fields",
    "fields",
    "group_by_domain",
    "per_domain_limit",
}


def _caps(capabilities: frozenset[str] = _CAPABILITIES) -> ComponentCaps:
    return ComponentCaps(1, "999.0.0", capabilities, {})


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    probe = AsyncMock(return_value=_caps())
    monkeypatch.setattr("ha_mcp.transforms.component_search.get_component_caps", probe)
    return probe


@pytest.fixture
def search_tool() -> Tool:
    return Tool.from_function(SearchTools(MagicMock(), MagicMock()).ha_search)


async def test_registered_search_schema_hides_only_legacy_options(
    probe: AsyncMock, search_tool: Tool
) -> None:
    original = deepcopy(search_tool.parameters)
    description = search_tool.description
    client = MagicMock()
    transform = ComponentSearchSchemaTransform(client)

    listed = (await transform.list_tools([search_tool]))[0]
    resolved = await transform.get_tool(
        "ha_search", AsyncMock(return_value=search_tool)
    )

    assert resolved is not None
    assert set(listed.parameters["properties"]) == _RETAINED
    assert resolved.parameters == listed.parameters
    assert not _HIDDEN.intersection(listed.parameters.get("required", []))
    assert search_tool.parameters == original
    assert search_tool.description == description
    assert "ha_config_get_scene" in listed.description
    assert "ha_config_get_dashboard" in listed.description
    assert "broader discovery" in listed.description
    assert "config_time_budget" not in listed.description
    assert "search_types" not in listed.description
    assert (
        "any find-something"
        not in listed.parameters["properties"]["query"]["description"]
    )
    listed.parameters["properties"]["query"]["description"] = "changed by caller"
    assert search_tool.parameters == original
    assert (
        resolved.parameters["properties"]["query"]["description"] != "changed by caller"
    )
    probe.assert_awaited_with(client)


@pytest.mark.parametrize("missing", [None, *sorted(_CAPABILITIES)])
async def test_absent_or_incomplete_component_keeps_original_catalog(
    missing: str | None, probe: AsyncMock, search_tool: Tool
) -> None:
    probe.return_value = None if missing is None else _caps(_CAPABILITIES - {missing})
    transform = ComponentSearchSchemaTransform(MagicMock())
    assert (await transform.list_tools([search_tool]))[0] is search_tool
    assert (
        await transform.get_tool("ha_search", AsyncMock(return_value=search_tool))
        is search_tool
    )


async def test_failed_probe_keeps_original_catalog(
    probe: AsyncMock, search_tool: Tool
) -> None:
    probe.side_effect = RuntimeError("capability discovery unavailable")
    transform = ComponentSearchSchemaTransform(MagicMock())
    assert (await transform.list_tools([search_tool]))[0] is search_tool
    assert (
        await transform.get_tool("ha_search", AsyncMock(return_value=search_tool))
        is search_tool
    )


async def test_unrelated_or_missing_tools_do_not_probe(probe: AsyncMock) -> None:
    async def other() -> str:
        return "ok"

    tool = Tool.from_function(other)
    transform = ComponentSearchSchemaTransform(MagicMock())
    assert (await transform.list_tools([tool]))[0] is tool
    assert await transform.get_tool("other", AsyncMock(return_value=tool)) is tool
    assert await transform.get_tool("missing", AsyncMock(return_value=None)) is None
    probe.assert_not_awaited()


async def test_capabilities_are_rechecked_without_mutating_catalog(
    probe: AsyncMock, search_tool: Tool
) -> None:
    probe.side_effect = [None, _caps(), None]
    transform = ComponentSearchSchemaTransform(MagicMock())
    assert (await transform.list_tools([search_tool]))[0] is search_tool
    assert _HIDDEN.isdisjoint(
        (await transform.list_tools([search_tool]))[0].parameters["properties"]
    )
    assert (await transform.list_tools([search_tool]))[0] is search_tool


async def test_capability_cache_is_shared_per_ha_client(
    monkeypatch: pytest.MonkeyPatch, search_tool: Tool
) -> None:
    modern = MagicMock(base_url="http://modern.invalid", token="test", verify_ssl=True)
    old = MagicMock(base_url="http://old.invalid", token="test", verify_ssl=True)
    modern_ws = MagicMock(
        send_command=AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "schema_version": 1,
                    "component_version": "2.0.0",
                    "capabilities": sorted(_CAPABILITIES),
                    "limits": {},
                },
            }
        )
    )
    old_ws = MagicMock(
        send_command=AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "schema_version": 1,
                    "component_version": "999.0.0",
                    "capabilities": ["search"],
                    "limits": {},
                },
            }
        )
    )
    connect = AsyncMock(side_effect=[modern_ws, old_ws])
    monkeypatch.setattr("ha_mcp.tools.component_api.get_websocket_client", connect)
    for client in (modern, old, modern, old):
        transform = ComponentSearchSchemaTransform(client)
        listed = (await transform.list_tools([search_tool]))[0]
        if client is modern:
            assert _HIDDEN.isdisjoint(listed.parameters["properties"])
        else:
            assert listed is search_tool
    assert connect.await_count == 2
    modern_ws.send_command.assert_awaited_once()
    old_ws.send_command.assert_awaited_once()


@pytest.mark.parametrize("categorized", [False, True])
async def test_wire_catalog_and_calls_preserve_legacy_arguments(
    categorized: bool, probe: AsyncMock
) -> None:
    async def ha_search(
        query: str | None = None,
        search_types: list[str] | None = None,
        include_config: bool = False,
        config_time_budget: float = 20,
    ) -> dict[str, Any]:
        return {
            "query": query,
            "search_types": search_types,
            "include_config": include_config,
            "config_time_budget": config_time_budget,
        }

    mcp = FastMCP("component-search-schema")
    mcp.add_tool(
        Tool.from_function(ha_search, annotations=ToolAnnotations(read_only_hint=True))
    )
    mcp.add_transform(ComponentSearchSchemaTransform(MagicMock()))
    if categorized:
        mcp.add_transform(CategorizedSearchTransform(always_visible=["ha_search"]))
    arguments = {
        "query": "kitchen",
        "search_types": ["scene"],
        "include_config": True,
        "config_time_budget": 7.0,
    }
    async with Client(mcp) as client:
        listed = next(t for t in await client.list_tools() if t.name == "ha_search")
        assert _HIDDEN.isdisjoint(listed.inputSchema["properties"])
        resolved = await mcp.get_tool("ha_search")
        assert resolved is not None
        assert _HIDDEN.isdisjoint(resolved.parameters["properties"])
        result = await client.call_tool("ha_search", arguments)
        assert result.data == arguments
        if categorized:
            proxied = await client.call_tool(
                "ha_call_read_tool", {"name": "ha_search", "arguments": arguments}
            )
            assert proxied.data == arguments


async def test_search_and_get_schema_share_transformed_catalog(
    probe: AsyncMock, search_tool: Tool
) -> None:
    mcp = FastMCP("component-search-discovery")
    mcp.add_tool(search_tool)
    mcp.add_transform(ComponentSearchSchemaTransform(MagicMock()))
    search = CategorizedSearchTransform(always_visible=["get_schema"])
    mcp.add_transform(search)
    mcp.add_tool(GetSchemas()(search.get_tool_catalog))
    async with Client(mcp) as client:
        result = await client.call_tool(
            "ha_search_tools", {"query": "broader discovery"}
        )
        found = next(t for t in result.data if t["name"] == "ha_search")
        assert _HIDDEN.isdisjoint(found["inputSchema"]["properties"])
        schemas = await client.call_tool(
            "get_schema", {"tools": ["ha_search"], "detail": "full"}
        )
        schema = json.loads(schemas.data)[0]
        assert schema["inputSchema"] == found["inputSchema"]
        assert schema["description"] == found["description"]
