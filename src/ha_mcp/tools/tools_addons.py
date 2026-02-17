"""
Add-on management tools for Home Assistant MCP Server.

Provides tools to list installed and available add-ons via the Supervisor API,
and to call add-on web APIs through Home Assistant's Ingress proxy.

Note: These tools only work with Home Assistant OS or Supervised installations.
"""

import json
import logging
from typing import Annotated, Any

import httpx
from pydantic import Field

from ..client.rest_client import HomeAssistantClient
from ..errors import (
    ErrorCode,
    create_connection_error,
    create_error_response,
    create_timeout_error,
    create_validation_error,
)
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
)

logger = logging.getLogger(__name__)

# Maximum response size to return from add-on API calls (50 KB)
_MAX_RESPONSE_SIZE = 50 * 1024


async def _supervisor_api_call(
    client: HomeAssistantClient,
    endpoint: str,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Make a Supervisor API call via WebSocket.

    Handles connection, command execution, error checking, and cleanup.

    Args:
        client: Home Assistant REST client (provides base_url and token)
        endpoint: Supervisor API endpoint (e.g., "/addons", "/addons/{slug}/info")
        method: HTTP method (default "GET")
        data: Optional request body data
        timeout: Optional timeout override

    Returns:
        The "result" field from a successful response, or an error dict.
    """
    ws_client = None
    try:
        ws_client, error = await get_connected_ws_client(client.base_url, client.token)
        if error or ws_client is None:
            return error or create_connection_error(
                "Failed to establish WebSocket connection",
            )

        kwargs: dict[str, Any] = {"endpoint": endpoint, "method": method}
        if data is not None:
            kwargs["data"] = data
        if timeout is not None:
            kwargs["timeout"] = timeout

        result = await ws_client.send_command("supervisor/api", **kwargs)

        if not result.get("success"):
            error_msg = str(result.get("error", ""))
            if "not_found" in error_msg.lower() or "unknown" in error_msg.lower():
                return create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "Supervisor API not available",
                    details=str(result),
                    suggestions=[
                        "This feature requires Home Assistant OS or Supervised installation",
                    ],
                )
            return create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Supervisor API call failed: {endpoint}",
                details=str(result),
            )

        return {"success": True, "result": result.get("result", {})}

    except Exception as e:
        logger.error(f"Error calling Supervisor API {endpoint}: {e}")
        return exception_to_structured_error(
            e,
            context={"endpoint": endpoint},
            raise_error=False,
            suggestions=["Check Home Assistant connection and Supervisor availability"],
        )
    finally:
        if ws_client:
            try:
                await ws_client.disconnect()
            except Exception:
                pass


async def get_addon_info(
    client: HomeAssistantClient, slug: str
) -> dict[str, Any]:
    """Get detailed info for a specific add-on.

    Args:
        client: Home Assistant REST client
        slug: Add-on slug (e.g., "a0d7b954_nodered")

    Returns:
        Dictionary with add-on details including ingress info, state, options, etc.
    """
    response = await _supervisor_api_call(client, f"/addons/{slug}/info")
    if not response.get("success"):
        return response
    return {"success": True, "addon": response["result"]}


async def list_addons(
    client: HomeAssistantClient, include_stats: bool = False
) -> dict[str, Any]:
    """List installed Home Assistant add-ons.

    Args:
        client: Home Assistant REST client
        include_stats: Include CPU/memory usage statistics

    Returns:
        Dictionary with installed add-ons and their status.
    """
    response = await _supervisor_api_call(client, "/addons")
    if not response.get("success"):
        return response

    data = response["result"]
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


async def list_available_addons(
    client: HomeAssistantClient,
    repository: str | None = None,
    query: str | None = None,
) -> dict[str, Any]:
    """List add-ons available in the add-on store.

    Args:
        client: Home Assistant REST client
        repository: Filter by repository slug (e.g., "core", "community")
        query: Search filter for add-on names/descriptions

    Returns:
        Dictionary with available add-ons and repositories.
    """
    response = await _supervisor_api_call(client, "/store")
    if not response.get("success"):
        return response

    data = response["result"]
    repositories = data.get("repositories", [])
    addons = data.get("addons", [])

    # Format repository information
    formatted_repos = [
        {
            "slug": repo.get("slug"),
            "name": repo.get("name"),
            "source": repo.get("source"),
            "maintainer": repo.get("maintainer"),
        }
        for repo in repositories
    ]

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


async def _call_addon_api(
    client: HomeAssistantClient,
    slug: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Call an add-on's web API through Home Assistant's Ingress proxy.

    Args:
        client: Home Assistant REST client
        slug: Add-on slug (e.g., "a0d7b954_nodered")
        path: API path relative to add-on root (e.g., "/flows")
        method: HTTP method (GET, POST, PUT, DELETE, PATCH)
        body: Request body for POST/PUT/PATCH
        timeout: Request timeout in seconds (default 30)

    Returns:
        Dictionary with response data, status code, and content type.
    """
    # 1. Sanitize path to prevent traversal attacks
    normalized = path.lstrip("/")
    if ".." in normalized.split("/"):
        return create_validation_error(
            "Path contains '..' traversal component",
            parameter="path",
            details=f"Rejected path: {path}",
        )

    # 2. Get add-on info to verify ingress support and get entry path
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        return addon_response

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)

    # 3. Verify add-on supports Ingress
    if not addon.get("ingress"):
        return create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"Add-on '{addon_name}' does not support Ingress",
            suggestions=[
                "Check if this add-on exposes a direct port instead",
                f"Use ha_get_addon(slug='{slug}') to see port mappings",
            ],
            context={"slug": slug},
        )

    # 4. Verify add-on is running
    if addon.get("state") != "started":
        return create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Add-on '{addon_name}' is not running (state: {addon.get('state')})",
            suggestions=[
                f"Start the add-on first with: ha_call_service('hassio', 'addon_start', {{'addon': '{slug}'}})",
            ],
            context={"slug": slug, "state": addon.get("state")},
        )

    # 5. Build Ingress URL
    ingress_entry = addon.get("ingress_entry", "")
    if not ingress_entry:
        return create_error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Add-on '{addon_name}' has Ingress enabled but no ingress_entry path",
            context={"slug": slug},
        )

    url = f"{client.base_url}{ingress_entry}/{normalized}"

    # 6. Make HTTP request through Ingress
    headers: dict[str, str] = {
        "Authorization": f"Bearer {client.token}",
    }

    # Set content type based on body type
    if isinstance(body, dict):
        headers["Content-Type"] = "application/json"
        request_content = json.dumps(body).encode()
    elif isinstance(body, str):
        headers["Content-Type"] = "application/json"
        request_content = body.encode()
    else:
        request_content = None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as http_client:
            response = await http_client.request(
                method=method.upper(),
                url=url,
                headers=headers,
                content=request_content,
            )
    except httpx.TimeoutException:
        return create_timeout_error(
            f"add-on API call to '{addon_name}'",
            timeout,
            details=f"path={path}, method={method}",
            context={"slug": slug, "path": path},
        )
    except httpx.ConnectError as e:
        return create_connection_error(
            f"Failed to connect to add-on '{addon_name}': {e!s}",
            details="Check that the add-on is running and Home Assistant Ingress is working",
            context={"slug": slug},
        )

    # 6. Parse response
    content_type = response.headers.get("content-type", "")
    response_data: Any

    if "application/json" in content_type:
        try:
            response_data = response.json()
        except (json.JSONDecodeError, ValueError):
            response_data = response.text
    else:
        response_data = response.text

    # 7. Truncate large responses
    truncated = False
    if isinstance(response_data, str) and len(response_data) > _MAX_RESPONSE_SIZE:
        response_data = response_data[:_MAX_RESPONSE_SIZE]
        truncated = True
    elif isinstance(response_data, (dict, list)):
        serialized = json.dumps(response_data, default=str)
        if len(serialized) > _MAX_RESPONSE_SIZE:
            response_data = serialized[:_MAX_RESPONSE_SIZE]
            truncated = True

    result: dict[str, Any] = {
        "success": response.status_code < 400,
        "status_code": response.status_code,
        "response": response_data,
        "content_type": content_type,
        "addon_name": addon_name,
        "slug": slug,
    }

    if truncated:
        result["truncated"] = True
        result["note"] = f"Response truncated to {_MAX_RESPONSE_SIZE // 1024}KB. The full response was larger."

    if response.status_code >= 400:
        result["error"] = f"Add-on API returned HTTP {response.status_code}"

    return result


