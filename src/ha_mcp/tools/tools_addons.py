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
from ..utils.python_sandbox import PythonSandboxError, safe_execute_expression
from .helpers import (
    exception_to_structured_error,
    get_connected_ws_client,
    log_tool_usage,
    raise_tool_error,
)
from .util_helpers import ANSI_ESCAPE_RE

logger = logging.getLogger(__name__)

# Maximum response size to return from add-on API calls (50 KB)
_MAX_RESPONSE_SIZE = 50 * 1024

# Hard safety cap on WebSocket messages collected per call. `message_limit`
# can lower this but never raise it.
_MAX_WS_MESSAGES = 1000

# Substrings that flag a WebSocket message as "signal" for the summarize pass.
# Keep conservative: false negatives get elided, false positives just mean
# no elision. Case-insensitive match on the JSON-stringified message.
_SIGNAL_PATTERNS = re.compile(
    r"(?:^|[^A-Za-z])(INFO|WARN(?:ING)?|ERROR|FATAL|FAIL(?:ED|URE)?|EXCEPTION|"
    r"TRACEBACK|Configuration is valid|Successfully|unsuccessful|exit|"
    r"returncode|Compiling|Linking)",
    re.IGNORECASE,
)

# Consecutive non-signal messages needed to trigger elision. Below this,
# the run passes through untouched.
_SUMMARIZE_RUN_THRESHOLD = 10

# Messages preserved verbatim at each end of an elided run for context.
_SUMMARIZE_CONTEXT_KEEP = 2


