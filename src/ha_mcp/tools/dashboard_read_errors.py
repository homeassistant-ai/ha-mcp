"""What a failed ``lovelace/config`` read means.

Core answers ``config_not_found`` for two causes
(``homeassistant/components/lovelace/websocket.py``, ``_handle_errors``): an
identifier it does not know ("Unknown config specified: <url_path>") and an
existing dashboard with no saved config ("No config found."), which the
frontend shows as its generated default view. The code alone cannot tell them
apart, so the message does; re-verify these strings on major HA upgrades.
"""

from __future__ import annotations

import json
from typing import Any

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode
from .helpers import extract_tool_error_message

UNKNOWN_CONFIG_PREFIX = "Unknown config specified"
# Bare (nested error shape) and as ``send_websocket_message``'s flat envelope
# prefixes it.
NO_STORED_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
CONFIG_NOT_FOUND_CODE = "config_not_found"
DASHBOARD_FETCH_FAILED_PREFIX = "Dashboard fetch failed: "


def is_unknown_dashboard(error_msg: str) -> bool:
    """Whether Core rejected the identifier itself (the lazy-resolve trigger)."""
    return UNKNOWN_CONFIG_PREFIX in error_msg


def is_no_stored_config_error(exc: ToolError) -> bool:
    """Whether a raised dashboard read means the dashboard has no saved config."""
    try:
        error_data = json.loads(str(exc))
    except (json.JSONDecodeError, TypeError):
        return False
    if (
        not isinstance(error_data, dict)
        or error_data.get("ha_error_code") != CONFIG_NOT_FOUND_CODE
    ):
        return False
    message = extract_tool_error_message(exc).removeprefix(
        DASHBOARD_FETCH_FAILED_PREFIX
    )
    return message in NO_STORED_CONFIG_MESSAGES


def read_failure_code(error_msg: str) -> ErrorCode:
    """Not found when Core still names the dashboard unknown after the lazy resolve."""
    if is_unknown_dashboard(error_msg):
        return ErrorCode.RESOURCE_NOT_FOUND
    return ErrorCode.SERVICE_CALL_FAILED


def no_stored_config_result(url_path: str | None) -> dict[str, Any]:
    """Get-mode answer for a dashboard that exists with no saved config."""
    return {
        "success": True,
        "action": "get",
        "url_path": url_path,
        "config": None,
        "stored_config": False,
        "message": (
            "The dashboard exists but has no saved config; Home Assistant shows "
            "its generated default view until a config is saved."
        ),
    }
