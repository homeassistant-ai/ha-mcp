"""
Reusable helper functions for MCP tools.

Centralized utilities that can be shared across multiple tool implementations.
"""

import functools
import logging
import time
from typing import Any

from ..client.rest_client import (
    HomeAssistantAPIError,
    HomeAssistantAuthError,
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


def exception_to_structured_error(
    error: Exception,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Convert an exception to a structured error response.

    This function maps common exception types to appropriate error codes
    and creates informative error responses.

    Args:
        error: The exception to convert
        context: Additional context to include in the response

    Returns:
        Structured error response dictionary
    """
    error_str = str(error).lower()
    error_msg = str(error)

    # Handle specific exception types
    if isinstance(error, HomeAssistantConnectionError):
        if "timeout" in error_str:
            return create_connection_error(error_msg, timeout=True)
        return create_connection_error(error_msg)

    if isinstance(error, HomeAssistantAuthError):
        if "expired" in error_str:
            return create_auth_error(error_msg, expired=True)
        return create_auth_error(error_msg)

    if isinstance(error, HomeAssistantAPIError):
        # Check for specific error patterns
        if error.status_code == 404:
            # Entity or resource not found
            entity_id = context.get("entity_id") if context else None
            if entity_id:
                return create_entity_not_found_error(entity_id, details=error_msg)
            return create_error_response(
                ErrorCode.RESOURCE_NOT_FOUND,
                error_msg,
                context=context,
            )
        if error.status_code == 401:
            return create_auth_error(error_msg)
        if error.status_code == 400:
            return create_validation_error(error_msg, context=context)

        # Generic API error
        return create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            error_msg,
            context=context,
        )

    if isinstance(error, TimeoutError):
        operation = context.get("operation", "request") if context else "request"
        timeout_seconds = context.get("timeout_seconds", 30) if context else 30
        return create_timeout_error(operation, timeout_seconds, details=error_msg)

    if isinstance(error, ValueError):
        return create_validation_error(error_msg)

    # Check for common error patterns in error message
    if "not found" in error_str or "404" in error_str:
        entity_id = context.get("entity_id") if context else None
        if entity_id:
            return create_entity_not_found_error(entity_id, details=error_msg)
        return create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            error_msg,
            context=context,
        )

    if "timeout" in error_str:
        return create_timeout_error("operation", 30, details=error_msg)

    if "connection" in error_str or "connect" in error_str:
        return create_connection_error(error_msg)

    if "auth" in error_str or "token" in error_str or "401" in error_str:
        return create_auth_error(error_msg)

    # Default to internal error
    return create_error_response(
        ErrorCode.INTERNAL_ERROR,
        error_msg,
        details="An unexpected error occurred",
        context=context,
    )


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
