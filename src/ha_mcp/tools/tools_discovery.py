"""
Tool discovery and filtering tools for Home Assistant MCP server.

This module provides mechanisms for AI clients to discover and filter available tools:
- ha_search_tools: Search for tools by keyword, category, or purpose
- Tool filtering based on configured profiles

Tool Categories:
- search: Entity search, system overview, deep search
- service: Service calls, bulk operations, device control
- automation: Automation CRUD, triggers
- script: Script CRUD, execution
- helper: Input helpers (boolean, number, text, etc.)
- backup: Backup creation and restoration
- calendar: Calendar events
- camera: Camera image capture
- dashboard: Lovelace dashboard configuration
- hacs: HACS integration management
- history: Entity history, statistics
- system: System info, restart, reload
- area: Area/floor management
- zone: Zone configuration
- label: Label management
- todo: Todo list management
- blueprint: Blueprint import/management
- integration: Integration listing
- update: Update discovery and management
- group: Entity groups
- trace: Automation/script traces
"""

import logging
import re
from typing import Any

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)

# Tool category definitions with descriptions
TOOL_CATEGORIES = {
    "search": {
        "description": "Entity search and discovery tools",
        "tools": ["ha_search_entities", "ha_get_overview", "ha_deep_search", "ha_get_state"],
    },
    "service": {
        "description": "Service execution and device control",
        "tools": ["ha_call_service", "ha_bulk_control", "ha_get_operation_status", "ha_get_bulk_status"],
    },
    "automation": {
        "description": "Automation configuration and management",
        "tools": ["ha_config_get_automation", "ha_config_set_automation", "ha_config_delete_automation"],
    },
    "script": {
        "description": "Script configuration and execution",
        "tools": ["ha_config_get_script", "ha_config_set_script", "ha_config_delete_script"],
    },
    "helper": {
        "description": "Input helper management (booleans, numbers, text, etc.)",
        "tools": ["ha_config_list_helpers", "ha_config_set_helper", "ha_config_delete_helper"],
    },
    "backup": {
        "description": "Backup creation and restoration",
        "tools": ["ha_backup_create", "ha_backup_restore"],
    },
    "calendar": {
        "description": "Calendar event management",
        "tools": ["ha_calendar_get_events", "ha_calendar_set_event", "ha_calendar_delete_event"],
    },
    "camera": {
        "description": "Camera image capture",
        "tools": ["ha_camera_get_image"],
    },
    "dashboard": {
        "description": "Lovelace dashboard configuration",
        "tools": [
            "ha_dashboard_list",
            "ha_dashboard_get_config",
            "ha_dashboard_set",
            "ha_dashboard_update_metadata",
            "ha_dashboard_delete",
            "ha_dashboard_get_guide",
            "ha_dashboard_get_card_types",
            "ha_dashboard_get_card_docs",
        ],
    },
    "hacs": {
        "description": "HACS (Home Assistant Community Store) management",
        "tools": [
            "ha_hacs_info",
            "ha_hacs_list_installed",
            "ha_hacs_search_store",
            "ha_hacs_get_repository_info",
            "ha_hacs_add_repository",
            "ha_hacs_download_repository",
        ],
    },
    "history": {
        "description": "Entity history and statistics",
        "tools": ["ha_get_history", "ha_get_statistics", "ha_get_logbook"],
    },
    "system": {
        "description": "System management and monitoring",
        "tools": [
            "ha_check_config",
            "ha_restart",
            "ha_reload_core",
            "ha_get_system_info",
            "ha_get_system_health",
        ],
    },
    "area": {
        "description": "Area and floor management",
        "tools": [
            "ha_list_areas",
            "ha_set_area",
            "ha_delete_area",
            "ha_list_floors",
            "ha_set_floor",
            "ha_delete_floor",
        ],
    },
    "zone": {
        "description": "Zone configuration",
        "tools": ["ha_list_zones", "ha_create_zone", "ha_update_zone", "ha_delete_zone"],
    },
    "label": {
        "description": "Label management for entities",
        "tools": [
            "ha_list_labels",
            "ha_get_label",
            "ha_set_label",
            "ha_delete_label",
            "ha_assign_label",
        ],
    },
    "todo": {
        "description": "Todo list and item management",
        "tools": [
            "ha_list_todo_lists",
            "ha_get_todo_items",
            "ha_add_todo_item",
            "ha_update_todo_item",
            "ha_delete_todo_item",
        ],
    },
    "blueprint": {
        "description": "Blueprint management and import",
        "tools": ["ha_list_blueprints", "ha_get_blueprint", "ha_import_blueprint"],
    },
    "integration": {
        "description": "Integration discovery",
        "tools": ["ha_list_integrations"],
    },
    "update": {
        "description": "Update management",
        "tools": ["ha_list_updates", "ha_get_release_notes", "ha_get_system_version"],
    },
    "group": {
        "description": "Entity group management",
        "tools": ["ha_list_groups", "ha_set_group", "ha_delete_group"],
    },
    "trace": {
        "description": "Automation and script execution traces",
        "tools": ["ha_get_automation_traces"],
    },
    "registry": {
        "description": "Device and entity registry management",
        "tools": [
            "ha_list_devices",
            "ha_get_device",
            "ha_update_device",
            "ha_remove_device",
        ],
    },
    "addon": {
        "description": "Home Assistant add-on management",
        "tools": ["ha_list_installed_addons", "ha_list_available_addons"],
    },
    "services": {
        "description": "Service discovery",
        "tools": ["ha_list_services"],
    },
    "utility": {
        "description": "Utility tools (templates, documentation)",
        "tools": ["ha_eval_template", "ha_get_domain_docs"],
    },
}