def register_addon_tools(mcp: Any, client: HomeAssistantClient, **kwargs: Any) -> None:
    """Register add-on management tools with the MCP server.

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
        slug: Annotated[
            str | None,
            Field(
                description="Add-on slug for detailed info (e.g., 'a0d7b954_nodered'). "
                "Omit to list all add-ons.",
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
        """Get Home Assistant add-ons - list installed, available, or get details for one.

        This tool retrieves add-on information based on the parameters:
        - slug provided: Returns detailed info for a single add-on (ingress, ports, options, state)
        - source='installed' (default): Lists currently installed add-ons
        - source='available': Lists add-ons available in the add-on store

        **Note:** This tool only works with Home Assistant OS or Supervised installations.

        **SINGLE ADD-ON (slug provided):**
        Returns comprehensive details including ingress entry, ports, options, and state.
        Useful for discovering what APIs an add-on exposes before calling ha_call_addon_api.

        **INSTALLED ADD-ONS (source='installed'):**
        Returns add-ons with version, state (started/stopped), and update availability.
        - include_stats: Optionally include CPU/memory usage statistics

        **AVAILABLE ADD-ONS (source='available'):**
        Returns add-ons from official and custom repositories that can be installed.
        - repository: Filter by repository slug (e.g., 'core', 'community')
        - query: Search by name or description (case-insensitive)

        **Example Usage:**
        - List installed add-ons: ha_get_addon()
        - Get Node-RED details: ha_get_addon(slug="a0d7b954_nodered")
        - List with resource usage: ha_get_addon(include_stats=True)
        - List available add-ons: ha_get_addon(source="available")
        - Search for MQTT: ha_get_addon(source="available", query="mqtt")
        """
        # If slug is provided, return detailed info for that specific add-on
        if slug:
            return await get_addon_info(client, slug)

        # Default to installed if not specified
        effective_source = (source or "installed").lower()

        if effective_source == "available":
            return await list_available_addons(client, repository, query)
        elif effective_source == "installed":
            return await list_addons(client, include_stats)
        else:
            return create_validation_error(
                f"Invalid source: {source}. Must be 'installed' or 'available'.",
                parameter="source",
                details="Valid sources: installed, available",
            )

    @mcp.tool(annotations={
        "destructiveHint": False,
        "idempotentHint": False,
        "readOnlyHint": False,
        "tags": ["addon"],
        "title": "Call Add-on API",
    })
    @log_tool_usage
    async def ha_call_addon_api(
        slug: Annotated[
            str,
            Field(
                description="Add-on slug (e.g., 'a0d7b954_nodered', 'ccab4aaf_frigate'). "
                "Use ha_get_addon() to find installed add-on slugs.",
            ),
        ],
        path: Annotated[
            str,
            Field(
                description="API path relative to the add-on root (e.g., '/flows', '/api/events', '/api/stats').",
            ),
        ],
        method: Annotated[
            str,
            Field(
                description="HTTP method: GET, POST, PUT, DELETE, PATCH. Defaults to GET.",
                default="GET",
            ),
        ] = "GET",
        body: Annotated[
            dict[str, Any] | str | None,
            Field(
                description="Request body for POST/PUT/PATCH. Pass a JSON object or JSON string.",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Call an add-on's web API through Home Assistant's Ingress proxy.

        Sends HTTP requests to any add-on that supports Ingress, enabling programmatic
        interaction with add-on APIs like Node-RED Admin API, Frigate HTTP API, etc.

        The request is authenticated via the HA Bearer token and proxied through
        Home Assistant's Ingress system — no direct network access to the add-on is needed.

        **Prerequisites:**
        - Add-on must support Ingress (most add-ons with a web UI do)
        - Add-on must be running
        - Use ha_get_addon(slug="...") to check Ingress support and discover API paths

        **Examples:**
        - Get Node-RED flows: ha_call_addon_api(slug="a0d7b954_nodered", path="/flows")
        - Get Frigate events: ha_call_addon_api(slug="ccab4aaf_frigate", path="/api/events")
        - Deploy Node-RED flows: ha_call_addon_api(slug="a0d7b954_nodered", path="/flows", method="POST", body={...})
        """
        # Validate HTTP method
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        if method.upper() not in valid_methods:
            return create_validation_error(
                f"Invalid HTTP method: {method}. Must be one of: {', '.join(sorted(valid_methods))}",
                parameter="method",
            )

        return await _call_addon_api(
            client=client,
            slug=slug,
            path=path,
            method=method,
            body=body,
        )
