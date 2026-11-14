"""Capability-aware search catalogs preserve the callable legacy signature.

The transform asks the client's component for its capabilities: here the
server's own client on a real ``hass``, with the ha_mcp_tools commands
registered (``component_core_client``) or not (``core_client``), so every
capability answer is the real component's.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ha_mcp_tools import websocket_api
from custom_components.ha_mcp_tools.websocket_api import system
from ha_mcp._vendor.fastmcp import Client, FastMCP
from ha_mcp._vendor.fastmcp.experimental.transforms.code_mode import GetSchemas
from ha_mcp._vendor.fastmcp.tools import Tool
from ha_mcp._vendor.mcp.types import ToolAnnotations
from ha_mcp.client.rest_client import HomeAssistantClient, HomeAssistantConnectionError
from ha_mcp.client.websocket_client import websocket_manager
from ha_mcp.tools.component_api import INFO_COMMAND, invalidate_caps
from ha_mcp.tools.smart_search import SmartSearchTools
from ha_mcp.tools.tools_search import SearchTools
from ha_mcp.transforms import (
    CategorizedSearchTransform,
    ComponentSearchSchemaTransform,
    component_search,
)

_UNIFIED = ("search", "device_registry_child_semantics", "search_unified")
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


def _advertise_without(monkeypatch: pytest.MonkeyPatch, *missing: str) -> None:
    """The component advertises its capabilities less ``missing``, as an older
    component does."""
    monkeypatch.setattr(
        system, "CAPABILITIES", [c for c in system.CAPABILITIES if c not in missing]
    )


@pytest.fixture
def search_tool(core_client: HomeAssistantClient) -> Tool:
    return Tool.from_function(
        SearchTools(core_client, SmartSearchTools(client=core_client)).ha_search
    )


async def test_registered_search_schema_hides_only_legacy_options(
    component_core_client: HomeAssistantClient, search_tool: Tool
) -> None:
    original = deepcopy(search_tool.parameters)
    description = search_tool.description
    transform = ComponentSearchSchemaTransform(component_core_client)

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


async def _assert_original_catalog(client: HomeAssistantClient, tool: Tool) -> None:
    transform = ComponentSearchSchemaTransform(client)
    assert (await transform.list_tools([tool]))[0] is tool
    assert await transform.get_tool("ha_search", AsyncMock(return_value=tool)) is tool


async def test_install_without_the_component_keeps_original_catalog(
    core_client: HomeAssistantClient, search_tool: Tool
) -> None:
    await _assert_original_catalog(core_client, search_tool)


@pytest.mark.parametrize("missing", _UNIFIED)
async def test_component_missing_a_unified_capability_keeps_original_catalog(
    monkeypatch: pytest.MonkeyPatch,
    component_core_client: HomeAssistantClient,
    search_tool: Tool,
    missing: str,
) -> None:
    _advertise_without(monkeypatch, missing)

    await _assert_original_catalog(component_core_client, search_tool)


async def test_unanswered_probe_keeps_original_catalog(
    component_core_client: HomeAssistantClient, search_tool: Tool
) -> None:
    """With no WebSocket answer at all, the legacy contract stays advertised."""
    with patch(
        "ha_mcp.tools.component_api.get_websocket_client",
        AsyncMock(side_effect=HomeAssistantConnectionError("connection refused")),
    ):
        await _assert_original_catalog(component_core_client, search_tool)


async def test_unrelated_or_missing_tools_do_not_probe(
    component_core_client: HomeAssistantClient, search_tool: Tool
) -> None:
    async def other() -> str:
        return "ok"

    tool = Tool.from_function(other)
    transform = ComponentSearchSchemaTransform(component_core_client)
    with patch.object(
        component_search,
        "get_component_caps",
        wraps=component_search.get_component_caps,
    ) as probe:
        assert (await transform.list_tools([tool]))[0] is tool
        assert await transform.get_tool("other", AsyncMock(return_value=tool)) is tool
        assert await transform.get_tool("missing", AsyncMock(return_value=None)) is None
        probe.assert_not_awaited()

        await transform.list_tools([tool, search_tool])
        probe.assert_awaited_with(component_core_client)


async def test_capabilities_are_rechecked_without_mutating_catalog(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    core_client: HomeAssistantClient,
    search_tool: Tool,
) -> None:
    """The catalog follows the component as it is installed and downgraded,
    and the registered tool never takes the trimmed schema."""
    original = deepcopy(search_tool.parameters)
    transform = ComponentSearchSchemaTransform(core_client)
    assert (await transform.list_tools([search_tool]))[0] is search_tool

    websocket_api.async_register_commands(hass)
    invalidate_caps(core_client)
    assert _HIDDEN.isdisjoint(
        (await transform.list_tools([search_tool]))[0].parameters["properties"]
    )

    _advertise_without(monkeypatch, "search_unified")
    invalidate_caps(core_client)
    assert (await transform.list_tools([search_tool]))[0] is search_tool
    assert search_tool.parameters == original


async def _unified(client: HomeAssistantClient, tool: Tool) -> bool:
    """Whether a fresh transform on ``client`` lists the trimmed schema."""
    listed = (await ComponentSearchSchemaTransform(client).list_tools([tool]))[0]
    return listed is not tool and _HIDDEN.isdisjoint(listed.parameters["properties"])


async def test_capability_cache_is_kept_per_ha_client(
    monkeypatch: pytest.MonkeyPatch,
    component_core_client: HomeAssistantClient,
    second_client: HomeAssistantClient,
    search_tool: Tool,
) -> None:
    """Each client is probed once and keeps its own answer: a component that
    changed between the two probes does not leak into the other's catalog."""
    socket = await websocket_manager.get_client(
        url=component_core_client.base_url,
        token=component_core_client.token,
        verify_ssl=False,
    )
    with patch.object(socket, "send_command", wraps=socket.send_command) as sent:
        assert await _unified(component_core_client, search_tool)
        # The second client meets an older component.
        _advertise_without(monkeypatch, "search_unified")
        assert not await _unified(second_client, search_tool)
        # Asked again, each keeps the answer it was given.
        assert await _unified(component_core_client, search_tool)
        assert not await _unified(second_client, search_tool)

    assert [c.args[0] for c in sent.call_args_list].count(INFO_COMMAND) == 2


