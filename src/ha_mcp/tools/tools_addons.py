"""
Add-on management tools for Home Assistant MCP Server.

Provides tools to list installed and available add-ons via the Supervisor API,
and to call add-on web APIs through Home Assistant's Ingress proxy.

Note: These tools only work with Home Assistant OS or Supervised installations.
"""

import asyncio
import json
import logging
import re
import time
from typing import Annotated, Any
from urllib.parse import unquote

import httpx
import websockets
from fastmcp.exceptions import ToolError
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
    raise_tool_error,
)

logger = logging.getLogger(__name__)

# Maximum response size to return from add-on API calls (50 KB)
_MAX_RESPONSE_SIZE = 50 * 1024

# Maximum number of WebSocket messages to collect
_MAX_WS_MESSAGES = 1000

# ANSI escape code pattern for stripping terminal colors from addon output
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


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
                raise_tool_error(create_error_response(
                    ErrorCode.RESOURCE_NOT_FOUND,
                    "Supervisor API not available",
                    details=str(result),
                    suggestions=[
                        "This feature requires Home Assistant OS or Supervised installation",
                    ],
                ))
            raise_tool_error(create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Supervisor API call failed: {endpoint}",
                details=str(result),
            ))

        return {"success": True, "result": result.get("result", {})}

    except ToolError:
        raise
    except Exception as e:
        logger.error(f"Error calling Supervisor API {endpoint}: {e}")
        exception_to_structured_error(
            e,
            context={"endpoint": endpoint},
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


async def _call_addon_ws(
    client: HomeAssistantClient,
    slug: str,
    path: str,
    body: dict[str, Any] | str | None = None,
    timeout: int = 60,
    debug: bool = False,
    port: int | None = None,
    wait_for_close: bool = True,
) -> dict[str, Any]:
    """Connect to an add-on's WebSocket API and collect messages.

    Args:
        client: Home Assistant REST client
        slug: Add-on slug (e.g., "5c53de3b_esphome")
        path: WebSocket endpoint path (e.g., "/compile", "/validate")
        body: Message to send after connecting (JSON-encoded if dict, raw if string)
        timeout: Max seconds to wait for messages (default 60)
        debug: Include diagnostic info
        port: Override port (same as HTTP tool)
        wait_for_close: If True, collect messages until server closes or timeout.
            If False, return after first batch of messages (up to 2s of silence).

    Returns:
        Dictionary with collected messages, metadata, and status.
    """
    # 1. Sanitize path
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        raise_tool_error(create_validation_error(
            "Path contains '..' traversal component",
            parameter="path",
            details=f"Rejected path: {path}",
        ))

    # 2. Get add-on info
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        raise_tool_error(addon_response)

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)

    # 3. Verify add-on supports Ingress (unless using direct port override)
    if not port and not addon.get("ingress"):
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"Add-on '{addon_name}' does not support Ingress",
            suggestions=[
                "Use the 'port' parameter for WebSocket connections to this add-on",
                f"Use ha_get_addon(slug='{slug}') to see available ports",
            ],
            context={"slug": slug},
        ))

    # 4. Verify add-on is running
    if addon.get("state") != "started":
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Add-on '{addon_name}' is not running (state: {addon.get('state')})",
            suggestions=[
                f"Start the add-on first with: ha_call_service('hassio', 'addon_start', {{'addon': '{slug}'}})",
            ],
            context={"slug": slug, "state": addon.get("state")},
        ))

    # 5. Build WebSocket URL
    addon_ip = addon.get("ip_address", "")
    if port:
        if not addon_ip:
            raise_tool_error(create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Add-on '{addon_name}' is missing ip_address",
                context={"slug": slug},
            ))
        target_port = port
    else:
        ingress_port = addon.get("ingress_port")
        if not addon_ip or not ingress_port:
            raise_tool_error(create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Add-on '{addon_name}' is missing network info",
                context={"slug": slug},
            ))
        target_port = ingress_port

    ws_url = f"ws://{addon_ip}:{target_port}/{normalized}"

    # 6. Build connection headers
    headers: dict[str, str] = {}
    if not port:
        ingress_entry = addon.get("ingress_entry", "")
        headers["X-Ingress-Path"] = ingress_entry
        headers["X-Hass-Source"] = "core.ingress"

    # 7. Connect and exchange messages
    collected: list[str] = []
    total_size = 0
    close_reason = "unknown"
    start_time = time.monotonic()

    try:
        async with websockets.connect(
            ws_url,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=10,
            max_size=5 * 1024 * 1024,  # 5MB max per message
            open_timeout=10,
            close_timeout=5,
        ) as ws:
            # Send initial message if provided
            if body is not None:
                if isinstance(body, dict):
                    await ws.send(json.dumps(body))
                else:
                    await ws.send(str(body))

            # Collect responses
            while True:
                remaining = timeout - (time.monotonic() - start_time)
                if remaining <= 0:
                    close_reason = "timeout"
                    break

                if len(collected) >= _MAX_WS_MESSAGES:
                    close_reason = "message_limit"
                    break

                if total_size >= _MAX_RESPONSE_SIZE:
                    close_reason = "size_limit"
                    break

                try:
                    # If not waiting for close, use a short timeout to detect silence
                    recv_timeout = remaining if wait_for_close else min(remaining, 2.0)
                    message = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
                except TimeoutError:
                    if wait_for_close:
                        close_reason = "timeout"
                    else:
                        close_reason = "silence"
                    break
                except websockets.exceptions.ConnectionClosed:
                    close_reason = "server_closed"
                    break

                # Process message (skip binary frames)
                if isinstance(message, bytes):
                    continue

                # Strip ANSI escape codes
                clean = _ANSI_ESCAPE_RE.sub("", message)
                collected.append(clean)
                total_size += len(clean)

    except websockets.exceptions.InvalidHandshake as e:
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"WebSocket handshake failed with '{addon_name}': {e!s}",
            suggestions=[
                "Check that the add-on supports WebSocket on this path",
                f"Use ha_get_addon(slug='{slug}') to inspect available endpoints",
            ],
            context={"slug": slug, "path": path},
        ))
    except websockets.exceptions.ConnectionClosed as e:
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"WebSocket connection to '{addon_name}' closed unexpectedly: {e!s}",
            suggestions=[
                "The add-on may have rejected the connection or restarted",
                "Try again or check add-on logs for errors",
            ],
            context={"slug": slug, "path": path},
        ))
    except TimeoutError:
        raise_tool_error(create_timeout_error(
            f"WebSocket connection to '{addon_name}'",
            timeout,
            details=f"path={path}",
            context={"slug": slug, "path": path},
        ))
    except OSError as e:
        raise_tool_error(create_connection_error(
            f"Failed to connect to add-on '{addon_name}' WebSocket: {e!s}",
            details="Check that the add-on is running and the port is correct",
            context={"slug": slug},
        ))

    elapsed = round(time.monotonic() - start_time, 2)

    # 8. Build result
    # Try to parse each message as JSON; keep as string if not JSON
    parsed_messages: list[Any] = []
    for msg in collected:
        try:
            parsed_messages.append(json.loads(msg))
        except (json.JSONDecodeError, ValueError):
            parsed_messages.append(msg)

    result: dict[str, Any] = {
        "success": True,
        "messages": parsed_messages,
        "message_count": len(parsed_messages),
        "closed_by": close_reason,
        "duration_seconds": elapsed,
        "addon_name": addon_name,
        "slug": slug,
    }

    if debug:
        result["_debug"] = {
            "ws_url": ws_url,
            "request_headers": dict(headers),
            "initial_message": body,
            "total_bytes_collected": total_size,
        }

    # Cap the serialized result size (raw bytes undercount due to JSON + MCP overhead)
    result_serialized = json.dumps(result, default=str)
    if len(result_serialized) > _MAX_RESPONSE_SIZE:
        result = {
            "success": True,
            "error": "RESPONSE_TOO_LARGE",
            "message": f"WebSocket collected {len(parsed_messages)} messages "
            f"({len(result_serialized)} bytes serialized) exceeding "
            f"{_MAX_RESPONSE_SIZE // 1024}KB limit.",
            "message_count": len(parsed_messages),
            "closed_by": close_reason,
            "duration_seconds": elapsed,
            "addon_name": addon_name,
            "slug": slug,
            "truncated": True,
            "hint": "Use wait_for_close=false for shorter collection, "
            "or use the HTTP endpoint with offset/limit for paginated access.",
        }

    return result


