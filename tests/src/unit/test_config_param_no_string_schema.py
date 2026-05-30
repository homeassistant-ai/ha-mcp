"""Schema regression test: dict/object parameters must not advertise string type.

Verifies that the MCP JSON schema for parameters that must be dicts/objects
never includes type:string or an anyOf containing string. A string in the schema
tells models that passing a JSON string is valid, causing retry loops when models
pass strings that fail server-side parsing.

Fix: annotate as `dict | None`, not `str | dict | None`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest


def _get_param_schema(
    register_fn: Callable[..., Any], tool_name: str, param_name: str
) -> dict[str, Any]:
    from fastmcp import FastMCP

    async def _inner() -> dict[str, Any]:
        mcp = FastMCP("test")
        register_fn(mcp, MagicMock(), device_tools=MagicMock())
        tool = await mcp.get_tool(tool_name)
        return tool.parameters["properties"][param_name]  # type: ignore[no-any-return]

    return asyncio.run(_inner())


def _contains_string_type(schema: dict[str, Any]) -> bool:
    """Return True if the schema allows string values."""
    return schema.get("type") == "string" or any(
        variant.get("type") == "string" for variant in schema.get("anyOf", [])
    )


# ---------------------------------------------------------------------------
# config param on set tools (PR #1485)
# ---------------------------------------------------------------------------

_CONFIG_TOOLS = [
    (
        "ha_mcp.tools.tools_config_automations",
        "register_config_automation_tools",
        "ha_config_set_automation",
        "config",
    ),
    (
        "ha_mcp.tools.tools_config_scripts",
        "register_config_script_tools",
        "ha_config_set_script",
        "config",
    ),
    (
        "ha_mcp.tools.tools_config_scenes",
        "register_config_scene_tools",
        "ha_config_set_scene",
        "config",
    ),
    (
        "ha_mcp.tools.tools_config_helpers",
        "register_config_helper_tools",
        "ha_config_set_helper",
        "config",
    ),
    (
        "ha_mcp.tools.tools_config_dashboards",
        "register_config_dashboard_tools",
        "ha_config_set_dashboard",
        "config",
    ),
]

# ---------------------------------------------------------------------------
# data/options/categories/expose_to params on service & entity tools
# ---------------------------------------------------------------------------

_SERVICE_AND_ENTITY_TOOLS = [
    (
        "ha_mcp.tools.tools_service",
        "register_service_tools",
        "ha_call_service",
        "data",
    ),
    (
        "ha_mcp.tools.tools_service",
        "register_service_tools",
        "ha_call_event",
        "data",
    ),
    (
        "ha_mcp.tools.tools_entities",
        "register_entity_tools",
        "ha_set_entity",
        "options",
    ),
    (
        "ha_mcp.tools.tools_entities",
        "register_entity_tools",
        "ha_set_entity",
        "categories",
    ),
    (
        "ha_mcp.tools.tools_entities",
        "register_entity_tools",
        "ha_set_entity",
        "expose_to",
    ),
]

# ---------------------------------------------------------------------------
# operations param on ha_bulk_control (list[dict], not dict)
# ---------------------------------------------------------------------------

_BULK_TOOLS = [
    (
        "ha_mcp.tools.tools_service",
        "register_service_tools",
        "ha_bulk_control",
        "operations",
    ),
]

_ALL_TOOLS = _CONFIG_TOOLS + _SERVICE_AND_ENTITY_TOOLS


@pytest.mark.parametrize(
    ("module", "register_fn", "tool_name", "param_name"),
    _ALL_TOOLS,
)
def test_param_does_not_advertise_string(module, register_fn, tool_name, param_name):
    """Object/dict parameter schema must not include type:string."""
    import importlib

    mod = importlib.import_module(module)
    fn = getattr(mod, register_fn)
    schema = _get_param_schema(fn, tool_name, param_name)
    assert not _contains_string_type(schema), (
        f"{tool_name}.{param_name}: schema still advertises string type. Schema: {schema}"
    )


@pytest.mark.parametrize(
    ("module", "register_fn", "tool_name", "param_name"),
    _ALL_TOOLS,
)
def test_param_advertises_object(module, register_fn, tool_name, param_name):
    """Object/dict parameter schema must include type:object."""
    import importlib

    mod = importlib.import_module(module)
    fn = getattr(mod, register_fn)
    schema = _get_param_schema(fn, tool_name, param_name)
    has_object = schema.get("type") == "object" or any(
        v.get("type") == "object" for v in schema.get("anyOf", [])
    )
    assert has_object, (
        f"{tool_name}.{param_name}: schema does not include type:object. Schema: {schema}"
    )


@pytest.mark.parametrize(
    ("module", "register_fn", "tool_name", "param_name"),
    _BULK_TOOLS,
)
def test_list_param_does_not_advertise_string(
    module, register_fn, tool_name, param_name
):
    """List parameter schema must not include type:string."""
    import importlib

    mod = importlib.import_module(module)
    fn = getattr(mod, register_fn)
    schema = _get_param_schema(fn, tool_name, param_name)
    assert not _contains_string_type(schema), (
        f"{tool_name}.{param_name}: schema still advertises string type. Schema: {schema}"
    )


@pytest.mark.parametrize(
    ("module", "register_fn", "tool_name", "param_name"),
    _BULK_TOOLS,
)
def test_list_param_advertises_array(module, register_fn, tool_name, param_name):
    """List parameter schema must include type:array."""
    import importlib

    mod = importlib.import_module(module)
    fn = getattr(mod, register_fn)
    schema = _get_param_schema(fn, tool_name, param_name)
    has_array = schema.get("type") == "array" or any(
        v.get("type") == "array" for v in schema.get("anyOf", [])
    )
    assert has_array, (
        f"{tool_name}.{param_name}: schema does not include type:array. Schema: {schema}"
    )
