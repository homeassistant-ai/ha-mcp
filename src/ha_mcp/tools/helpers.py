"""
Reusable helper functions for MCP tools.

Centralized utilities that can be shared across multiple tool implementations.
"""

from typing import Any

from ..client.websocket_client import HomeAssistantWebSocketClient


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


def get_backup_hint_text() -> str:
    """
    Generate dynamic backup hint text based on BACKUP_HINT config.

    Returns:
        Backup hint text appropriate for the configured hint level.
    """
    from ..config import get_global_settings

    settings = get_global_settings()
    hint = getattr(settings, "backup_hint", "normal")

    hints = {
        "strong": "Run this backup before the FIRST modification of the day/session. This is usually not required since most operations can be rolled back (the model fetches definitions before modifying). Users with daily backups configured should use 'normal' or 'weak' instead.",
        "normal": "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",
        "weak": "Backups are usually not required for configuration changes since most operations can be manually undone. Only run this if specifically requested or before irreversible system operations.",
        "auto": "Run before operations that CANNOT be undone (e.g., deleting devices). If the current definition was fetched or can be fetched, this tool is usually not needed.",  # Same as normal for now, will auto-detect in future
    }
    return hints.get(hint, hints["normal"])
