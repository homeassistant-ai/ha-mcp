"""
Reusable helper functions for MCP tools.

Centralized utilities that can be shared across multiple tool implementations.
"""

import functools
import json
import logging
import re
import sys
import time
from typing import Any, Literal, NoReturn, overload

from fastmcp.exceptions import ToolError

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
    HomeAssistantCommandError,
    HomeAssistantConnectionError,
)
from ..client.websocket_client import HomeAssistantWebSocketClient
from ..errors import (
    ErrorCode,
    create_auth_error,
    create_connection_error,
    create_entity_not_found_error,
    create_error_response,
    create_timeout_error,
    create_validation_error,
)
from ..utils.usage_logger import log_tool_call

logger = logging.getLogger(__name__)


def raise_tool_error(error_response: dict[str, Any]) -> NoReturn:
    """
    Raise a ToolError with structured error information.

    This function converts a structured error response dictionary into a ToolError
    exception, which signals to MCP clients that the tool execution failed via
    the isError flag in the protocol response.

    The structured error information is preserved as JSON in the error message,
    allowing AI agents to parse and act on the detailed error information.

    Args:
        error_response: Structured error response dictionary with 'success': False
                       and 'error' containing code, message, suggestions, etc.

    Raises:
        ToolError: Always raises with the JSON-serialized error response

    Example:
        >>> error = create_error_response(
        ...     ErrorCode.ENTITY_NOT_FOUND,
        ...     "Entity light.nonexistent not found"
        ... )
        >>> raise_tool_error(error)  # Raises ToolError with isError=true
    """
    raise ToolError(json.dumps(error_response, indent=2, default=str))


def extract_tool_error_message(te: ToolError) -> str:
    """Extract a human-readable error message from a ToolError.

    Pairs with raise_tool_error() which serializes error dicts as JSON.
    Falls back to str(te) if the message is not valid JSON.
    """
    try:
        error_data = json.loads(str(te))
        msg = error_data.get("error", {}).get("message", str(te))
        return str(msg)
    except (json.JSONDecodeError, TypeError, AttributeError):
        return str(te)


async def get_connected_ws_client(
    base_url: str, token: str
) -> tuple[HomeAssistantWebSocketClient | None, dict[str, Any] | None]:
    """
    Create and connect a WebSocket client.

    Args:
        base_url: Home Assistant base URL
        token: Authentication token

    Returns:
        Tuple of (ws_client, error_dict). If connection fails, ws_client is None.
    """
    ws_client = HomeAssistantWebSocketClient(base_url, token)
    connected = await ws_client.connect()
    if not connected:
        return None, create_connection_error(
            "Failed to connect to Home Assistant WebSocket",
            details="WebSocket connection could not be established",
        )
    return ws_client, None



