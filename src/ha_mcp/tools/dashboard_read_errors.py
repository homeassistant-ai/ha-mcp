"""What a failed dashboard read or lookup means.

Core answers ``config_not_found`` for two causes
(``homeassistant/components/lovelace/websocket.py``, ``_handle_errors``): an
identifier it does not know ("Unknown config specified: <url_path>") and a
dashboard it holds but has no config for ("No config found."), which the
frontend shows as its generated default view. The second comes from Core's
``ConfigNotFound`` (``lovelace/dashboard.py``): a storage dashboard with nothing
saved, a YAML dashboard whose file is missing, or any storage dashboard in
recovery mode. The code alone cannot tell the causes apart, so the message does.
If HA rewords either string, the lazy resolve stops firing and an empty
dashboard reads as a failure; re-verify them on major HA upgrades.
"""

from __future__ import annotations

import json
from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response
from .helpers import extract_tool_error_message, raise_tool_error

UNKNOWN_CONFIG_PREFIX = "Unknown config specified"
# Bare (nested error shape) and with the "Command failed: " prefix that
# ``send_websocket_message``'s flat envelope adds. Matched exactly, unlike the
# unknown-identifier substring, so any other read failure stays a failure.
NO_STORED_CONFIG_MESSAGES = frozenset(
    {"No config found.", "Command failed: No config found."}
)
CONFIG_NOT_FOUND_CODE = "config_not_found"
DASHBOARD_FETCH_FAILED_PREFIX = "Dashboard fetch failed: "

NO_STORED_CONFIG_NOTE = (
    "Home Assistant has no config for this dashboard (a storage dashboard with "
    "nothing saved yet, a YAML dashboard whose file is missing, or recovery "
    "mode), so the frontend shows its generated default view."
)


def is_unknown_dashboard(error_msg: str) -> bool:
    """Whether Core rejected the identifier itself (the lazy-resolve trigger)."""
    return UNKNOWN_CONFIG_PREFIX in error_msg


def is_no_stored_config_error(exc: ToolError) -> bool:
    """Whether ``_get_dashboard_config_internal``'s ToolError means no config."""
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
    """RESOURCE_NOT_FOUND when Core names the identifier unknown (call after the
    lazy resolve); otherwise SERVICE_CALL_FAILED."""
    if is_unknown_dashboard(error_msg):
        return ErrorCode.RESOURCE_NOT_FOUND
    return ErrorCode.SERVICE_CALL_FAILED


def raise_registry_read_error(*, action: str, url_path: str) -> NoReturn:
    """Raise the shared user-facing error for an unreadable dashboard registry."""
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED,
            "Failed to read the Home Assistant dashboard registry",
            suggestions=[
                "Retry the operation",
                "Check the Home Assistant connection and logs",
                "Use ha_config_get_dashboard(list_only=True) to verify registry access",
            ],
            context={"action": action, "url_path": url_path},
        )
    )


DASHBOARD_LIST_SUGGESTION = (
    "Use ha_config_get_dashboard(list_only=True) to see available dashboards"
)


def raise_unresolved_delete(
    url_path: str, dashboards: list[dict[str, Any]]
) -> NoReturn:
    """Raise the delete error for an identifier with no collection item."""
    # YAML rows carry no collection id, so ``_resolve_dashboard`` skips them.
    if any(
        d.get("url_path") == url_path and d.get("mode") == "yaml" for d in dashboards
    ):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"Dashboard '{url_path}' is a YAML-mode dashboard",
                details="It is defined in configuration.yaml; the "
                "Home Assistant API cannot delete it.",
                context={"action": "delete", "url_path": url_path},
            )
        )
    available_ids = [d.get("url_path") for d in dashboards[:10] if d.get("url_path")]
    raise_tool_error(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND,
            f"Dashboard '{url_path}' not found",
            details=f"No dashboard found with URL path or internal ID '{url_path}'.",
            suggestions=[DASHBOARD_LIST_SUGGESTION],
            context={
                "action": "delete",
                "url_path": url_path,
                "available_dashboard_ids": available_ids,
            },
        )
    )
