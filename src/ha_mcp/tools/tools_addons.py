"""
Add-on management tools for Home Assistant MCP Server.

Provides tools to list installed and available add-ons via the Supervisor API.

Note: These tools only work with Home Assistant OS or Supervised installations.
"""

import logging
from typing import Annotated, Any

from pydantic import Field

from ..client.rest_client import HomeAssistantClient
from .helpers import get_connected_ws_client, log_tool_usage

logger = logging.getLogger(__name__)


async def list_addons(
    client: HomeAssistantClient, include_stats: bool = False
) -> dict[str, Any]:
    """
    List installed Home Assistant add-ons.

    Args:
        client: Home Assistant REST client
        include_stats: Include CPU/memory usage statistics

    Returns:
        Dictionary with installed add-ons and their status.
    """
    ws_client = None

    try:
        # Connect to WebSocket
        ws_client, error = await get_connected_ws_client(client.base_url, client.token)
        if error or ws_client is None:
            return error or {
                "success": False,
                "error": "Failed to establish WebSocket connection",
            }

        # Call Supervisor API to get installed add-ons
        result = await ws_client.send_command(
            "supervisor/api",
            endpoint="/addons",
            method="GET",
        )

        if not result.get("success"):
            # Check if this is a non-Supervisor installation
            error_msg = str(result.get("error", ""))
            if "not_found" in error_msg.lower() or "unknown" in error_msg.lower():
                return {
                    "success": False,
                    "error": "Supervisor API not available",
                    "suggestion": "This feature requires Home Assistant OS or Supervised installation",
                    "details": result,
                }
            return {
                "success": False,
                "error": "Failed to retrieve add-ons list",
                "details": result,
            }

        # Response structure: result.addons (not result.data.addons)
        data = result.get("result", {})
        addons = data.get("addons", [])

        # Format add-on information
        formatted_addons = []
        for addon in addons:
            addon_info = {
                "name": addon.get("name"),
                "slug": addon.get("slug"),
                "description": addon.get("description"),
                "version": addon.get("version"),
                "installed": True,
                "state": addon.get("state"),
                "update_available": addon.get("update_available", False),
                "repository": addon.get("repository"),
            }

            # Include stats if requested
            if include_stats:
                addon_info["stats"] = {
                    "cpu_percent": addon.get("cpu_percent"),
                    "memory_percent": addon.get("memory_percent"),
                    "memory_usage": addon.get("memory_usage"),
                    "memory_limit": addon.get("memory_limit"),
                }

            formatted_addons.append(addon_info)

        # Count add-ons by state
        running_count = sum(1 for a in addons if a.get("state") == "started")
        update_count = sum(1 for a in addons if a.get("update_available"))

        return {
            "success": True,
            "addons": formatted_addons,
            "summary": {
                "total_installed": len(formatted_addons),
                "running": running_count,
                "stopped": len(formatted_addons) - running_count,
                "updates_available": update_count,
            },
        }

    except Exception as e:
        logger.error(f"Error listing add-ons: {e}")
        return {
            "success": False,
            "error": f"Failed to list add-ons: {str(e)}",
            "suggestion": "Check Home Assistant connection and Supervisor availability",
        }
    finally:
        if ws_client:
            try:
                await ws_client.disconnect()
            except Exception:
                pass