async def _call_addon_api(
    client: HomeAssistantClient,
    slug: str,
    path: str,
    method: str = "GET",
    body: dict[str, Any] | str | None = None,
    timeout: int = 30,
    debug: bool = False,
    port: int | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> dict[str, Any]:
    """Call an add-on's web API through Home Assistant's Ingress proxy.

    Args:
        client: Home Assistant REST client
        slug: Add-on slug (e.g., "a0d7b954_nodered")
        path: API path relative to add-on root (e.g., "/flows")
        method: HTTP method (GET, POST, PUT, DELETE, PATCH)
        body: Request body for POST/PUT/PATCH
        timeout: Request timeout in seconds (default 30)
        port: Override port to connect to (e.g., direct access port instead of ingress port)
        offset: Skip this many items in array responses (default 0)
        limit: Return at most this many items from array responses

    Returns:
        Dictionary with response data, status code, and content type.
    """
    # 1. Sanitize path to prevent traversal attacks (including URL-encoded)
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        raise_tool_error(create_validation_error(
            "Path contains '..' traversal component",
            parameter="path",
            details=f"Rejected path: {path}",
        ))

    # 2. Get add-on info to verify ingress support and get entry path
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        raise_tool_error(addon_response)

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)

    # 3. Verify add-on supports Ingress (unless using direct port override)
    if not port and not addon.get("ingress"):
        raise_tool_error(create_error_response(
            ErrorCode.VALIDATION_FAILED,
            f"Add-on '{addon_name}' does not support Ingress",
            suggestions=[
                "Check if this add-on exposes a direct port instead",
                f"Use ha_get_addon(slug='{slug}') to see port mappings",
                "Use the 'port' parameter to connect to a direct access port",
            ],
            context={"slug": slug},
        ))

    # 4. Verify add-on is running
    if addon.get("state") != "started":
        raise_tool_error(create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            f"Add-on '{addon_name}' is not running (state: {addon.get('state')})",
            suggestions=[
                f"Start the add-on first with: ha_call_service('hassio', 'addon_start', {{'addon': '{slug}'}})",
            ],
            context={"slug": slug, "state": addon.get("state")},
        ))

    # 5. Build URL to the add-on container
    addon_ip = addon.get("ip_address", "")

    if port:
        # Direct port access: connect to the add-on's mapped network port
        # (e.g., 1880 for Node-RED, 6052 for ESPHome) instead of the ingress port.
        # Requires 'leave_front_door_open' or equivalent setting on the add-on.
        if not addon_ip:
            raise_tool_error(create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Add-on '{addon_name}' is missing ip_address",
                context={"slug": slug, "ip_address": addon_ip},
            ))
        target_port = port
    else:
        # Default: use the ingress port for direct container communication
        ingress_port = addon.get("ingress_port")
        if not addon_ip or not ingress_port:
            raise_tool_error(create_error_response(
                ErrorCode.INTERNAL_ERROR,
                f"Add-on '{addon_name}' is missing network info (ip_address or ingress_port)",
                context={"slug": slug, "ip_address": addon_ip, "ingress_port": ingress_port},
            ))
        target_port = ingress_port

    url = f"http://{addon_ip}:{target_port}/{normalized}"

    # 6. Make HTTP request directly to the add-on container
    # Include Ingress headers so the add-on's web server (e.g., Nginx) recognizes
    # this as an authenticated Ingress request and bypasses its own auth layer.
    # When using a direct port, skip Ingress headers (not needed/recognized).
    ingress_entry = addon.get("ingress_entry", "")
    headers: dict[str, str] = {}
    if not port:
        headers["X-Ingress-Path"] = ingress_entry
        headers["X-Hass-Source"] = "core.ingress"

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
        raise_tool_error(create_timeout_error(
            f"add-on API call to '{addon_name}'",
            timeout,
            details=f"path={path}, method={method}",
            context={"slug": slug, "path": path},
        ))
    except httpx.ConnectError as e:
        raise_tool_error(create_connection_error(
            f"Failed to connect to add-on '{addon_name}': {e!s}",
            details="Check that the add-on is running and Home Assistant Ingress is working",
            context={"slug": slug},
        ))

    # 7. Parse response
    content_type = response.headers.get("content-type", "")
    response_data: Any

    if "application/json" in content_type:
        try:
            response_data = response.json()
        except (json.JSONDecodeError, ValueError):
            response_data = response.text
    else:
        response_data = response.text

    # 8. Apply offset/limit slicing to array responses
    pagination_meta: dict[str, Any] | None = None
    if isinstance(response_data, list) and (offset > 0 or limit is not None):
        total_items = len(response_data)
        end = offset + limit if limit is not None else total_items
        response_data = response_data[offset:end]
        pagination_meta = {
            "total_items": total_items,
            "offset": offset,
            "limit": limit,
            "returned": len(response_data),
        }

    # 9. Truncate large responses
    truncated = False
    if isinstance(response_data, str) and len(response_data) > _MAX_RESPONSE_SIZE:
        response_data = response_data[:_MAX_RESPONSE_SIZE]
        truncated = True
    elif isinstance(response_data, list):
        serialized = json.dumps(response_data, default=str)
        if len(serialized) > _MAX_RESPONSE_SIZE:
            total_items = len(response_data)
            response_data = {
                "error": "RESPONSE_TOO_LARGE",
                "message": f"The JSON array ({len(serialized)} bytes, {total_items} items) exceeds the {_MAX_RESPONSE_SIZE // 1024}KB limit.",
                "total_items": total_items,
                "hint": "Use offset and limit to paginate. Example: offset=0, limit=20",
            }
            truncated = True
    elif isinstance(response_data, dict):
        serialized = json.dumps(response_data, default=str)
        if len(serialized) > _MAX_RESPONSE_SIZE:
            # Show top-level keys and their approximate sizes to help caller
            # make more targeted API calls
            key_info = {}
            for k, v in response_data.items():
                v_serialized = json.dumps(v, default=str)
                if isinstance(v, list):
                    key_info[k] = f"array[{len(v)}] ({len(v_serialized)} bytes)"
                elif isinstance(v, dict):
                    key_info[k] = f"object ({len(v_serialized)} bytes)"
                else:
                    key_info[k] = f"{type(v).__name__} ({len(v_serialized)} bytes)"
            response_data = {
                "error": "RESPONSE_TOO_LARGE",
                "message": f"The JSON object ({len(serialized)} bytes) exceeds the {_MAX_RESPONSE_SIZE // 1024}KB limit.",
                "top_level_keys": key_info,
                "hint": "Use a more specific API path to request individual keys/sections.",
            }
            truncated = True

    result: dict[str, Any] = {
        "success": response.status_code < 400,
        "status_code": response.status_code,
        "response": response_data,
        "content_type": content_type,
        "addon_name": addon_name,
        "slug": slug,
    }

    # Include diagnostic info when debug mode is enabled
    if debug:
        result["_debug"] = {
            "url": url,
            "request_headers": dict(headers),
            "response_headers": dict(response.headers),
        }

    if pagination_meta:
        result["pagination"] = pagination_meta

    if truncated:
        result["truncated"] = True
        result["note"] = f"Response truncated to {_MAX_RESPONSE_SIZE // 1024}KB. The full response was larger."

    if response.status_code >= 400:
        result["error"] = f"Add-on API returned HTTP {response.status_code}"
        # On 403/401, include addon config so the LLM can spot relevant settings
        # (e.g., "leave_front_door_open", auth toggles, port mappings)
        if response.status_code in (401, 403):
            addon_options = addon.get("options")
            addon_ports = addon.get("network") or addon.get("ports")
            addon_host_network = addon.get("host_network")
            result["addon_config"] = {
                "options": addon_options,
                "ports": addon_ports,
                "host_network": addon_host_network,
                "ingress_port": addon.get("ingress_port"),
            }
            result["suggestion"] = (
                "This add-on is blocking direct connections (likely Nginx IP restriction). "
                "Try using the 'port' parameter to connect to the add-on's direct access port "
                "(see addon_config.ports above) with 'leave_front_door_open' enabled. "
                "Example: ha_call_addon_api(slug='...', path='...', port=<direct_port>). "
                "The user may need to change add-on settings in the HA UI and restart the add-on."
            )

    return result


