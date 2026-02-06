"""
Persistent notification tools for Home Assistant MCP Server.

This module provides tools for retrieving persistent notifications
from Home Assistant, useful for debugging automations and monitoring
system messages.
"""

import logging
from typing import Any

from .helpers import log_tool_usage

logger = logging.getLogger(__name__)


def register_notification_tools(mcp: Any, client: Any, **kwargs: Any) -> None:
    """Register persistent notification tools with the MCP server."""

    @mcp.tool(
        annotations={
            "idempotentHint": True,
            "readOnlyHint": True,
            "tags": ["notifications"],
            "title": "Get Notifications",
        }
    )
    @log_tool_usage
    async def ha_get_notifications() -> dict[str, Any]:
        """List active persistent notifications.

        Returns all persistent notifications currently displayed in Home Assistant.
        Useful for verifying automations that create notifications and for
        monitoring system alerts.

        Persistent notifications are created via the persistent_notification.create
        service and remain until explicitly dismissed.

        RELATED TOOLS:
        - ha_call_service(): Create or dismiss notifications via persistent_notification domain
        - ha_get_history(): Check notification history over time
        """
        try:
            message: dict[str, Any] = {
                "type": "persistent_notification/get",
            }
            result = await client.send_websocket_message(message)

            if not result.get("success"):
                error = result.get("error", {})
                error_msg = (
                    error.get("message", str(error))
                    if isinstance(error, dict)
                    else str(error)
                )
                return {
                    "success": False,
                    "error": f"Failed to retrieve notifications: {error_msg}",
                    "suggestions": [
                        "Ensure Home Assistant is running and accessible",
                        "Check your connection settings",
                    ],
                }

            notifications = result.get("result", [])

            formatted = [
                {
                    "notification_id": notif.get("notification_id"),
                    "title": notif.get("title"),
                    "message": notif.get("message"),
                    "created_at": notif.get("created_at"),
                }
                for notif in notifications
            ]

            return {
                "success": True,
                "notifications": formatted,
                "count": len(formatted),
            }

        except Exception as e:
            logger.error(f"Failed to get notifications: {e}")
            return {
                "success": False,
                "error": f"Failed to retrieve notifications: {str(e)}",
                "suggestions": [
                    "Check Home Assistant connection",
                    "Ensure Home Assistant is running",
                ],
            }