def _classify_api_status(
    error: HomeAssistantAPIError,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify HomeAssistantAPIError by HTTP status code."""
    match error.status_code:
        case 404:
            entity_id = context.get("entity_id") if context else None
            if entity_id:
                result = create_entity_not_found_error(entity_id, details=error_msg)
            else:
                result = create_error_response(ErrorCode.RESOURCE_NOT_FOUND, error_msg, context=context)
        case 401 | 403:
            result = create_auth_error(error_msg, context=context)
        case 400:
            result = create_validation_error(error_msg, context=context)
        case _:
            result = create_error_response(ErrorCode.SERVICE_CALL_FAILED, error_msg, context=context)
    return result


def _classify_exception(
    error: Exception,
    error_str: str,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify exception into structured error response by type, then message."""
    result: dict[str, Any] | None = None

    # Type-based classification
    match error:
        case HomeAssistantConnectionError():
            result = create_connection_error(
                error_msg, timeout="timeout" in error_str, context=context
            )
        case HomeAssistantAuthError():
            result = create_auth_error(
                error_msg, expired="expired" in error_str, context=context
            )
        case HomeAssistantAPIError():
            result = _classify_api_status(error, error_msg, context)
        case HomeAssistantCommandError():
            # WebSocket command-failure. The ``error.code`` on Supervisor
            # calls routed through HA Core's hassio WS bridge is always
            # ``unknown_error`` (see homeassistant/components/hassio/
            # websocket_api.py), so discrimination must come from the
            # message. Fall through to ``_classify_by_message`` which
            # pattern-matches schema, auth, not-found and timeout cases.
            result = None
        case TimeoutError():
            operation = context.get("operation", "request") if context else "request"
            timeout_seconds = context.get("timeout_seconds", 30) if context else 30
            result = create_timeout_error(operation, timeout_seconds, details=error_msg, context=context)
        case ValueError():
            result = create_validation_error(error_msg, context=context)

    if result is not None:
        return result

    # Message-based classification fallback
    return _classify_by_message(error_str, error_msg, context)


def _classify_by_message(
    error_str: str,
    error_msg: str,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Classify exception by error message patterns."""
    result: dict[str, Any]
    # Schema-branch must precede the "not found" / 404 branch (most-specific-first):
    # a vol.Invalid message phrased like "Command failed: key X not found" would
    # otherwise misclassify as RESOURCE_NOT_FOUND. The "command failed:" prefix
    # gates the branch so non-schema WS errors fall through.
    if "command failed:" in error_str and (
        any(
            marker in error_str
            for marker in (
                "missing option",
                "extra keys not allowed",
                "unknown secret",
                "unknown type",
            )
        )
        or re.search(r"expected (?:a |str|int|bool|dict|list|float|type|one of)", error_str)
    ):
        # Supervisor schema validation: vol.Invalid message arriving as a
        # HomeAssistantCommandError via HA Core's hassio WS bridge. The
        # markers plus the "expected <type>" anchor regex cover the
        # heterogeneous vol.Invalid vocabulary without relying on an
        # error code (always unknown_error from the bridge).
        result = create_validation_error(error_msg, context=context)
    elif "not found" in error_str or "404" in error_str:
        entity_id = context.get("entity_id") if context else None
        if entity_id:
            result = create_entity_not_found_error(entity_id, details=error_msg)
        else:
            result = create_error_response(ErrorCode.RESOURCE_NOT_FOUND, error_msg, context=context)
    elif "timeout" in error_str:
        result = create_timeout_error("operation", 30, details=error_msg, context=context)
    elif "connection" in error_str or "connect" in error_str:
        result = create_connection_error(error_msg, context=context)
    elif any(
        phrase in error_str
        for phrase in (
            "unauthorized",
            "authentication",
            "invalid token",
            "access denied",
        )
    ) or "401" in error_str:
        result = create_auth_error(error_msg, context=context)
    elif error_str.startswith("command failed:"):
        # HomeAssistantCommandError fallback: WS ``success=False`` with a
        # message that doesn't match any specific marker above. This is a
        # known failure mode (the WS command itself failed), not an
        # unexpected internal error — route to SERVICE_CALL_FAILED,
        # mirroring the 4xx fallback in _classify_api_status.
        result = create_error_response(ErrorCode.SERVICE_CALL_FAILED, error_msg, context=context)
    else:
        result = create_error_response(
            ErrorCode.INTERNAL_ERROR, "An unexpected error occurred", details=error_msg, context=context
        )
    return result


def _append_macos_hints(error_response: dict[str, Any]) -> None:
    if not (
        sys.platform == "darwin"
        and "error" in error_response
        and isinstance(error_response["error"], dict)
        and error_response["error"].get("code")
        in (ErrorCode.CONNECTION_FAILED, ErrorCode.CONNECTION_TIMEOUT)
    ):
        return
    macos_hints = [
        "macOS may block local network access for Claude Desktop subprocesses "
        "(System Settings > Privacy & Security > Local Network)",
        "Try an SSH tunnel: ssh -N -L 8123:localhost:8123 user@ha-server, "
        "then use http://localhost:8123",
        "Ensure you are using http:// (not https://) unless SSL/TLS is configured",
    ]
    # Handle both "suggestions" (plural, 2+ items) and "suggestion" (singular, 1 item)
    existing = error_response["error"].get("suggestions") or []
    if not existing:
        single = error_response["error"].get("suggestion")
        if single:
            existing = [single]
    error_response["error"]["suggestions"] = existing + macos_hints


@overload
def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    raise_error: Literal[True] = True,
    suggestions: list[str] | None = None,
) -> NoReturn: ...


@overload
def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    raise_error: Literal[False],
    suggestions: list[str] | None = None,
) -> dict[str, Any]: ...


def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
    *,
    raise_error: bool = True,
    suggestions: list[str] | None = None,
) -> dict[str, Any]:
    """
    Convert an exception to a structured error response.

    This function maps common exception types to appropriate error codes
    and creates informative error responses. By default, it raises a ToolError
    to signal the error at the MCP protocol level (isError=true).

    Args:
        error: The exception to convert
        context: Additional context to include in the response
        raise_error: If True (default), raises ToolError with the structured error.
                    If False, returns the error dict for further modification.
        suggestions: Optional list of actionable suggestions to embed in the error.
                    Saves callers from manually inserting suggestions after the call.

    Returns:
        Structured error response dictionary (only if raise_error=False)

    Raises:
        ToolError: If raise_error=True (default), raises with JSON-serialized error
    """
    error_str = str(error).lower()
    error_msg = str(error)

    error_response = _classify_exception(error, error_str, error_msg, context)

    if suggestions and "error" in error_response and isinstance(error_response["error"], dict):
        # Set both `suggestion` (singular, first item) and `suggestions`
        # (plural, full list). create_error_response (errors.py) sets the
        # singular key; existing tests for exception_to_structured_error
        # rely on the plural key being present even for single-item caller
        # suggestions. Setting both keeps response consumers on both code
        # paths working.
        error_response["error"]["suggestion"] = suggestions[0]
        error_response["error"]["suggestions"] = suggestions

    # Append macOS-specific hints for connection failures (after all other processing
    # so hints survive regardless of whether caller provided explicit suggestions)
    _append_macos_hints(error_response)

    if raise_error:
        raise_tool_error(error_response)

    return error_response


def log_tool_usage(func: Any) -> Any:
    """
    Decorator to automatically log MCP tool usage.

    Tracks execution time, success/failure, and response size for all tool calls.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start_time = time.time()
        tool_name = func.__name__
        success = True
        error_message = None
        response_size = None

        try:
            result = await func(*args, **kwargs)
            if isinstance(result, str):
                response_size = len(result.encode("utf-8"))
            elif hasattr(result, "__len__"):
                response_size = len(str(result).encode("utf-8"))
            return result
        except Exception as e:
            success = False
            error_message = str(e)
            raise
        finally:
            execution_time_ms = (time.time() - start_time) * 1000
            log_tool_call(
                tool_name=tool_name,
                parameters=kwargs,
                execution_time_ms=execution_time_ms,
                success=success,
                error_message=error_message,
                response_size_bytes=response_size,
            )

    return wrapper


def register_tool_methods(mcp: Any, instance: Any) -> None:
    """Register all @tool-decorated methods from a class instance with the MCP server.

    Discovers methods bearing a ``__fastmcp__`` attribute (set by the outermost
    ``@tool`` decorator — must be listed above ``@log_tool_usage``) and registers
    them via ``mcp.add_tool()``.
    """
    count = 0
    for attr in dir(instance):
        method = getattr(instance, attr)
        if callable(method) and hasattr(method, "__fastmcp__"):
            mcp.add_tool(method)
            count += 1
    if count == 0:
        logger.warning(
            f"No @tool-decorated methods found on {type(instance).__name__}"
        )
