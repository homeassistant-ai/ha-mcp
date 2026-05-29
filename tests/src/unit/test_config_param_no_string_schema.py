"""Schema regression test: config parameters must not advertise string type.

Verifies that the MCP JSON schema for the `config` parameter on all five
config-set tools is `{type: object}` (or `anyOf` containing only object/null),
never `{type: string}` or an `anyOf` that includes string.

A string in the schema tells models that passing a JSON string is valid.
This causes retry loops when models pass strings that fail server-side
parsing. The fix: annotate config as `dict | None`, not `str | dict | None`.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest


def _get_config_schema(register_fn, tool_name: str) -> dict:
    from fastmcp import FastMCP

    async def _inner():
        mcp = FastMCP("test")
        register_fn(mcp, MagicMock())
        tool = await mcp.get_tool(tool_name)
        return tool.parameters["properties"]["config"]

    return asyncio.run(_inner())


def _contains_string_type(schema: dict) -> bool:
    """Return True if the schema allows string values."""
    return schema.get("type") == "string" or any(
        variant.get("type") == "string" for variant in schema.get("anyOf", [])
    )


@pytest.mark.parametrize(
    ("module", "register_fn", "tool_name"),
    [
        (
            "ha_mcp.tools.tools_config_automations",
            "register_config_automation_tools",
            "ha_config_set_automation",
        ),
        (
            "ha_mcp.tools.tools_config_scripts",
            "register_config_script_tools",
            "ha_config_set_script",
        ),
        (
            "ha_mcp.tools.tools_config_scenes",
            "register_config_scene_tools",
            "ha_config_set_scene",
        ),
        (
            "ha_mcp.tools.tools_config_helpers",
            "register_config_helper_tools",
            "ha_config_set_helper",
        ),
        (
            "ha_mcp.tools.tools_config_dashboards",
            "register_config_dashboard_tools",
            "ha_config_set_dashboard",
        ),
    ],
)
def test_config_param_does_not_advertise_string(
    module: str, register_fn: str, tool_name: str
) -> None:
    """config parameter schema must not include type:string."""
    import importlib

    mod = importlib.import_module(module)
    fn = getattr(mod, register_fn)
    schema = _get_config_schema(fn, tool_name)
    assert not _contains_string_type(schema), (
        f"{tool_name}: config schema still advertises string type. Schema: {schema}"
    )


@pytest.mark.parametrize(
    ("module", "register_fn", "tool_name"),
    [
        (
            "ha_mcp.tools.tools_config_automations",
            "register_config_automation_tools",
            "ha_config_set_automation",
        ),
        (
            "ha_mcp.tools.tools_config_scripts",
            "register_config_script_tools",
            "ha_config_set_script",
        ),
        (
            "ha_mcp.tools.tools_config_scenes",
            "register_config_scene_tools",
            "ha_config_set_scene",
        ),
        (
            "ha_mcp.tools.tools_config_helpers",
            "register_config_helper_tools",
            "ha_config_set_helper",
        ),
        (
            "ha_mcp.tools.tools_config_dashboards",
            "register_config_dashboard_tools",
            "ha_config_set_dashboard",
        ),
    ],
)
def test_config_param_advertises_object(
    module: str, register_fn: str, tool_name: str
) -> None:
    """config parameter schema must include type:object."""
    import importlib

    mod = importlib.import_module(module)
    fn = getattr(mod, register_fn)
    schema = _get_config_schema(fn, tool_name)
    # Either top-level type:object or anyOf containing {type:object}
    has_object = schema.get("type") == "object" or any(
        v.get("type") == "object" for v in schema.get("anyOf", [])
    )
    assert has_object, (
        f"{tool_name}: config schema does not include type:object. Schema: {schema}"
    )
