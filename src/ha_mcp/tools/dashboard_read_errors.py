"""What a failed dashboard read or lookup means.

Core answers ``config_not_found`` for two causes
(``homeassistant/components/lovelace/websocket.py``, ``_handle_errors``): an
identifier it does not know ("Unknown config specified: <url_path>") and a
dashboard it holds but has no config for ("No config found."), which the
frontend shows as its generated default view. The second comes from Core's
``ConfigNotFound`` (``lovelace/dashboard.py``): a storage dashboard with nothing
saved, a YAML dashboard whose file is missing, or any storage dashboard in
recovery mode. The code alone cannot tell the causes apart, so under that code
the message does (``error_wording``). If HA rewords either string, the lazy
resolve stops firing and an empty dashboard reads as a failure.
"""

from __future__ import annotations

from typing import Any, NoReturn

from ha_mcp._vendor.fastmcp.exceptions import ToolError

from ..error_wording import dashboard_has_no_config, dashboard_unknown
from ..errors import ErrorCode, create_error_response, get_error_code, get_error_message
from .helpers import extract_tool_error_message, raise_tool_error, tool_error_ha_code

CONFIG_NOT_FOUND_CODE = "config_not_found"
DASHBOARD_FETCH_FAILED_PREFIX = "Dashboard fetch failed: "

NO_STORED_CONFIG_NOTE = (
    "Home Assistant has no config for this dashboard (a storage dashboard with "
    "nothing saved yet, a YAML dashboard whose file is missing, or recovery "
    "mode), so the frontend shows its generated default view."
)


def is_unknown_dashboard(error_msg: str) -> bool:
    """Whether Core rejected the identifier itself (the lazy-resolve trigger)."""
    return dashboard_unknown(error_msg)


def config_not_found(reply: dict[str, Any]) -> bool:
    """Read the native code from a flattened or raw Core reply."""
    return (reply.get("error_code") or get_error_code(reply)) == CONFIG_NOT_FOUND_CODE


def no_stored_config(reply: dict[str, Any]) -> bool:
    """Only differentiate the two causes once Core reports config_not_found."""
    return config_not_found(reply) and dashboard_has_no_config(get_error_message(reply))


def is_no_stored_config_error(exc: ToolError) -> bool:
    """Whether ``_get_dashboard_config_internal``'s ToolError means no config."""
    if tool_error_ha_code(exc) != CONFIG_NOT_FOUND_CODE:
        return False
    message = extract_tool_error_message(exc).removeprefix(
        DASHBOARD_FETCH_FAILED_PREFIX
    )
    return dashboard_has_no_config(message)


def read_failure_code(reply: dict[str, Any]) -> ErrorCode:
    """RESOURCE_NOT_FOUND when Core names the identifier unknown (call after the
    lazy resolve); otherwise SERVICE_CALL_FAILED."""
    if config_not_found(reply) and is_unknown_dashboard(get_error_message(reply) or ""):
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