async def list_available_addons(
    client: HomeAssistantClient,
    repository: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """
    List add-ons available in the add-on store.

    Args:
        client: Home Assistant REST client
        repository: Filter by repository slug (e.g., "core", "community")
        query: Search filter for add-on names/descriptions

    Returns:
        Dictionary with available add-ons and repositories.
    """
    ws_client = None

    try:
        # Connect to WebSocket
        ws_client, error = await get_connected_ws_client(client.base_url, client.token)
        if error or ws_client is None:
            return error or {
                "success": False,
                "error": "Failed to establish WebSocket connection",
            }

        # Call Supervisor API to get store information
        result = await ws_client.send_command(
            "supervisor/api",
            endpoint="/store",
            method="GET",
        )

        if not result.get("success"):
            # Check if this is a non-Supervisor installation
            error_msg = str(result.get("error", ""))
            if "not_found" in error_msg.lower() or "unknown" in error_msg.lower():
                return {
                    "success": False,
                    "error": "Supervisor API not available",
                    "suggestion": "This feature requires Home Assistant OS or Supervised installation",
                    "details": result,
                }
            return {
                "success": False,
                "error": "Failed to retrieve add-on store",
                "details": result,
            }

        # Response structure: result.addons/repositories (not result.data.*)
        data = result.get("result", {})
        repositories = data.get("repositories", [])
        addons = data.get("addons", [])

        # Format repository information
        formatted_repos = []
        for repo in repositories:
            formatted_repos.append(
                {
                    "slug": repo.get("slug"),
                    "name": repo.get("name"),
                    "source": repo.get("source"),
                    "maintainer": repo.get("maintainer"),
                }
            )

        # Filter and format add-ons
        formatted_addons = []
        for addon in addons:
            # Apply repository filter
            if repository and addon.get("repository") != repository:
                continue

            # Apply search query filter
            if query:
                query_lower = query.lower()
                name = (addon.get("name") or "").lower()
                description = (addon.get("description") or "").lower()
                if query_lower not in name and query_lower not in description:
                    continue

            addon_info = {
                "name": addon.get("name"),
                "slug": addon.get("slug"),
                "description": addon.get("description"),
                "version": addon.get("version"),
                "available": addon.get("available", True),
                "installed": addon.get("installed", False),
                "repository": addon.get("repository"),
                "url": addon.get("url"),
                "icon": addon.get("icon"),
                "logo": addon.get("logo"),
            }
            formatted_addons.append(addon_info)

        # Count statistics
        installed_count = sum(1 for a in formatted_addons if a.get("installed"))

        return {
            "success": True,
            "repositories": formatted_repos,
            "addons": formatted_addons,
            "summary": {
                "total_available": len(formatted_addons),
                "installed": installed_count,
                "not_installed": len(formatted_addons) - installed_count,
                "repository_count": len(formatted_repos),
            },
            "filters_applied": {
                "repository": repository,
                "query": query,
            },
        }

    except Exception as e:
        logger.error(f"Error listing available add-ons: {e}")
        return {
            "success": False,
            "error": f"Failed to list available add-ons: {str(e)}",
            "suggestion": "Check Home Assistant connection and Supervisor availability",
        }
    finally:
        if ws_client:
            try:
                await ws_client.disconnect()
            except Exception:
                pass


def register_addon_tools(mcp: Any, client: HomeAssistantClient, **kwargs) -> None:
    """
    Register add-on management tools with the MCP server.

    Args:
        mcp: FastMCP server instance
        client: Home Assistant REST client
        **kwargs: Additional arguments (ignored, for auto-discovery compatibility)
    """

    @mcp.tool(annotations={"idempotentHint": True, "readOnlyHint": True, "tags": ["addon"], "title": "Get Add-ons"})
    @log_tool_usage
    async def ha_get_addon(
        source: Annotated[
            str | None,
            Field(
                description="Add-on source: 'installed' (default) for currently installed add-ons, "
                "'available' for add-ons in the store that can be installed.",
                default=None,
            ),
        ] = None,
        include_stats: Annotated[
            bool,
            Field(
                description="Include CPU/memory usage statistics (only for source='installed')",
                default=False,
            ),
        ] = False,
        repository: Annotated[
            str | None,
            Field(
                description="Filter by repository slug, e.g., 'core', 'community' (only for source='available')",
                default=None,
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Search filter for add-on names/descriptions (only for source='available')",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        Get Home Assistant add-ons - list installed or available from store.

        This tool retrieves add-on information based on the source parameter:
        - source='installed' (default): Lists currently installed add-ons
        - source='available': Lists add-ons available in the add-on store

        **Note:** This tool only works with Home Assistant OS or Supervised installations.

        **INSTALLED ADD-ONS (source='installed'):**
        Returns add-ons with version, state (started/stopped), and update availability.
        - include_stats: Optionally include CPU/memory usage statistics

        **AVAILABLE ADD-ONS (source='available'):**
        Returns add-ons from official and custom repositories that can be installed.
        - repository: Filter by repository slug (e.g., 'core', 'community')
        - query: Search by name or description (case-insensitive)

        **Example Usage:**
        - List installed add-ons: ha_get_addon()
        - List with resource usage: ha_get_addon(include_stats=True)
        - List available add-ons: ha_get_addon(source="available")
        - Search for MQTT: ha_get_addon(source="available", query="mqtt")
        - List official add-ons: ha_get_addon(source="available", repository="core")

        **Use Cases:**
        - Check which add-ons are installed and running
        - Monitor add-on health and resource usage
        - Find add-ons with available updates
        - Find add-ons to recommend for user's needs
        - Check if a specific add-on is available
        """
        # Default to installed if not specified
        effective_source = (source or "installed").lower()

        if effective_source == "available":
            return await list_available_addons(client, repository, query)
        elif effective_source == "installed":
            return await list_addons(client, include_stats)
        else:
            return {
                "success": False,
                "error": f"Invalid source: {source}. Must be 'installed' or 'available'.",
                "valid_sources": ["installed", "available"],
            }
