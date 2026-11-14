"""The tool parameters that take a dict or a list of dicts, shared by the
schema and coercion guards. Each row is (module, register function, tool, param).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# config param on set tools (PR #1485)
# ---------------------------------------------------------------------------

CONFIG_TOOLS = [
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

SERVICE_AND_ENTITY_TOOLS = [
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

BULK_TOOLS = [
    (
        "ha_mcp.tools.tools_service",
        "register_service_tools",
        "ha_bulk_control",
        "operations",
    ),
]

DICT_TOOLS = CONFIG_TOOLS + SERVICE_AND_ENTITY_TOOLS