def register_addon_tools(mcp: Any, client: HomeAssistantClient, **kwargs: Any) -> None:
    """
    Register add-on management tools with the MCP server.

    Args:
        mcp: FastMCP server instance
        client: Home Assistant REST client
        **kwargs: Additional arguments (ignored, for auto-discovery compatibility)
    """

    @mcp.tool(tags={"Add-ons"}, annotations={"idempotentHint": True, "readOnlyHint": True, "title": "Get Add-ons"})
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
            result = await get_addon_info(client, slug)
            if not result.get("success"):
                raise_tool_error(result)
            return result

        # Default to installed if not specified
        effective_source = (source or "installed").lower()

        if effective_source == "available":
            result = await list_available_addons(client, repository, query)
        elif effective_source == "installed":
            result = await list_addons(client, include_stats)
        else:
            raise_tool_error(create_validation_error(
                f"Invalid source: {source}. Must be 'installed' or 'available'.",
                parameter="source",
                details="Valid sources: installed, available",
            ))

        if not result.get("success"):
            raise_tool_error(result)
        return result

    @mcp.tool(
        tags={"Add-ons"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
            "title": "Call Add-on API",
        },
    )
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
        debug: Annotated[
            bool,
            Field(
                description="Include diagnostic info (request URL, headers sent, response headers). Default: false.",
                default=False,
            ),
        ] = False,
        port: Annotated[
            int | None,
            Field(
                description="Connect to this port instead of the Ingress port. "
                "Use ha_get_addon(slug='...') to find available ports.",
                default=None,
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="HTTP only. Skip this many items in a JSON array response. Default: 0.",
                default=0,
            ),
        ] = 0,
        limit: Annotated[
            int | None,
            Field(
                description="HTTP only. Return at most this many items from a JSON array response (e.g., limit=20).",
                default=None,
            ),
        ] = None,
        websocket: Annotated[
            bool,
            Field(
                description="Use WebSocket instead of HTTP. For streaming endpoints "
                "(e.g., ESPHome /compile, /validate). Sends 'body' as initial message, "
                "collects responses. Default: false.",
                default=False,
            ),
        ] = False,
        wait_for_close: Annotated[
            bool,
            Field(
                description="WebSocket only. True: wait for server to close (for compile/validate). "
                "False: return after first response batch (for quick commands). Default: true.",
                default=True,
            ),
        ] = True,
    ) -> dict[str, Any]:
        """Call an add-on's HTTP or WebSocket API.

        Sends requests directly to add-on containers. Use `websocket=true` for
        streaming endpoints (e.g., ESPHome compile/validate). Use `port` to bypass
        Nginx IP restrictions on community add-ons. Use ha_get_addon(slug="...")
        to discover available ports and endpoints.

        **Examples:**
        - HTTP: ha_call_addon_api(slug="...", path="/api/events")
        - Direct port: ha_call_addon_api(slug="...", path="/flows", port=1880)
        - WebSocket: ha_call_addon_api(slug="...", path="/validate", port=6052, websocket=true, body={"type": "spawn", "configuration": "device.yaml"})
        """
        # WebSocket mode
        if websocket:
            result = await _call_addon_ws(
                client=client,
                slug=slug,
                path=path,
                body=body,
                timeout=120 if wait_for_close else 10,
                debug=debug,
                port=port,
                wait_for_close=wait_for_close,
            )
            if not result.get("success"):
                raise_tool_error(result)
            return result

        # HTTP mode
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        if method.upper() not in valid_methods:
            raise_tool_error(create_validation_error(
                f"Invalid HTTP method: {method}. Must be one of: {', '.join(sorted(valid_methods))}",
                parameter="method",
            ))

        result = await _call_addon_api(
            client=client,
            slug=slug,
            path=path,
            method=method,
            body=body,
            debug=debug,
            port=port,
            offset=offset,
            limit=limit,
        )
        if not result.get("success"):
            raise_tool_error(result)
        return result