# Tool profiles - predefined sets of tools for different use cases
TOOL_PROFILES = {
    "minimal": {
        "description": "Essential tools only (10 tools) - for basic control and monitoring",
        "categories": ["search", "service"],
        "explicit_tools": ["ha_search_entities", "ha_get_state", "ha_call_service", "ha_get_overview"],
    },
    "standard": {
        "description": "Common tools (30+ tools) - for typical smart home management",
        "categories": ["search", "service", "automation", "script", "helper", "history", "system"],
    },
    "extended": {
        "description": "Extended tools (50+ tools) - includes calendar, todo, zones",
        "categories": [
            "search", "service", "automation", "script", "helper",
            "history", "system", "calendar", "todo", "zone", "area", "label", "group",
        ],
    },
    "full": {
        "description": "All available tools (70+ tools) - complete functionality",
        "categories": list(TOOL_CATEGORIES.keys()),
    },
    "developer": {
        "description": "Tools for developers and power users",
        "categories": [
            "search", "service", "automation", "script", "helper",
            "dashboard", "blueprint", "trace", "registry", "system", "utility",
        ],
    },
    "monitoring": {
        "description": "Read-only monitoring tools - no modification capabilities",
        "categories": ["search", "history", "integration", "update"],
        "explicit_tools": [
            "ha_search_entities", "ha_get_state", "ha_get_overview", "ha_deep_search",
            "ha_get_history", "ha_get_statistics", "ha_get_logbook",
            "ha_list_integrations", "ha_list_updates", "ha_get_system_version",
            "ha_get_system_info", "ha_get_system_health", "ha_check_config",
        ],
    },
}


def get_all_tool_metadata() -> dict[str, dict[str, Any]]:
    """
    Get metadata for all tools including category, description hints, etc.

    Returns a dict mapping tool name to metadata.
    """
    metadata: dict[str, dict[str, Any]] = {}

    for category_name, category_info in TOOL_CATEGORIES.items():
        for tool_name in category_info["tools"]:
            metadata[tool_name] = {
                "category": category_name,
                "category_description": category_info["description"],
            }

    return metadata


