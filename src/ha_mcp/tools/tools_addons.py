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
            "hassio/api",
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

        data = result.get("result", {}).get("data", {})
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
            "hassio/api",
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

        data = result.get("result", {}).get("data", {})
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
                "icon": addon.get("icon", False),
                "logo": addon.get("logo", False),
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


def register_addon_tools(mcp: Any, client: HomeAssistantClient) -> None:
    """
    Register add-on management tools with the MCP server.

    Args:
        mcp: FastMCP server instance
        client: Home Assistant REST client
    """

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_addons(
        include_stats: Annotated[
            bool,
            Field(
                description="Include CPU/memory usage statistics for each add-on",
                default=False,
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        List installed Home Assistant add-ons.

        Returns add-ons with version, state (started/stopped), and update availability.
        Optionally includes CPU/memory usage statistics.

        **Note:** This tool only works with Home Assistant OS or Supervised installations.

        **Response includes:**
        - Add-on name, slug, description
        - Current version and state
        - Whether an update is available
        - Repository the add-on came from
        - Optional: CPU/memory usage stats

        **Example Usage:**
        - List all add-ons: ha_list_addons()
        - List with resource usage: ha_list_addons(include_stats=True)

        **Use Cases:**
        - Check which add-ons are installed and running
        - Monitor add-on health and resource usage
        - Find add-ons with available updates
        """
        return await list_addons(client, include_stats)

    @mcp.tool(annotations={"readOnlyHint": True})
    @log_tool_usage
    async def ha_list_available_addons(
        repository: Annotated[
            str | None,
            Field(
                description="Filter by repository slug (e.g., 'core', 'community')",
                default=None,
            ),
        ] = None,
        query: Annotated[
            str | None,
            Field(
                description="Search filter for add-on names/descriptions",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """
        List add-ons available in the Home Assistant add-on store.

        Returns add-ons from official and custom repositories that can be installed.

        **Note:** This tool only works with Home Assistant OS or Supervised installations.

        **Response includes:**
        - List of configured repositories
        - Available add-ons with name, slug, description, version
        - Installation status for each add-on
        - Summary statistics

        **Parameters:**
        - repository: Filter results to a specific repository
        - query: Search by name or description (case-insensitive)

        **Example Usage:**
        - List all available add-ons: ha_list_available_addons()
        - Search for MQTT: ha_list_available_addons(query="mqtt")
        - List official add-ons: ha_list_available_addons(repository="core")

        **Use Cases:**
        - Find add-ons to recommend for user's needs
        - Check if a specific add-on is available
        - Explore add-on store contents
        """
        return await list_available_addons(client, repository, query)
