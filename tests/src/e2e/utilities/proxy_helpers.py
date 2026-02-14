"""
Proxy-aware tool calling helpers for E2E tests.

When tools are proxied behind meta-tools (ha_find_tools, ha_get_tool_details,
ha_execute_tool), E2E tests need to route through the proxy instead of calling
tools directly.

This module provides a transparent wrapper that detects proxied tools and routes
them through ha_execute_tool automatically.
"""

import json
import logging
from typing import Any

from .assertions import parse_mcp_result

logger = logging.getLogger(__name__)

# Cache schema hashes to avoid repeated ha_get_tool_details calls
_schema_hash_cache: dict[str, str] = {}


async def proxy_call_tool(
    mcp_client: Any,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    """Call a proxied tool through the ha_execute_tool meta-tool.

    This function:
    1. Calls ha_get_tool_details to get the schema_hash (cached)
    2. Calls ha_execute_tool with the tool_name, args, and schema_hash

    Returns the raw FastMCP CallToolResult, compatible with parse_mcp_result()
    and assert_mcp_success().

    Args:
        mcp_client: FastMCP Client instance
        tool_name: Name of the proxied tool (e.g., "ha_config_get_label")
        args: Tool arguments dict (default: empty dict)

    Returns:
        FastMCP CallToolResult from ha_execute_tool
    """
    if args is None:
        args = {}

    # Get schema hash (cached for efficiency)
    if tool_name not in _schema_hash_cache:
        details_result = await mcp_client.call_tool(
            "ha_get_tool_details", {"tool_name": tool_name}
        )
        details = parse_mcp_result(details_result)
        if not details.get("success"):
            logger.error(
                f"Failed to get details for {tool_name}: {details}"
            )
            return details_result
        _schema_hash_cache[tool_name] = details["schema_hash"]

    schema_hash = _schema_hash_cache[tool_name]

    # Execute through proxy
    return await mcp_client.call_tool(
        "ha_execute_tool",
        {
            "tool_name": tool_name,
            "args": json.dumps(args),
            "tool_schema": schema_hash,
        },
    )
