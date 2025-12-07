"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import json
import logging
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)

# Card documentation base URL
CARD_DOCS_BASE_URL = (
    "https://raw.githubusercontent.com/home-assistant/home-assistant.io/"
    "refs/heads/current/source/_dashboards"
)


def _get_resources_dir() -> Path:
    """Get resources directory path, works for both dev and installed package."""
    # Try to find resources directory relative to this file
    resources_dir = Path(__file__).parent.parent / "resources"
    if resources_dir.exists():
        return resources_dir

    # Fallback: try to find in package data (for installed packages)
    try:
        import importlib.resources as pkg_resources

        # For Python 3.9+
        if hasattr(pkg_resources, "files"):
            resources_dir = pkg_resources.files("ha_mcp") / "resources"
            if hasattr(resources_dir, "__fspath__"):
                return Path(str(resources_dir))
    except (ImportError, AttributeError):
        # If importlib.resources or its attributes are unavailable, fall back to relative path
        pass

    # Last resort: return the relative path and let it fail with clear error
    return resources_dir


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard"],
            "title": "List Dashboards",
        }
    )
    @log_tool_usage
    async def ha_config_list_dashboards() -> dict[str, Any]:
        """
        List all Home Assistant storage-mode dashboards.

        Returns metadata for all custom dashboards including url_path, title,
        icon, admin requirements, and sidebar visibility.

        Note: Only shows storage-mode dashboards. YAML-mode dashboards
        (defined in configuration.yaml) are not included.

        EXAMPLES:
        - List dashboards: ha_config_list_dashboards()
        """
        try:
            result = await client.send_websocket_message(
                {"type": "lovelace/dashboards/list"}
            )
            if isinstance(result, dict) and "result" in result:
                dashboards = result["result"]
            elif isinstance(result, list):
                dashboards = result
            else:
                dashboards = []

            return {
                "success": True,
                "action": "list",
                "dashboards": dashboards,
                "count": len(dashboards),
            }
        except Exception as e:
            logger.error(f"Error listing dashboards: {e}")
            return {"success": False, "action": "list", "error": str(e)}

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard"],
            "title": "Get Dashboard Config",
        }
    )
    @log_tool_usage
    async def ha_config_get_dashboard(
        url_path: Annotated[
            str | None,
            Field(
                description="Dashboard URL path (e.g., 'lovelace-home'). "
                "Use None or empty string for default dashboard."
            ),
        ] = None,
        force_reload: Annotated[
            bool, Field(description="Force reload from storage (bypass cache)")
        ] = False,
    ) -> dict[str, Any]:
        """
        Get complete dashboard configuration including all views and cards.

        Returns the full Lovelace dashboard configuration.

        EXAMPLES:
        - Get default dashboard: ha_config_get_dashboard()
        - Get custom dashboard: ha_config_get_dashboard("lovelace-mobile")
        - Force reload: ha_config_get_dashboard("lovelace-home", force_reload=True)

        Note: url_path=None retrieves the default dashboard configuration.
        """
        try:
            # Build WebSocket message
            data: dict[str, Any] = {"type": "lovelace/config", "force": force_reload}
            if url_path:
                data["url_path"] = url_path

            response = await client.send_websocket_message(data)

            # Check if request failed
            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "action": "get",
                    "url_path": url_path,
                    "error": str(error_msg),
                    "suggestions": [
                        "Verify dashboard exists using ha_config_list_dashboards()",
                        "Check if you have permission to access this dashboard",
                        "Use None for default dashboard",
                    ],
                }

            # Extract config from WebSocket response
            config = response.get("result") if isinstance(response, dict) else response
            return {
                "success": True,
                "action": "get",
                "url_path": url_path,
                "config": config,
            }
        except Exception as e:
            logger.error(f"Error getting dashboard config: {e}")
            return {
                "success": False,
                "action": "get",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard exists using ha_config_list_dashboards()",
                    "Check if you have permission to access this dashboard",
                    "Use None for default dashboard",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["dashboard"],
            "title": "Create or Update Dashboard",
        }
    )
    @log_tool_usage
    async def ha_config_set_dashboard(
        url_path: Annotated[
            str,
            Field(
                description="Unique URL path for dashboard (must contain hyphen, "
                "e.g., 'my-dashboard', 'mobile-view')"
            ),
        ],
        config: Annotated[
            str | dict[str, Any] | None,
            Field(
                description="Dashboard configuration with views and cards. "
                "Can be dict or JSON string. "
                "Omit or set to None to create dashboard without initial config."
            ),
        ] = None,
        title: Annotated[
            str | None,
            Field(description="Dashboard display name shown in sidebar"),
        ] = None,
        icon: Annotated[
            str | None,
            Field(
                description="MDI icon name (e.g., 'mdi:home', 'mdi:cellphone'). "
                "Defaults to 'mdi:view-dashboard'"
            ),
        ] = None,
        require_admin: Annotated[
            bool, Field(description="Restrict dashboard to admin users only")
        ] = False,
        show_in_sidebar: Annotated[
            bool, Field(description="Show dashboard in sidebar navigation")
        ] = True,
    ) -> dict[str, Any]:
        """
        Create or update a Home Assistant dashboard.

        Creates a new dashboard or updates an existing one with the provided configuration.

        IMPORTANT: url_path must contain a hyphen (-) to be valid.

        MODERN DASHBOARD BEST PRACTICES (2024+):
        - Use "sections" view type (default) with grid-based layouts
        - Use "tile" cards as primary card type (replaces legacy entity/light/climate cards)
        - Use "grid" cards for multi-column layouts within sections
        - Create multiple views with navigation paths (avoid single-view endless scrolling)
        - Use "area" cards with navigation for hierarchical organization

        DISCOVERING ENTITY IDs FOR DASHBOARDS:
        Do NOT guess entity IDs - use these tools to find exact entity IDs:
        1. ha_get_overview(include_entity_id=True) - Get all entities organized by domain/area
        2. ha_search_entities(query, domain_filter, area_filter) - Find specific entities
        3. ha_deep_search(query) - Comprehensive search across entities, areas, automations

        If unsure about entity IDs, ALWAYS use one of these tools first.

        DASHBOARD DOCUMENTATION:
        - ha_get_dashboard_guide() - Complete guide (structure, views, cards, features, pitfalls)
        - ha_get_card_types() - List of all 41 available card types
        - ha_get_card_documentation(card_type) - Card-specific docs (e.g., "tile", "grid")

        EXAMPLES:

        Create empty dashboard:
        ha_config_set_dashboard(
            url_path="mobile-dashboard",
            title="Mobile View",
            icon="mdi:cellphone"
        )

        Create dashboard with modern sections view:
        ha_config_set_dashboard(
            url_path="home-dashboard",
            title="Home Overview",
            config={
                "views": [{
                    "title": "Home",
                    "type": "sections",
                    "sections": [{
                        "title": "Climate",
                        "cards": [{
                            "type": "tile",
                            "entity": "climate.living_room",
                            "features": [{"type": "target-temperature"}]
                        }]
                    }]
                }]
            }
        )

        Create strategy-based dashboard (auto-generated):
        ha_config_set_dashboard(
            url_path="my-home",
            title="My Home",
            config={
                "strategy": {
                    "type": "home",
                    "favorite_entities": ["light.bedroom"]
                }
            }
        )

        Note: Strategy dashboards cannot be converted to custom dashboards via this tool.
        Use the "Take Control" feature in the Home Assistant interface to convert them.

        Update existing dashboard config:
        ha_config_set_dashboard(
            url_path="existing-dashboard",
            config={
                "views": [{
                    "title": "Updated View",
                    "type": "sections",
                    "sections": [{
                        "cards": [{"type": "markdown", "content": "Updated!"}]
                    }]
                }]
            }
        )

        Note: If dashboard exists, only the config is updated. To change metadata
        (title, icon), use ha_config_update_dashboard_metadata().
        """
        try:
            # Validate url_path contains hyphen
            if "-" not in url_path:
                return {
                    "success": False,
                    "action": "set",
                    "error": "url_path must contain a hyphen (-)",
                    "suggestions": [
                        f"Try '{url_path.replace('_', '-')}' instead",
                        "Use format like 'my-dashboard' or 'mobile-view'",
                    ],
                }

            # Check if dashboard exists
            result = await client.send_websocket_message(
                {"type": "lovelace/dashboards/list"}
            )
            if isinstance(result, dict) and "result" in result:
                existing_dashboards = result["result"]
            elif isinstance(result, list):
                existing_dashboards = result
            else:
                existing_dashboards = []
            dashboard_exists = any(
                d.get("url_path") == url_path for d in existing_dashboards
            )

            # If dashboard doesn't exist, create it
            dashboard_id = None
            if not dashboard_exists:
                # Use provided title or generate from url_path
                dashboard_title = title or url_path.replace("-", " ").title()

                # Build create message
                create_data: dict[str, Any] = {
                    "type": "lovelace/dashboards/create",
                    "url_path": url_path,
                    "title": dashboard_title,
                    "require_admin": require_admin,
                    "show_in_sidebar": show_in_sidebar,
                }
                if icon:
                    create_data["icon"] = icon
                create_result = await client.send_websocket_message(create_data)

                # Check if dashboard creation was successful
                if isinstance(create_result, dict) and not create_result.get(
                    "success", True
                ):
                    error_msg = create_result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "create",
                        "url_path": url_path,
                        "error": str(error_msg),
                    }

                # Extract dashboard ID from create response
                if isinstance(create_result, dict) and "result" in create_result:
                    dashboard_info = create_result["result"]
                    dashboard_id = dashboard_info.get("id")
                elif isinstance(create_result, dict):
                    dashboard_id = create_result.get("id")
            else:
                # If dashboard already exists, get its ID from the list
                for dashboard in existing_dashboards:
                    if dashboard.get("url_path") == url_path:
                        dashboard_id = dashboard.get("id")
                        break

            # Set config if provided
            config_updated = False
            if config is not None:
                parsed_config = parse_json_param(config, "config")
                if parsed_config is None or not isinstance(parsed_config, dict):
                    return {
                        "success": False,
                        "action": "set",
                        "error": "Config parameter must be a dict/object",
                        "provided_type": type(parsed_config).__name__,
                    }

                config_dict = cast(dict[str, Any], parsed_config)

                # Build save config message
                save_data: dict[str, Any] = {
                    "type": "lovelace/config/save",
                    "config": config_dict,
                }
                if url_path:
                    save_data["url_path"] = url_path
                save_result = await client.send_websocket_message(save_data)

                # Check if save failed
                if isinstance(save_result, dict) and not save_result.get(
                    "success", True
                ):
                    error_msg = save_result.get("error", {})
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    return {
                        "success": False,
                        "action": "set",
                        "url_path": url_path,
                        "error": f"Failed to save dashboard config: {error_msg}",
                        "suggestions": [
                            "Verify config format is valid Lovelace JSON",
                            "Check that you have admin permissions",
                            "Ensure all entity IDs in config exist",
                        ],
                    }

                config_updated = True

            return {
                "success": True,
                "action": "create" if not dashboard_exists else "update",
                "url_path": url_path,
                "dashboard_id": dashboard_id,
                "dashboard_created": not dashboard_exists,
                "config_updated": config_updated,
                "message": f"Dashboard {url_path} {'created' if not dashboard_exists else 'updated'} successfully",
            }

        except Exception as e:
            logger.error(f"Error setting dashboard: {e}")
            return {
                "success": False,
                "action": "set",
                "url_path": url_path,
                "error": str(e),
                "suggestions": [
                    "Ensure url_path is unique (not already in use for different dashboard type)",
                    "Verify url_path contains a hyphen",
                    "Check that you have admin permissions",
                    "Verify config format is valid Lovelace JSON",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "tags": ["dashboard"],
            "title": "Update Dashboard Metadata",
        }
    )
    @log_tool_usage
    async def ha_config_update_dashboard_metadata(
        dashboard_id: Annotated[
            str, Field(description="Dashboard ID (typically same as url_path)")
        ],
        title: Annotated[str | None, Field(description="New dashboard title")] = None,
        icon: Annotated[str | None, Field(description="New MDI icon name")] = None,
        require_admin: Annotated[
            bool | None, Field(description="Update admin requirement")
        ] = None,
        show_in_sidebar: Annotated[
            bool | None, Field(description="Update sidebar visibility")
        ] = None,
    ) -> dict[str, Any]:
        """
        Update dashboard metadata (title, icon, permissions) without changing content.

        Updates dashboard properties without modifying the actual configuration
        (views/cards). At least one field must be provided.

        EXAMPLES:

        Change dashboard title:
        ha_config_update_dashboard_metadata(
            dashboard_id="mobile-dashboard",
            title="Mobile View v2"
        )

        Update multiple properties:
        ha_config_update_dashboard_metadata(
            dashboard_id="admin-panel",
            title="Admin Dashboard",
            icon="mdi:shield-account",
            require_admin=True
        )

        Hide from sidebar:
        ha_config_update_dashboard_metadata(
            dashboard_id="hidden-dashboard",
            show_in_sidebar=False
        )
        """
        if all(x is None for x in [title, icon, require_admin, show_in_sidebar]):
            return {
                "success": False,
                "action": "update_metadata",
                "error": "At least one field must be provided to update",
            }

        try:
            # Build update message
            update_data: dict[str, Any] = {
                "type": "lovelace/dashboards/update",
                "dashboard_id": dashboard_id,
            }
            if title is not None:
                update_data["title"] = title
            if icon is not None:
                update_data["icon"] = icon
            if require_admin is not None:
                update_data["require_admin"] = require_admin
            if show_in_sidebar is not None:
                update_data["show_in_sidebar"] = show_in_sidebar

            result = await client.send_websocket_message(update_data)

            # Check if update failed
            if isinstance(result, dict) and not result.get("success", True):
                error_msg = result.get("error", {})
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                return {
                    "success": False,
                    "action": "update_metadata",
                    "dashboard_id": dashboard_id,
                    "error": str(error_msg),
                    "suggestions": [
                        "Verify dashboard ID exists using ha_config_list_dashboards()",
                        "Check that you have admin permissions",
                    ],
                }

            return {
                "success": True,
                "action": "update_metadata",
                "dashboard_id": dashboard_id,
                "updated_fields": {
                    k: v
                    for k, v in {
                        "title": title,
                        "icon": icon,
                        "require_admin": require_admin,
                        "show_in_sidebar": show_in_sidebar,
                    }.items()
                    if v is not None
                },
                "dashboard": result,
            }
        except Exception as e:
            logger.error(f"Error updating dashboard metadata: {e}")
            return {
                "success": False,
                "action": "update_metadata",
                "dashboard_id": dashboard_id,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard ID exists using ha_config_list_dashboards()",
                    "Check that you have admin permissions",
                ],
            }

    @mcp.tool(
        annotations={
            "destructiveHint": True,
            "idempotentHint": True,
            "tags": ["dashboard"],
            "title": "Delete Dashboard",
        }
    )
    @log_tool_usage
    async def ha_config_delete_dashboard(
        dashboard_id: Annotated[
            str,
            Field(description="Dashboard ID to delete (typically same as url_path)"),
        ],
    ) -> dict[str, Any]:
        """
        Delete a storage-mode dashboard completely.

        WARNING: This permanently deletes the dashboard and all its configuration.
        Cannot be undone. Does not work on YAML-mode dashboards.

        EXAMPLES:
        - Delete dashboard: ha_config_delete_dashboard("mobile-dashboard")

        Note: The default dashboard cannot be deleted via this method.
        """
        try:
            response = await client.send_websocket_message(
                {"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id}
            )

            # Check response for error indication
            if isinstance(response, dict) and not response.get("success", True):
                error_msg = response.get("error", {})
                if isinstance(error_msg, dict):
                    error_str = error_msg.get("message", str(error_msg))
                else:
                    error_str = str(error_msg)

                logger.error(f"Error deleting dashboard: {error_str}")

                # If the error is "not found" / "doesn't exist", treat as success (idempotent)
                if (
                    "unable to find" in error_str.lower()
                    or "not found" in error_str.lower()
                ):
                    return {
                        "success": True,
                        "action": "delete",
                        "dashboard_id": dashboard_id,
                        "message": "Dashboard already deleted or does not exist",
                    }

                # For other errors, return failure
                return {
                    "success": False,
                    "action": "delete",
                    "dashboard_id": dashboard_id,
                    "error": error_str,
                    "suggestions": [
                        "Verify dashboard exists and is storage-mode",
                        "Check that you have admin permissions",
                        "Use ha_config_list_dashboards() to see available dashboards",
                        "Cannot delete YAML-mode or default dashboard",
                    ],
                }

            # Delete successful
            return {
                "success": True,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "message": "Dashboard deleted successfully",
            }
        except Exception as e:
            error_str = str(e)
            logger.error(f"Error deleting dashboard: {error_str}")

            # If the error is "not found" / "doesn't exist", treat as success (idempotent)
            if (
                "unable to find" in error_str.lower()
                or "not found" in error_str.lower()
            ):
                return {
                    "success": True,
                    "action": "delete",
                    "dashboard_id": dashboard_id,
                    "message": "Dashboard already deleted or does not exist",
                }

            # For other errors, return failure
            return {
                "success": False,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "error": error_str,
                "suggestions": [
                    "Verify dashboard exists and is storage-mode",
                    "Check that you have admin permissions",
                    "Use ha_config_list_dashboards() to see available dashboards",
                    "Cannot delete YAML-mode or default dashboard",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "docs", "guide"],
            "title": "Get Dashboard Guide",
        }
    )
    @log_tool_usage
    async def ha_get_dashboard_guide() -> dict[str, Any]:
        """
        Get comprehensive guide for designing Home Assistant dashboards.

        Covers:
        - Part 1: Dashboard structure, views, sections, navigation
        - Part 2: Built-in cards (tile, grid, button), features, actions
        - Part 3: Custom cards (JavaScript modules, registration)
        - Part 4: CSS styling (themes, card-mod patterns)
        - Part 5: HACS integration (finding/installing community cards)
        - Part 6: Complete examples and workflows

        Use this guide before designing dashboards to understand
        all available options, from built-in cards to custom resources.

        Related tools:
        - ha_create_dashboard_resource: Host inline JS/CSS
        - ha_hacs_search / ha_hacs_download: Community cards

        EXAMPLES:
        - Get full guide: ha_get_dashboard_guide()
        """
        try:
            resources_dir = _get_resources_dir()
            guide_path = resources_dir / "dashboard_guide.md"
            guide_content = guide_path.read_text()
            return {
                "success": True,
                "action": "get_guide",
                "guide": guide_content,
                "format": "markdown",
            }
        except Exception as e:
            logger.error(f"Error reading dashboard guide: {e}")
            return {
                "success": False,
                "action": "get_guide",
                "error": str(e),
                "suggestions": [
                    "Ensure dashboard_guide.md exists in resources directory",
                    f"Attempted path: {resources_dir / 'dashboard_guide.md' if 'resources_dir' in locals() else 'unknown'}",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "docs"],
            "title": "Get Card Types",
        }
    )
    @log_tool_usage
    async def ha_get_card_types() -> dict[str, Any]:
        """
        Get list of all available Home Assistant dashboard card types.

        Returns all 41 card types that can be used in dashboard configurations.

        EXAMPLES:
        - Get card types: ha_get_card_types()

        Use ha_get_card_documentation(card_type) to get detailed docs for a specific card.
        """
        try:
            resources_dir = _get_resources_dir()
            types_path = resources_dir / "card_types.json"
            card_types_data = json.loads(types_path.read_text())
            return {
                "success": True,
                "action": "get_card_types",
                "card_types": card_types_data["card_types"],
                "total_count": card_types_data["total_count"],
                "documentation_base_url": card_types_data["documentation_base_url"],
            }
        except Exception as e:
            logger.error(f"Error reading card types: {e}")
            return {
                "success": False,
                "action": "get_card_types",
                "error": str(e),
                "suggestions": [
                    "Ensure card_types.json exists in resources directory",
                    f"Attempted path: {resources_dir / 'card_types.json' if 'resources_dir' in locals() else 'unknown'}",
                ],
            }

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["dashboard", "docs"],
            "title": "Get Card Documentation",
        }
    )
    @log_tool_usage
    async def ha_get_card_documentation(
        card_type: Annotated[
            str,
            Field(
                description="Card type name (e.g., 'light', 'thermostat', 'entity'). "
                "Use ha_get_card_types() to see all available types."
            ),
        ],
    ) -> dict[str, Any]:
        """
        Fetch detailed documentation for a specific dashboard card type.

        Returns the official Home Assistant documentation for the specified card type
        in markdown format, fetched directly from the Home Assistant documentation repository.

        EXAMPLES:
        - Get light card docs: ha_get_card_documentation("light")
        - Get thermostat card docs: ha_get_card_documentation("thermostat")
        - Get entity card docs: ha_get_card_documentation("entity")

        First use ha_get_card_types() to see all 41 available card types.
        """
        try:
            # Validate card type exists
            resources_dir = _get_resources_dir()
            types_path = resources_dir / "card_types.json"
            card_types_data = json.loads(types_path.read_text())

            if card_type not in card_types_data["card_types"]:
                available = ", ".join(card_types_data["card_types"][:10])
                return {
                    "success": False,
                    "action": "get_card_documentation",
                    "card_type": card_type,
                    "error": f"Unknown card type '{card_type}'",
                    "suggestions": [
                        f"Available types include: {available}...",
                        "Use ha_get_card_types() to see full list of 41 card types",
                    ],
                }

            # Fetch documentation from GitHub
            doc_url = f"{CARD_DOCS_BASE_URL}/{card_type}.markdown"

            async with httpx.AsyncClient(timeout=10.0) as http_client:
                response = await http_client.get(doc_url)
                response.raise_for_status()
                return {
                    "success": True,
                    "action": "get_card_documentation",
                    "card_type": card_type,
                    "documentation": response.text,
                    "format": "markdown",
                    "source_url": doc_url,
                }
        except httpx.HTTPStatusError as e:
            logger.error(f"Failed to fetch card docs for {card_type}: {e}")
            return {
                "success": False,
                "action": "get_card_documentation",
                "card_type": card_type,
                "error": f"Failed to fetch documentation (HTTP {e.response.status_code})",
                "source_url": doc_url,
            }
        except Exception as e:
            logger.error(f"Error fetching card docs for {card_type}: {e}")
            return {
                "success": False,
                "action": "get_card_documentation",
                "card_type": card_type,
                "error": str(e),
            }


    # =========================================================================
    # Dashboard Resource Management Tools
    # =========================================================================
    # Resource tools have been moved to tools_resources.py for better organization.
    # Available tools:
    # - ha_config_list_dashboard_resources: List all resources
    # - ha_config_set_inline_dashboard_resource: Create/update inline code resources
    # - ha_config_set_dashboard_resource: Create/update URL-based resources
    # - ha_config_delete_dashboard_resource: Delete resources
    # =========================================================================
