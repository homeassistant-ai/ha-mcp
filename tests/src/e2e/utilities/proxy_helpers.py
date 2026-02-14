"""
Proxy-aware tool calling helpers for E2E tests.

When tools are proxied behind meta-tools (ha_find_tools, ha_get_tool_details,
ha_execute_tool), E2E tests need to route through the proxy instead of calling
tools directly.

This module provides a transparent wrapper that detects proxied tools and routes
them through ha_execute_tool automatically. Non-proxied tools fall back to
direct mcp_client.call_tool() calls.
"""

import json
import logging
from typing import Any

from .assertions import (
    MCPAssertions,
    assert_mcp_failure,
    assert_mcp_success,
    parse_mcp_result,
    tool_error_to_result,
)

logger = logging.getLogger(__name__)

# Cache schema hashes to avoid repeated ha_get_tool_details calls
_schema_hash_cache: dict[str, str] = {}

# Cache tools confirmed as non-proxied (direct MCP registration)
_direct_tools: set[str] = set()


async def proxy_call_tool(
    mcp_client: Any,
    tool_name: str,
    args: dict[str, Any] | None = None,
) -> Any:
    """Call a tool transparently through the proxy or directly.

    For proxied tools:
    1. Calls ha_get_tool_details to get the schema_hash (cached)
    2. Calls ha_execute_tool with the tool_name, args, and schema_hash

    For non-proxied tools (ha_get_tool_details returns not found):
    Falls back to direct mcp_client.call_tool() call.

    Returns the raw FastMCP CallToolResult, compatible with parse_mcp_result()
    and assert_mcp_success().

    Args:
        mcp_client: FastMCP Client instance
        tool_name: Name of the tool (e.g., "ha_config_get_label")
        args: Tool arguments dict (default: empty dict)

    Returns:
        FastMCP CallToolResult
    """
    if args is None:
        args = {}

    # Known non-proxied tool — call directly
    if tool_name in _direct_tools:
        return await mcp_client.call_tool(tool_name, args)

    # Get schema hash (cached for efficiency)
    if tool_name not in _schema_hash_cache:
        details_result = await mcp_client.call_tool(
            "ha_get_tool_details", {"tool_name": tool_name}
        )
        details = parse_mcp_result(details_result)
        if not details.get("success"):
            # Tool is not proxied — fall back to direct call
            _direct_tools.add(tool_name)
            logger.debug(f"{tool_name} is not proxied, calling directly")
            return await mcp_client.call_tool(tool_name, args)
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


class ProxyMCPAssertions(MCPAssertions):
    """MCPAssertions that routes all tool calls through the proxy transparently.

    Drop-in replacement for MCPAssertions — proxied tools go through
    ha_execute_tool, non-proxied tools fall back to direct calls.
    """

    async def call_tool_success(
        self, tool_name: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Call tool through proxy and assert success."""
        from fastmcp.exceptions import ToolError

        try:
            result = await proxy_call_tool(self.client, tool_name, params)
            return assert_mcp_success(
                result, f"{tool_name}({list(params.keys())})"
            )
        except ToolError as exc:
            error_data = tool_error_to_result(exc)
            raise AssertionError(
                f"{tool_name}({list(params.keys())}) should have succeeded "
                f"but raised ToolError: {error_data.get('error', str(exc))}"
            ) from exc

    async def call_tool_failure(
        self,
        tool_name: str,
        params: dict[str, Any],
        expected_error: str | None = None,
    ) -> dict[str, Any]:
        """Call tool through proxy and assert failure."""
        from fastmcp.exceptions import ToolError

        operation_name = f"{tool_name}({list(params.keys())})"
        try:
            result = await proxy_call_tool(self.client, tool_name, params)
            return assert_mcp_failure(result, operation_name, expected_error)
        except ToolError as exc:
            data = tool_error_to_result(exc)
            if data.get("success"):
                raise AssertionError(
                    f"{operation_name} should have failed but succeeded"
                ) from exc
            if expected_error:
                error_msg = str(data.get("error", "")).lower()
                if expected_error.lower() not in error_msg:
                    raise AssertionError(
                        f"{operation_name} failed but error doesn't contain "
                        f"'{expected_error}'. Actual: {data.get('error')}"
                    ) from exc
            return data
