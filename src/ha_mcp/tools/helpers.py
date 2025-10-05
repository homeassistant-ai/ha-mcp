"""
Reusable helper functions for MCP tools.

Centralized utilities that can be shared across multiple tool implementations.
"""

import functools
import time
from typing import Any

from ..client.websocket_client import HomeAssistantWebSocketClient
from ..utils.usage_logger import log_tool_call


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
        return None, {
            "success": False,
            "error": "Failed to connect to Home Assistant WebSocket",
            "suggestion": "Check Home Assistant connection and ensure WebSocket API is available",
        }
    return ws_client, None


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