def search_tools(
    query: str,
    category_filter: str | None = None,
    include_descriptions: bool = True,
) -> list[dict[str, Any]]:
    """
    Search for tools by keyword in name, category, or description.

    Args:
        query: Search query (case-insensitive, partial match)
        category_filter: Optional category to filter by
        include_descriptions: Whether to include category descriptions

    Returns:
        List of matching tools with metadata
    """
    query_lower = query.lower().strip()
    results = []

    metadata = get_all_tool_metadata()

    for tool_name, tool_meta in metadata.items():
        # Apply category filter if specified
        if category_filter and tool_meta["category"] != category_filter.lower():
            continue

        # If category filter is set but no query, include all tools in category
        if category_filter and not query_lower:
            result = {
                "tool_name": tool_name,
                "category": tool_meta["category"],
                "score": 100,
                "match_reasons": ["category_filter"],
            }
            if include_descriptions:
                result["category_description"] = tool_meta["category_description"]
            results.append(result)
            continue

        # Skip if no query (empty searches without category filter return nothing)
        if not query_lower:
            continue

        # Match against tool name or category
        tool_name_lower = tool_name.lower()
        category_lower = tool_meta["category"].lower()
        category_desc_lower = tool_meta["category_description"].lower()

        # Score the match
        score = 0
        match_reasons = []

        # Exact tool name match
        if query_lower == tool_name_lower:
            score = 100
            match_reasons.append("exact_name")
        # Tool name contains query
        elif query_lower in tool_name_lower:
            score = 80
            match_reasons.append("name_contains")
        # Tool name starts with ha_ + query
        elif f"ha_{query_lower}" in tool_name_lower:
            score = 75
            match_reasons.append("name_prefix")
        # Category exact match
        elif query_lower == category_lower:
            score = 60
            match_reasons.append("category_match")
        # Category contains query
        elif query_lower in category_lower:
            score = 50
            match_reasons.append("category_contains")
        # Description contains query
        elif query_lower in category_desc_lower:
            score = 40
            match_reasons.append("description_contains")

        if score > 0:
            result = {
                "tool_name": tool_name,
                "category": tool_meta["category"],
                "score": score,
                "match_reasons": match_reasons,
            }
            if include_descriptions:
                result["category_description"] = tool_meta["category_description"]
            results.append(result)

    # Sort by score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    return results


def get_tools_for_profile(profile_name: str) -> list[str]:
    """
    Get the list of tool names enabled for a given profile.

    Args:
        profile_name: Name of the profile (minimal, standard, extended, full, developer, monitoring)

    Returns:
        List of tool names enabled for this profile
    """
    if profile_name not in TOOL_PROFILES:
        raise ValueError(f"Unknown profile: {profile_name}. Available: {list(TOOL_PROFILES.keys())}")

    profile = TOOL_PROFILES[profile_name]
    enabled_tools = set()

    # Add tools from included categories
    for category in profile.get("categories", []):
        if category in TOOL_CATEGORIES:
            enabled_tools.update(TOOL_CATEGORIES[category]["tools"])

    # Add explicit tools if specified
    explicit_tools = profile.get("explicit_tools", [])
    if explicit_tools:
        # If explicit_tools is set, use only those (for profiles like "minimal" and "monitoring")
        if profile_name in ("minimal", "monitoring"):
            return list(explicit_tools)
        enabled_tools.update(explicit_tools)

    return sorted(enabled_tools)


def get_profile_info(profile_name: str) -> dict[str, Any]:
    """
    Get detailed information about a profile.

    Args:
        profile_name: Name of the profile

    Returns:
        Profile info including description, categories, and tool count
    """
    if profile_name not in TOOL_PROFILES:
        raise ValueError(f"Unknown profile: {profile_name}. Available: {list(TOOL_PROFILES.keys())}")

    profile = TOOL_PROFILES[profile_name]
    tools = get_tools_for_profile(profile_name)

    return {
        "name": profile_name,
        "description": profile["description"],
        "categories": profile.get("categories", []),
        "tool_count": len(tools),
        "tools": tools,
    }


