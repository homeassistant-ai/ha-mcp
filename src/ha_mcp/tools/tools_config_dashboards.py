"""
Configuration management tools for Home Assistant Lovelace dashboards.

This module provides tools for managing dashboard metadata and content.
"""

import logging
from typing import Annotated, Any, cast

from pydantic import Field

from .helpers import log_tool_usage
from .util_helpers import parse_json_param

logger = logging.getLogger(__name__)


def register_config_dashboard_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register Home Assistant dashboard configuration tools."""

    @mcp.tool
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
            dashboards = await client.websocket_client.list_dashboards()
            return {
                "success": True,
                "action": "list",
                "dashboards": dashboards,
                "count": len(dashboards),
            }
        except Exception as e:
            logger.error(f"Error listing dashboards: {e}")
            return {"success": False, "action": "list", "error": str(e)}

    @mcp.tool
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
            config = await client.websocket_client.get_dashboard_config(
                url_path=url_path or None, force=force_reload
            )
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

    @mcp.tool
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
                "Can be dict, JSON string, or YAML string. "
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

        EXAMPLES:

        Create empty dashboard:
        ha_config_set_dashboard(
            url_path="mobile-dashboard",
            title="Mobile View",
            icon="mdi:cellphone"
        )

        Create dashboard with initial config:
        ha_config_set_dashboard(
            url_path="home-dashboard",
            title="Home Overview",
            config={
                "views": [{
                    "title": "Home",
                    "cards": [{
                        "type": "entities",
                        "entities": ["light.living_room"]
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

        Update existing dashboard config:
        ha_config_set_dashboard(
            url_path="existing-dashboard",
            config={
                "views": [{
                    "title": "Updated View",
                    "cards": [{"type": "markdown", "content": "Updated!"}]
                }]
            }
        )

        Note: If dashboard exists, only the config is updated. To change metadata
        (title, icon), use ha_config_update_dashboard_metadata().

        Strategy types available: home, areas, map, original-states, iframe
        See documentation for strategy-specific configuration options.
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
            existing_dashboards = await client.websocket_client.list_dashboards()
            dashboard_exists = any(d.get("url_path") == url_path for d in existing_dashboards)

            # If dashboard doesn't exist, create it
            if not dashboard_exists:
                # Use provided title or generate from url_path
                dashboard_title = title or url_path.replace("-", " ").title()

                create_result = await client.websocket_client.create_dashboard(
                    url_path=url_path,
                    title=dashboard_title,
                    icon=icon,
                    require_admin=require_admin,
                    show_in_sidebar=show_in_sidebar,
                )

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
                await client.websocket_client.save_dashboard_config(
                    config=config_dict, url_path=url_path
                )
                config_updated = True

            return {
                "success": True,
                "action": "create" if not dashboard_exists else "update",
                "url_path": url_path,
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
                    "Verify config format is valid Lovelace YAML/JSON",
                ],
            }

    @mcp.tool
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
            result = await client.websocket_client.update_dashboard(
                dashboard_id=dashboard_id,
                title=title,
                icon=icon,
                require_admin=require_admin,
                show_in_sidebar=show_in_sidebar,
            )
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

    @mcp.tool
    @log_tool_usage
    async def ha_config_delete_dashboard(
        dashboard_id: Annotated[
            str, Field(description="Dashboard ID to delete (typically same as url_path)")
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
            await client.websocket_client.delete_dashboard(dashboard_id)
            return {
                "success": True,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "message": "Dashboard deleted successfully",
            }
        except Exception as e:
            logger.error(f"Error deleting dashboard: {e}")
            return {
                "success": False,
                "action": "delete",
                "dashboard_id": dashboard_id,
                "error": str(e),
                "suggestions": [
                    "Verify dashboard exists and is storage-mode",
                    "Check that you have admin permissions",
                    "Use ha_config_list_dashboards() to see available dashboards",
                    "Cannot delete YAML-mode or default dashboard",
                ],
            }