def _slice_ws_messages(
    messages: list[Any],
    offset: int,
    limit: int | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Apply offset/limit to a collected WebSocket message list.

    Returns ``(sliced_messages, pagination_metadata)``. Pagination metadata
    is always returned so the response shape is stable regardless of whether
    offset/limit were applied.
    """
    total_collected = len(messages)
    if offset < 0:
        offset = 0
    if offset > total_collected:
        sliced: list[Any] = []
    elif limit is None:
        sliced = messages[offset:]
    else:
        if limit < 0:
            limit = 0
        sliced = messages[offset : offset + limit]

    pagination: dict[str, Any] = {
        "total_collected": total_collected,
        "offset": offset,
        "returned": len(sliced),
    }
    if limit is not None:
        pagination["limit"] = limit
    return sliced, pagination


def _is_signal_message(msg: Any) -> bool:
    """Return True if ``msg`` looks like a log line or terminal event worth keeping.

    The heuristic errs toward keeping messages — false positives just mean
    a run doesn't get elided.
    """
    if isinstance(msg, (dict, list)):
        serialized = json.dumps(msg, default=str)
    else:
        serialized = str(msg)
    return bool(_SIGNAL_PATTERNS.search(serialized[:2000]))


def _summarize_ws_messages(
    messages: list[Any],
    *,
    run_threshold: int = _SUMMARIZE_RUN_THRESHOLD,
    context_keep: int = _SUMMARIZE_CONTEXT_KEEP,
) -> tuple[list[Any], dict[str, Any]]:
    """Collapse runs of non-signal WebSocket messages into elision markers.

    Each run of ≥ ``run_threshold`` consecutive non-signal entries becomes:
    ``context_keep`` originals, one elision dict
    ``{"elided": N, "note": "..."}``, then ``context_keep`` originals.
    Signal messages always pass through unchanged.
    """
    result: list[Any] = []
    run_start: int | None = None
    elided_total = 0

    def flush(run_end: int) -> None:
        nonlocal elided_total
        assert run_start is not None
        run_len = run_end - run_start
        if run_len >= run_threshold:
            result.extend(messages[run_start : run_start + context_keep])
            elided_count = run_len - 2 * context_keep
            result.append(
                {
                    "elided": elided_count,
                    "note": (
                        f"{elided_count} non-signal messages elided; "
                        "pass summarize=False for full output"
                    ),
                }
            )
            result.extend(messages[run_end - context_keep : run_end])
            elided_total += elided_count
        else:
            result.extend(messages[run_start:run_end])

    for i, msg in enumerate(messages):
        if _is_signal_message(msg):
            if run_start is not None:
                flush(i)
                run_start = None
            result.append(msg)
        else:
            if run_start is None:
                run_start = i

    if run_start is not None:
        flush(len(messages))

    return result, {
        "original_count": len(messages),
        "summarized_count": len(result),
        "elided_count": elided_total,
    }


def _apply_response_transform(response: Any, expr: str) -> Any:
    """Run a sandboxed ``python_transform`` expression against ``response``.

    Exposes the value to the expression as ``response``. Supports both
    in-place mutation and reassignment (``response = [...]``). Raises
    ToolError with VALIDATION_FAILED on sandbox errors so the agent gets
    a structured code it can react to.
    """
    try:
        return safe_execute_expression(expr, {"response": response}, "response")
    except PythonSandboxError as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                f"python_transform failed: {e!s}",
                context={"expression_preview": expr[:200]},
                suggestions=[
                    "Operate on the `response` variable (in-place or reassign)",
                    "Allowed: dict/list access, assignment, loops, "
                    "comprehensions, whitelisted str/list/dict methods",
                ],
            )
        )


def _merge_options(base: dict, override: dict) -> dict:
    """Merge caller options into current options with one-level deep merge.

    Top-level scalar values are replaced. Top-level dict values are merged
    one level deep so callers can update a single nested field (e.g.
    ``{"ssh": {"sftp": True}}``) without losing sibling fields.
    """
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


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
                raise_tool_error(
                    create_error_response(
                        ErrorCode.RESOURCE_NOT_FOUND,
                        "Supervisor API not available",
                        details=str(result),
                        suggestions=[
                            "This feature requires Home Assistant OS or Supervised installation",
                        ],
                    )
                )
            raise_tool_error(
                create_error_response(
                    ErrorCode.SERVICE_CALL_FAILED,
                    f"Supervisor API call failed: {endpoint}",
                    details=str(result),
                )
            )

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


async def get_addon_info(client: HomeAssistantClient, slug: str) -> dict[str, Any]:
    """Get detailed info for a specific add-on.

    Args:
        client: Home Assistant REST client
        slug: Add-on slug (e.g., "a0d7b954_nodered")

    Returns:
        Dictionary with add-on details including ingress info, state, options, etc.
    """
    response = await _supervisor_api_call(client, f"/addons/{slug}/info")
    if not response.get("success"):
        return response  # TODO(tech-debt): should raise ToolError per AGENTS.md Pattern B
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
        return response  # TODO(tech-debt): should raise ToolError per AGENTS.md Pattern B

    data = response["result"]
    addons = data.get("addons", [])

    # Fetch stats for running addons in parallel to avoid sequential overhead
    stats_by_slug: dict[str, dict[str, Any] | None] = {}
    if include_stats:
        running_slugs = [a.get("slug") for a in addons if a.get("state") == "started"]

        async def _fetch_stats(slug: str) -> tuple[str, dict[str, Any] | None]:
            try:
                resp = await _supervisor_api_call(client, f"/addons/{slug}/stats")
                if resp.get("success"):
                    s = resp["result"]
                    return slug, {
                        "cpu_percent": s.get("cpu_percent"),
                        "memory_percent": s.get("memory_percent"),
                        "memory_usage": s.get("memory_usage"),
                        "memory_limit": s.get("memory_limit"),
                    }
            except Exception as exc:
                logger.warning("Failed to fetch stats for addon %s: %s", slug, exc)
            return slug, None

        results = await asyncio.gather(*[_fetch_stats(slug) for slug in running_slugs])
        stats_by_slug = dict(results)

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

        if include_stats:
            addon_info["stats"] = stats_by_slug.get(addon.get("slug"))

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
    message_limit: int | None = None,
    message_offset: int = 0,
    summarize: bool = True,
    python_transform: str | None = None,
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
        message_limit: Cap on messages collected from the wire. Bounded by the
            hard ceiling ``_MAX_WS_MESSAGES``. None means "collect up to the
            ceiling" (legacy behavior).
        message_offset: Drop this many messages from the start of the collected
            list before returning. Useful for paginating past a known-noisy
            header when re-running the same call.
        summarize: When True (default), collapse runs of non-signal messages
            (typically YAML config dumps) into short elision markers. Set to
            False to return the raw stream.
        python_transform: Optional sandboxed Python expression that post-
            processes the response. The variable ``response`` is bound to
            the list of parsed messages (``list[dict | str]``); the value
            of ``response`` after execution replaces ``messages`` in the
            output. See ``ha_manage_addon`` docstring for details.

    Returns:
        Dictionary with collected messages, metadata, and status.
    """
    # 1. Sanitize path
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        raise_tool_error(
            create_validation_error(
                "Path contains '..' traversal component",
                parameter="path",
                details=f"Rejected path: {path}",
            )
        )

    # 2. Get add-on info
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        raise_tool_error(addon_response)

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)

    # 3. Verify add-on supports Ingress (unless using direct port override)
    if not port and not addon.get("ingress"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                f"Add-on '{addon_name}' does not support Ingress",
                suggestions=[
                    "Use the 'port' parameter for WebSocket connections to this add-on",
                    f"Use ha_get_addon(slug='{slug}') to see available ports",
                ],
                context={"slug": slug},
            )
        )

    # 4. Verify add-on is running
    if addon.get("state") != "started":
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Add-on '{addon_name}' is not running (state: {addon.get('state')})",
                suggestions=[
                    f"Start the add-on first with: ha_call_service('hassio', 'addon_start', {{'addon': '{slug}'}})",
                ],
                context={"slug": slug, "state": addon.get("state")},
            )
        )

    # 5. Build WebSocket URL
    addon_ip = addon.get("ip_address", "")
    if port:
        if not addon_ip:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Add-on '{addon_name}' is missing ip_address",
                    context={"slug": slug},
                )
            )
        target_port = port
    else:
        ingress_port = addon.get("ingress_port")
        if not addon_ip or not ingress_port:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Add-on '{addon_name}' is missing network info",
                    context={"slug": slug},
                )
            )
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

    # Effective collection cap: callers may lower _MAX_WS_MESSAGES via
    # message_limit but cannot raise it. A caller's message_limit interacts
    # with message_offset — we collect enough to satisfy `offset + limit`
    # so requesting a later window actually returns the window.
    if message_limit is None:
        collection_cap = _MAX_WS_MESSAGES
    else:
        requested = max(0, message_offset) + max(0, message_limit)
        collection_cap = min(_MAX_WS_MESSAGES, requested)

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

                if len(collected) >= collection_cap:
                    # Distinguish caller-set cap from the global safety ceiling
                    # so an agent reading the response can tell "I capped this"
                    # from "ha-mcp's hard ceiling kicked in".
                    close_reason = (
                        "message_limit"
                        if message_limit is not None
                        else "safety_ceiling"
                    )
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
                clean = ANSI_ESCAPE_RE.sub("", message)
                collected.append(clean)
                total_size += len(clean)

    except websockets.exceptions.InvalidHandshake as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"WebSocket handshake failed with '{addon_name}': {e!s}",
                suggestions=[
                    "Check that the add-on supports WebSocket on this path",
                    f"Use ha_get_addon(slug='{slug}') to inspect available endpoints",
                ],
                context={"slug": slug, "path": path},
            )
        )
    except websockets.exceptions.ConnectionClosed as e:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"WebSocket connection to '{addon_name}' closed unexpectedly: {e!s}",
                suggestions=[
                    "The add-on may have rejected the connection or restarted",
                    "Try again or check add-on logs for errors",
                ],
                context={"slug": slug, "path": path},
            )
        )
    except TimeoutError:
        raise_tool_error(
            create_timeout_error(
                f"WebSocket connection to '{addon_name}'",
                timeout,
                details=f"path={path}",
                context={"slug": slug, "path": path},
            )
        )
    except OSError as e:
        raise_tool_error(
            create_connection_error(
                f"Failed to connect to add-on '{addon_name}' WebSocket: {e!s}",
                details="Check that the add-on is running and the port is correct",
                context={"slug": slug},
            )
        )

    elapsed = round(time.monotonic() - start_time, 2)

    # 8. Build result
    # Try to parse each message as JSON; keep as string if not JSON.
    # Result shape is list[dict | str] — the heterogeneity is part of the
    # python_transform contract (see ha_manage_addon docstring).
    parsed_messages: list[Any] = []
    for msg in collected:
        try:
            parsed_messages.append(json.loads(msg))
        except (json.JSONDecodeError, ValueError):
            parsed_messages.append(msg)

    # 8a. Apply offset/limit slicing before summarize/transform so users
    # paginate the raw collected list, not the post-summarize output.
    sliced_messages, pagination = _slice_ws_messages(
        parsed_messages,
        offset=message_offset,
        limit=message_limit,
    )

    # 8b. Summarize (default on) — collapse bulk non-signal runs.
    summary_meta: dict[str, Any] | None = None
    processed_messages: list[Any] = sliced_messages
    if summarize:
        processed_messages, summary_meta = _summarize_ws_messages(sliced_messages)

    # 8c. python_transform (optional) — user-controlled post-processing.
    transformed = False
    pre_transform_count = len(processed_messages)
    if python_transform is not None:
        processed_messages = _apply_response_transform(
            processed_messages,
            python_transform,
        )
        transformed = True

    result: dict[str, Any] = {
        "success": True,
        "messages": processed_messages,
        "message_count": (
            len(processed_messages) if isinstance(processed_messages, list) else None
        ),
        "closed_by": close_reason,
        "duration_seconds": elapsed,
        "addon_name": addon_name,
        "slug": slug,
    }

    # Pagination metadata is always present when offset/limit were used so
    # callers have a stable shape to reason about.
    if message_offset > 0 or message_limit is not None:
        result["pagination"] = pagination

    if summary_meta is not None and summary_meta["elided_count"] > 0:
        result["summary"] = summary_meta

    if transformed:
        result["transformed"] = True
        result["pre_transform_message_count"] = pre_transform_count

    if debug:
        result["_debug"] = {
            "ws_url": ws_url,
            "request_headers": dict(headers),
            "initial_message": body,
            "total_bytes_collected": total_size,
            "collection_cap": collection_cap,
        }

    # Cap the serialized result size (raw bytes undercount due to JSON + MCP overhead)
    result_serialized = json.dumps(result, default=str)
    if len(result_serialized) > _MAX_RESPONSE_SIZE:
        result = {
            "success": True,
            "error": "RESPONSE_TOO_LARGE",
            "message": f"WebSocket response ({len(result_serialized)} bytes "
            f"serialized) exceeds {_MAX_RESPONSE_SIZE // 1024}KB limit.",
            "message_count": (
                len(processed_messages)
                if isinstance(processed_messages, list)
                else None
            ),
            "closed_by": close_reason,
            "duration_seconds": elapsed,
            "addon_name": addon_name,
            "slug": slug,
            "truncated": True,
            "hint": "Lower message_limit, raise message_offset, keep summarize=True, "
            "or narrow the response with python_transform.",
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
    python_transform: str | None = None,
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
        python_transform: Optional sandboxed Python expression applied to the
            parsed response body. The variable ``response`` is bound to
            ``dict | list | str`` depending on content-type. Transform runs
            after offset/limit slicing.

    Returns:
        Dictionary with response data, status code, and content type.
    """
    # 1. Sanitize path to prevent traversal attacks (including URL-encoded)
    normalized = unquote(path).lstrip("/")
    if ".." in normalized.split("/"):
        raise_tool_error(
            create_validation_error(
                "Path contains '..' traversal component",
                parameter="path",
                details=f"Rejected path: {path}",
            )
        )

    # 2. Get add-on info to verify ingress support and get entry path
    addon_response = await get_addon_info(client, slug)
    if not addon_response.get("success"):
        raise_tool_error(addon_response)

    addon = addon_response["addon"]
    addon_name = addon.get("name", slug)

    # 3. Verify add-on supports Ingress (unless using direct port override)
    if not port and not addon.get("ingress"):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_FAILED,
                f"Add-on '{addon_name}' does not support Ingress",
                suggestions=[
                    "Check if this add-on exposes a direct port instead",
                    f"Use ha_get_addon(slug='{slug}') to see port mappings",
                    "Use the 'port' parameter to connect to a direct access port",
                ],
                context={"slug": slug},
            )
        )

    # 4. Verify add-on is running
    if addon.get("state") != "started":
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                f"Add-on '{addon_name}' is not running (state: {addon.get('state')})",
                suggestions=[
                    f"Start the add-on first with: ha_call_service('hassio', 'addon_start', {{'addon': '{slug}'}})",
                ],
                context={"slug": slug, "state": addon.get("state")},
            )
        )

    # 5. Build URL to the add-on container
    addon_ip = addon.get("ip_address", "")

    if port:
        # Direct port access: connect to the add-on's mapped network port
        # (e.g., 1880 for Node-RED, 6052 for ESPHome) instead of the ingress port.
        # Requires 'leave_front_door_open' or equivalent setting on the add-on.
        if not addon_ip:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Add-on '{addon_name}' is missing ip_address",
                    context={"slug": slug, "ip_address": addon_ip},
                )
            )
        target_port = port
    else:
        # Default: use the ingress port for direct container communication
        ingress_port = addon.get("ingress_port")
        if not addon_ip or not ingress_port:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Add-on '{addon_name}' is missing network info (ip_address or ingress_port)",
                    context={
                        "slug": slug,
                        "ip_address": addon_ip,
                        "ingress_port": ingress_port,
                    },
                )
            )
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
        raise_tool_error(
            create_timeout_error(
                f"add-on API call to '{addon_name}'",
                timeout,
                details=f"path={path}, method={method}",
                context={"slug": slug, "path": path},
            )
        )
    except httpx.ConnectError as e:
        raise_tool_error(
            create_connection_error(
                f"Failed to connect to add-on '{addon_name}': {e!s}",
                details="Check that the add-on is running and Home Assistant Ingress is working",
                context={"slug": slug},
            )
        )

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

    # 8a. python_transform (optional) — runs after slicing, before size cap,
    # so an agent can narrow a large response down under the limit.
    transformed = False
    if python_transform is not None:
        response_data = _apply_response_transform(response_data, python_transform)
        transformed = True

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

    if transformed:
        result["transformed"] = True

    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Response truncated to {_MAX_RESPONSE_SIZE // 1024}KB. The full response was larger."
        )

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
                "Example: ha_manage_addon(slug='...', path='...', port=<direct_port>). "
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

    @mcp.tool(
        tags={"Add-ons"},
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "title": "Get Add-ons",
        },
    )
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
        Useful for discovering what APIs an add-on exposes before calling ha_manage_addon.

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
            raise_tool_error(
                create_validation_error(
                    f"Invalid source: {source}. Must be 'installed' or 'available'.",
                    parameter="source",
                    details="Valid sources: installed, available",
                )
            )

        if not result.get("success"):
            raise_tool_error(result)
        return result

    @mcp.tool(
        tags={"Add-ons"},
        annotations={
            "destructiveHint": True,
            "idempotentHint": False,
            "readOnlyHint": False,
            "title": "Manage Add-on",
        },
    )
    @log_tool_usage
    async def ha_manage_addon(
        slug: Annotated[
            str,
            Field(
                description="Add-on slug (e.g., 'a0d7b954_nodered', 'ccab4aaf_frigate'). "
                "Use ha_get_addon() to find installed add-on slugs.",
            ),
        ],
        path: Annotated[
            str | None,
            Field(
                description="Proxy mode: API path relative to the add-on root "
                "(e.g., '/flows', '/api/events', '/api/stats'). "
                "Required for proxy mode; mutually exclusive with config parameters.",
                default=None,
            ),
        ] = None,
        method: Annotated[
            str,
            Field(
                description="Proxy mode only. HTTP method: GET, POST, PUT, DELETE, PATCH. Defaults to GET.",
                default="GET",
            ),
        ] = "GET",
        body: Annotated[
            dict[str, Any] | str | None,
            Field(
                description="Proxy mode only. Request body for POST/PUT/PATCH. Pass a JSON object or JSON string.",
                default=None,
            ),
        ] = None,
        debug: Annotated[
            bool,
            Field(
                description="Proxy mode only. Include diagnostic info (request URL, headers sent, response headers). Default: false.",
                default=False,
            ),
        ] = False,
        port: Annotated[
            int | None,
            Field(
                description="Proxy mode only. Connect to this port instead of the Ingress port. "
                "Use ha_get_addon(slug='...') to find available ports.",
                default=None,
            ),
        ] = None,
        offset: Annotated[
            int,
            Field(
                description="Proxy mode only. HTTP: skip this many items in a JSON array response. Default: 0.",
                default=0,
            ),
        ] = 0,
        limit: Annotated[
            int | None,
            Field(
                description="Proxy mode only. HTTP: return at most this many items from a JSON array response.",
                default=None,
            ),
        ] = None,
        websocket: Annotated[
            bool,
            Field(
                description="Proxy mode only. Use WebSocket instead of HTTP. For streaming endpoints "
                "(e.g., ESPHome /compile, /validate). Sends 'body' as initial message, "
                "collects responses. Default: false.",
                default=False,
            ),
        ] = False,
        wait_for_close: Annotated[
            bool,
            Field(
                description="Proxy mode only. WebSocket: True: wait for server to close (for compile/validate). "
                "False: return after first response batch (for quick commands). Default: true.",
                default=True,
            ),
        ] = True,
        message_limit: Annotated[
            int | None,
            Field(
                description="Proxy mode only. WebSocket: cap on messages collected from the wire, "
                "bounded by an internal safety ceiling. None = collect up to the ceiling. "
                "Lower to save tokens on noisy streams (e.g., message_limit=50 for a quick health check).",
                default=None,
            ),
        ] = None,
        message_offset: Annotated[
            int,
            Field(
                description="Proxy mode only. WebSocket: drop this many messages from the start of the "
                "collected list before returning. Useful for paginating past known-noisy headers. Default: 0.",
                default=0,
            ),
        ] = 0,
        summarize: Annotated[
            bool,
            Field(
                description="Proxy mode only. WebSocket: when True (default), collapse runs of "
                "non-signal messages (typically YAML config dumps) into short elision markers. "
                "Set to False to return the raw stream.",
                default=True,
            ),
        ] = True,
        python_transform: Annotated[
            str | None,
            Field(
                description="Proxy mode only. Sandboxed Python expression that post-processes the response. "
                "Variable `response` is exposed — a list[dict | str] for WebSocket (parsed JSON or raw text), "
                "or dict/list/str for HTTP (parsed body). Supports in-place mutation "
                "(response.append(...)) or reassignment (response = [...]). "
                "Example: response = [m for m in response if 'ERROR' in str(m)]. "
                "Post-processing only — does not provide optimistic-locking write semantics.",
                default=None,
            ),
        ] = None,
        options: Annotated[
            dict[str, Any] | None,
            Field(
                description="Config mode: Add-on configuration values (the 'Configuration' tab in the UI).",
                default=None,
            ),
        ] = None,
        network: Annotated[
            dict[str, Any] | None,
            Field(
                description="Config mode: Host port mappings (e.g., {'5800/tcp': 8081}).",
                default=None,
            ),
        ] = None,
        boot: Annotated[
            str | None,
            Field(
                description="Config mode: Boot strategy — 'auto' (start with HA) or 'manual'.",
                default=None,
            ),
        ] = None,
        auto_update: Annotated[
            bool | None,
            Field(
                description="Config mode: Enable or disable automatic updates for this add-on.",
                default=None,
            ),
        ] = None,
        watchdog: Annotated[
            bool | None,
            Field(
                description="Config mode: Enable or disable Supervisor watchdog (auto-restart on crash).",
                default=None,
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Manage a Home Assistant add-on — update its configuration or call its internal API.

        Two mutually exclusive operating modes:

        **Config mode** (when any of options/network/boot/auto_update/watchdog is provided):
        Updates the add-on's Supervisor configuration via POST /addons/{slug}/options.
        All config parameters are optional; only provided fields are updated — current values
        are fetched and merged automatically (including one level of nested dicts).

        **Proxy mode** (when path is provided):
        Sends requests directly to the add-on container's own web API via HTTP or WebSocket.
        Use ha_get_addon(slug="...") to discover available ports and endpoints.

        **Response shaping (proxy mode):**
        - WebSocket streams can be noisy (ESPHome /validate often emits hundreds of
          config-dump lines). By default, `summarize=True` collapses long runs of
          non-signal messages into short elision markers; INFO/WARNING/ERROR/exit
          lines always pass through. Pagination via `message_offset` / `message_limit`
          works on the raw collected list before summarize runs.
        - `python_transform` applies a sandboxed Python expression as a final
          post-processing step in both HTTP and WebSocket modes. The variable
          `response` is bound to:
            * WebSocket: `list[dict | str]` — parsed JSON messages are dicts,
              undecodable frames stay as ANSI-stripped strings. Elision markers
              appear as `{"elided": N, "note": "..."}` dicts when summarize ran.
            * HTTP: `dict | list | str` — whichever the content-type produced.
          Transforms may mutate in place (response.append(...), del response[k])
          or reassign (response = [...]). This is post-processing only — it does
          NOT provide optimistic-locking or write-back semantics.

        **WARNING:** Setting boot="auto"/"manual" will fail for add-ons whose Supervisor
        metadata locks the boot mode. The Supervisor returns an error in this case.

        **NOTE:** This tool only works with Home Assistant OS or Supervised installations.

        **Examples:**
        - Set add-on option: ha_manage_addon(slug="...", options={"log_level": "debug"})
          Note: only the fields you provide are updated — current values are fetched first
          and merged automatically. Fields not in the add-on's schema are ignored with a warning.
        - Disable auto-update: ha_manage_addon(slug="...", auto_update=False)
        - Change host port: ha_manage_addon(slug="...", network={"5800/tcp": 8082})
        - Set boot mode: ha_manage_addon(slug="...", boot="manual")
        - Call HTTP API: ha_manage_addon(slug="...", path="/api/events")
        - Direct port: ha_manage_addon(slug="...", path="/flows", port=1880)
        - WebSocket: ha_manage_addon(slug="...", path="/validate", port=6052, websocket=True, body={"type": "spawn", "configuration": "device.yaml"})
        - Quick WS health check (50 msgs, raw): ha_manage_addon(slug="...", path="/logs", websocket=True, message_limit=50, summarize=False)
        - Filter WS errors only: ha_manage_addon(slug="...", path="/validate", websocket=True, python_transform="response = [m for m in response if 'ERROR' in str(m) or 'WARN' in str(m)]")
        - HTTP subset: ha_manage_addon(slug="...", path="/flows", python_transform="response = [f['id'] for f in response]")
        """
        # Build config payload from provided config parameters
        config_data: dict[str, Any] = {}
        if options:
            config_data["options"] = options
        if network:
            config_data["network"] = network
        if boot is not None:
            config_data["boot"] = boot
        if auto_update is not None:
            config_data["auto_update"] = auto_update
        if watchdog is not None:
            config_data["watchdog"] = watchdog

        # Validate mode selection
        if path is not None and path == "":
            raise_tool_error(
                create_validation_error(
                    "'path' must not be empty. Provide a non-empty path for proxy mode "
                    "(e.g., '/api/events') or omit it to use config mode.",
                    parameter="path",
                )
            )
        if path is not None and config_data:
            raise_tool_error(
                create_validation_error(
                    "Cannot combine 'path' (proxy mode) with config parameters "
                    "(options/network/boot/auto_update/watchdog). Use one mode at a time.",
                    parameter="path",
                )
            )
        if not path and not config_data:
            raise_tool_error(
                create_validation_error(
                    "Must provide either 'path' for proxy mode or at least one config parameter "
                    "(options/network/boot/auto_update/watchdog) for config mode.",
                    parameter="path",
                )
            )

        # Validate that proxy-only params are not passed in config mode
        if config_data:
            proxy_overrides: list[tuple[str, str]] = []
            if method != "GET":
                proxy_overrides.append(("method", f"method={method!r}"))
            if body is not None:
                proxy_overrides.append(("body", "body"))
            if debug:
                proxy_overrides.append(("debug", "debug=True"))
            if port is not None:
                proxy_overrides.append(("port", f"port={port}"))
            if offset != 0:
                proxy_overrides.append(("offset", f"offset={offset}"))
            if limit is not None:
                proxy_overrides.append(("limit", f"limit={limit}"))
            if websocket:
                proxy_overrides.append(("websocket", "websocket=True"))
            if not wait_for_close:
                proxy_overrides.append(("wait_for_close", "wait_for_close=False"))
            if message_limit is not None:
                proxy_overrides.append(
                    ("message_limit", f"message_limit={message_limit}")
                )
            if message_offset != 0:
                proxy_overrides.append(
                    ("message_offset", f"message_offset={message_offset}")
                )
            if not summarize:
                proxy_overrides.append(("summarize", "summarize=False"))
            if python_transform is not None:
                proxy_overrides.append(("python_transform", "python_transform"))
            if proxy_overrides:
                raise_tool_error(
                    create_validation_error(
                        f"Proxy-mode parameters cannot be used in config mode: {', '.join(d for _, d in proxy_overrides)}. "
                        "Remove these parameters or switch to proxy mode by providing 'path'.",
                        parameter=proxy_overrides[0][0],
                    )
                )

        # Config mode: update Supervisor settings
        if config_data:
            ignored_fields: list[str] = []  # populated only when options are provided
            # For options updates: fetch current state first.
            # GET /info provides both current options (for merge) and schema_ui
            # (for pre-write unknown-field detection) in a single roundtrip.
            if "options" in config_data:
                info_result = await _supervisor_api_call(client, f"/addons/{slug}/info")
                if not info_result.get("success"):
                    raise_tool_error(
                        create_error_response(
                            ErrorCode.RESOURCE_NOT_FOUND,
                            f"Add-on '{slug}' not found or Supervisor unavailable",
                            details=str(info_result),
                        )
                    )
                addon_info = info_result.get("result", {})

                # Merge caller's options into current options (fixes partial-update rejection).
                # Supervisor validates the full options dict against the add-on schema,
                # so callers must always submit all required fields — merging makes that
                # transparent.
                current_options: dict = addon_info.get("options") or {}
                merged_options = _merge_options(current_options, config_data["options"])

                # Pre-write schema check: identify fields not in the add-on's schema.
                # Supervisor silently drops unknown fields on write; surfacing them here
                # lets the caller correct mistakes before any state is changed.
                schema_ui: list | None = addon_info.get("schema")
                if schema_ui is not None:
                    allowed_keys = {item["name"] for item in schema_ui if "name" in item}
                    ignored_fields = [k for k in config_data["options"] if k not in allowed_keys]
                    # Remove unknown fields from the merged dict so Supervisor does not
                    # silently strip them after the write succeeds.
                    for k in ignored_fields:
                        merged_options.pop(k, None)

                config_data["options"] = merged_options

            result = await _supervisor_api_call(
                client,
                f"/addons/{slug}/options",
                method="POST",
                data=config_data,
            )
            if not result.get("success"):
                # Surface Supervisor schema errors (e.g. missing required field) as
                # VALIDATION_FAILED so the model receives an actionable error code.
                error_detail = str(result)
                raise_tool_error(
                    create_error_response(
                        ErrorCode.VALIDATION_FAILED,
                        f"Supervisor rejected configuration for add-on '{slug}'",
                        details=error_detail,
                        suggestions=[
                            "Fetch current options via ha_get_addon(slug) to see required fields",
                            "Re-submit all required option fields together",
                        ],
                    )
                )
            submitted_fields = list(config_data.keys())
            response: dict = {}
            if {"options", "network"} & config_data.keys():
                response = {
                    "status": "pending_restart",
                    "message": (
                        f"Configuration submitted for add-on '{slug}'. "
                        "Restart the add-on for options/network changes to take effect."
                    ),
                    "submitted_fields": submitted_fields,
                }
            else:
                response = {
                    "success": True,
                    "message": f"Configuration updated for add-on '{slug}'.",
                    "submitted_fields": submitted_fields,
                }
            if ignored_fields:
                response["warning"] = (
                    f"{len(ignored_fields)} field(s) not in add-on schema were ignored "
                    f"before write: {ignored_fields}. Use ha_get_addon(slug) to see the "
                    "declared schema."
                )
                response["ignored_fields"] = ignored_fields
            return response

        # Proxy mode: call add-on container API
        # At this point path is guaranteed non-None (validated above)
        assert path is not None
        # WebSocket
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
                message_limit=message_limit,
                message_offset=message_offset,
                summarize=summarize,
                python_transform=python_transform,
            )
            if not result.get("success"):
                raise_tool_error(result)
            return result

        # HTTP
        valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH"}
        if method.upper() not in valid_methods:
            raise_tool_error(
                create_validation_error(
                    f"Invalid HTTP method: {method}. Must be one of: {', '.join(sorted(valid_methods))}",
                    parameter="method",
                )
            )

        # HTTP mode does not use WebSocket-specific params. Reject explicit
        # use so misroutes surface immediately rather than silently ignoring.
        if message_limit is not None or message_offset != 0 or not summarize:
            raise_tool_error(
                create_validation_error(
                    "message_limit / message_offset / summarize apply only to "
                    "WebSocket mode. Set websocket=True or remove them.",
                    parameter="message_limit",
                )
            )

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
            python_transform=python_transform,
        )
        if not result.get("success"):
            raise_tool_error(result)
        return result