def list_all_profiles() -> list[dict[str, Any]]:
    """
    List all available profiles with their descriptions and tool counts.

    Returns:
        List of profile summaries
    """
    profiles = []
    for profile_name in TOOL_PROFILES:
        try:
            info = get_profile_info(profile_name)
            profiles.append({
                "name": info["name"],
                "description": info["description"],
                "tool_count": info["tool_count"],
                "categories": info["categories"],
            })
        except Exception as e:
            logger.warning(f"Error getting profile info for {profile_name}: {e}")

    return profiles


def list_all_categories() -> list[dict[str, Any]]:
    """
    List all available tool categories with descriptions and tool counts.

    Returns:
        List of category summaries
    """
    categories = []
    for category_name, category_info in TOOL_CATEGORIES.items():
        categories.append({
            "name": category_name,
            "description": category_info["description"],
            "tool_count": len(category_info["tools"]),
            "tools": category_info["tools"],
        })

    # Sort by tool count descending
    categories.sort(key=lambda x: x["tool_count"], reverse=True)

    return categories


def register_discovery_tools(mcp, client, **kwargs):
    """Register tool discovery and filtering tools with the MCP server."""

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["meta", "discovery"], "title": "Search Tools"})
    @log_tool_usage
    async def ha_search_tools(
        query: str = "",
        category: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """
        Search for available MCP tools by keyword, category, or purpose.

        This meta-tool helps discover the right tool for a task without needing
        to know all 70+ available tools. Search by:
        - Tool name: "automation", "backup", "search"
        - Category: "helper", "dashboard", "hacs"
        - Purpose: "delete", "create", "list"

        **Examples:**
        ```python
        # Find automation-related tools
        ha_search_tools(query="automation")

        # Find tools in the helper category
        ha_search_tools(category="helper")

        # Find tools for creating things
        ha_search_tools(query="create")

        # Find all tools (empty query lists categories)
        ha_search_tools()
        ```

        **Available Categories:**
        - search: Entity search and discovery
        - service: Service execution and device control
        - automation: Automation configuration
        - script: Script configuration
        - helper: Input helpers (boolean, number, text, etc.)
        - backup: Backup management
        - calendar: Calendar events
        - camera: Camera images
        - dashboard: Lovelace dashboards
        - hacs: HACS integration
        - history: Entity history and statistics
        - system: System management
        - area/zone: Area and zone configuration
        - label: Label management
        - todo: Todo lists
        - blueprint: Blueprint management
        - update: Update management
        - And more...

        Args:
            query: Search term (optional, empty shows all categories)
            category: Filter by specific category
            limit: Maximum number of results

        Returns:
            Matching tools with category and relevance information
        """
        try:
            # If no query, show categories overview
            if not query.strip() and not category:
                categories = list_all_categories()
                return {
                    "success": True,
                    "mode": "categories_overview",
                    "message": "No search query provided. Showing all tool categories.",
                    "total_categories": len(categories),
                    "categories": categories,
                    "tip": "Use query parameter to search for specific tools, or category parameter to filter by category.",
                }

            # Search for tools
            results = search_tools(query, category_filter=category)

            if not results:
                # No matches - suggest alternatives
                all_categories = list(TOOL_CATEGORIES.keys())
                return {
                    "success": True,
                    "query": query,
                    "category_filter": category,
                    "total_matches": 0,
                    "results": [],
                    "suggestions": [
                        "Try a broader search term",
                        f"Available categories: {', '.join(all_categories[:10])}...",
                        "Use empty query to see all categories",
                    ],
                }

            return {
                "success": True,
                "query": query,
                "category_filter": category,
                "total_matches": len(results),
                "results": results[:limit],
            }

        except Exception as e:
            logger.error(f"Error searching tools: {e}")
            return {
                "success": False,
                "error": str(e),
                "query": query,
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["meta", "discovery"], "title": "List Tool Profiles"})
    @log_tool_usage
    async def ha_list_tool_profiles() -> dict[str, Any]:
        """
        List available tool profiles with their descriptions and tool counts.

        Tool profiles are predefined sets of tools for different use cases:
        - **minimal**: Essential tools only (10 tools) - basic control and monitoring
        - **standard**: Common tools (30+ tools) - typical smart home management
        - **extended**: Extended tools (50+ tools) - includes calendar, todo, zones
        - **full**: All available tools (70+ tools) - complete functionality
        - **developer**: Tools for developers - dashboards, blueprints, traces
        - **monitoring**: Read-only tools only - no modification capabilities

        Use ha_get_tool_profile(profile_name) to see the full list of tools in a profile.

        Returns:
            List of profiles with descriptions and tool counts
        """
        try:
            profiles = list_all_profiles()
            return {
                "success": True,
                "total_profiles": len(profiles),
                "profiles": profiles,
                "note": "Use ha_get_tool_profile() to see all tools in a specific profile",
            }
        except Exception as e:
            logger.error(f"Error listing profiles: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["meta", "discovery"], "title": "Get Tool Profile"})
    @log_tool_usage
    async def ha_get_tool_profile(
        profile_name: str,
    ) -> dict[str, Any]:
        """
        Get detailed information about a specific tool profile.

        Returns the full list of tools included in the profile, organized by category.

        **Available Profiles:**
        - minimal: Essential tools (10 tools)
        - standard: Common tools (30+ tools)
        - extended: Extended tools (50+ tools)
        - full: All tools (70+ tools)
        - developer: Developer tools
        - monitoring: Read-only monitoring tools

        Args:
            profile_name: Name of the profile to retrieve

        Returns:
            Profile details including all tool names and categories
        """
        try:
            info = get_profile_info(profile_name)

            # Group tools by category for better readability
            tools_by_category: dict[str, list[str]] = {}
            metadata = get_all_tool_metadata()

            for tool in info["tools"]:
                category = metadata.get(tool, {}).get("category", "other")
                if category not in tools_by_category:
                    tools_by_category[category] = []
                tools_by_category[category].append(tool)

            return {
                "success": True,
                "profile": {
                    "name": info["name"],
                    "description": info["description"],
                    "tool_count": info["tool_count"],
                    "categories": info["categories"],
                },
                "tools_by_category": tools_by_category,
                "all_tools": info["tools"],
            }
        except ValueError as e:
            available_profiles = list(TOOL_PROFILES.keys())
            return {
                "success": False,
                "error": str(e),
                "available_profiles": available_profiles,
            }
        except Exception as e:
            logger.error(f"Error getting profile: {e}")
            return {
                "success": False,
                "error": str(e),
            }

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["meta", "discovery"], "title": "List Tool Categories"})
    @log_tool_usage
    async def ha_list_tool_categories() -> dict[str, Any]:
        """
        List all available tool categories with their descriptions and tools.

        Categories organize tools by functionality:
        - search: Entity discovery (4 tools)
        - service: Device control (4 tools)
        - automation: Automation management (3 tools)
        - script: Script management (3 tools)
        - helper: Input helpers (3 tools)
        - And 15+ more categories...

        Returns:
            All categories with descriptions, tool counts, and tool lists
        """
        try:
            categories = list_all_categories()

            # Calculate totals
            total_tools = sum(c["tool_count"] for c in categories)

            return {
                "success": True,
                "total_categories": len(categories),
                "total_tools": total_tools,
                "categories": categories,
            }
        except Exception as e:
            logger.error(f"Error listing categories: {e}")
            return {
                "success": False,
                "error": str(e),
            }