async def _echo_ha_search(
    query: str | None = None,
    search_types: list[str] | None = None,
    include_config: bool = False,
    config_time_budget: float = 20,
) -> dict[str, Any]:
    """A tool with ha_search's legacy options that returns what it was given."""
    return {
        "query": query,
        "search_types": search_types,
        "include_config": include_config,
        "config_time_budget": config_time_budget,
    }


@pytest.mark.usefixtures("tool_search_default")
@pytest.mark.parametrize("categorized", [False, True])
async def test_wire_catalog_and_calls_preserve_legacy_arguments(
    component_core_client: HomeAssistantClient, categorized: bool
) -> None:
    mcp = FastMCP("component-search-schema")
    mcp.add_tool(
        Tool.from_function(
            _echo_ha_search,
            name="ha_search",
            annotations=ToolAnnotations(read_only_hint=True),
        )
    )
    mcp.add_transform(ComponentSearchSchemaTransform(component_core_client))
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


@pytest.mark.usefixtures("tool_search_default")
async def test_search_and_get_schema_share_transformed_catalog(
    component_core_client: HomeAssistantClient, search_tool: Tool
) -> None:
    mcp = FastMCP("component-search-discovery")
    mcp.add_tool(search_tool)
    mcp.add_transform(ComponentSearchSchemaTransform(component_core_client))
    search = CategorizedSearchTransform(always_visible=["get_schema"])
    mcp.add_transform(search)
    mcp.add_tool(GetSchemas()(search.get_tool_catalog))
    async with Client(mcp) as client:
        result = await client.call_tool(
            "ha_search_tools", {"query": "broader discovery"}
        )
        hit = next(t for t in result.data if t["name"] == "ha_search")
        assert not any(f"{name} (" in hit["params"] for name in _HIDDEN)
        full = await client.call_tool("ha_search_tools", {"tools": ["ha_search"]})
        found = full.data[0]
        assert _HIDDEN.isdisjoint(found["inputSchema"]["properties"])
        schemas = await client.call_tool(
            "get_schema", {"tools": ["ha_search"], "detail": "full"}
        )
        schema = json.loads(schemas.data)[0]
        assert schema["inputSchema"] == found["inputSchema"]
        assert schema["description"] == found["description"]
