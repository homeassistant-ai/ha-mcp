"""Consistent failure codes and recovery guidance for dashboard edit backends."""

from __future__ import annotations

import json
from typing import Any, NoReturn

from fastmcp.exceptions import ToolError

from ..errors import ErrorCode, create_error_response, get_error_code, get_error_message
from .helpers import extract_tool_error_message, raise_tool_error

_MODE_SUGGESTIONS = [
    "Use a storage-mode dashboard created through the Home Assistant UI or API",
    "For a YAML dashboard, edit its dashboard YAML file directly",
]
_EDIT_SUGGESTIONS = {
    "yaml_not_supported": _MODE_SUGGESTIONS,
    "unsupported_mode": _MODE_SUGGESTIONS,
    "strategy_conversion": [
        "Use 'Take Control' in the Home Assistant interface to convert it",
        "Keep a strategy configuration when updating this dashboard",
    ],
    "not_found": [
        "Verify the dashboard URL with ha_config_get_dashboard(list_only=True)",
        "Use the 'config' parameter to create a dashboard or initialize its saved config",
    ],
    "validation_failed": [
        "Correct the config or patch according to the validation error",
        "For patch operations, check the JSON Pointer paths and operation values",
    ],
    "conflict": [
        "Read the dashboard again with ha_config_get_dashboard",
        "Use its fresh config_hash and rebase the edit on the current config",
    ],
    "write_outcome_unknown": [
        "Read the dashboard with ha_config_get_dashboard before retrying",
        "Use its fresh config_hash and check whether the requested changes already applied",
    ],
}


def raise_dashboard_edit_error(
    url_path: str | None,
    code: str,
    message: str,
    write_committed: bool | None,
    action: str,
) -> NoReturn:
    """Preserve the operation and outcome while suggesting a relevant next step."""
    error_code = {
        "validation_failed": ErrorCode.VALIDATION_FAILED,
        "yaml_not_supported": ErrorCode.VALIDATION_FAILED,
        "unsupported_mode": ErrorCode.VALIDATION_FAILED,
        "strategy_conversion": ErrorCode.VALIDATION_FAILED,
        "not_found": ErrorCode.RESOURCE_NOT_FOUND,
    }.get(code, ErrorCode.SERVICE_CALL_FAILED)
    suggestions = _EDIT_SUGGESTIONS.get(
        code,
        [
            "Check the Home Assistant connection and this session's permissions",
            "Check Home Assistant logs for the reported error before retrying",
        ],
    )
    if code == "write_outcome_unknown":
        message = f"Dashboard write outcome unknown: {message}"
    raise_tool_error(
        create_error_response(
            error_code,
            message,
            suggestions=suggestions,
            context={
                "action": action,
                "url_path": url_path,
                "reason": code,
                "write_committed": write_committed,
                "post_write_verified": False,
            },
        )
    )


def raise_dashboard_edit_fetch_error(
    exc: ToolError, url_path: str, action: str
) -> NoReturn:
    """Only Core's explicit config_not_found means an absent dashboard/config."""
    try:
        data = json.loads(str(exc))
    except (ValueError, TypeError):
        data = None
    reason = (
        "not_found"
        if isinstance(data, dict) and data.get("ha_error_code") == "config_not_found"
        else "load_failed"
    )
    raise_dashboard_edit_error(
        url_path, reason, extract_tool_error_message(exc), False, action
    )


def raise_known_dashboard_save_rejection(
    response: dict[str, Any], url_path: str, action: str
) -> None:
    """Translate definite Core rejections; leave all other save errors unchanged.

    Lovelace's WebSocket wrapper reports missing configs as config_not_found.
    LovelaceConfig.async_save rejects non-storage implementations (including
    YAML) with HomeAssistantError("Not supported"), encoded as code "error".
    Require both that code and exact message: recovery-mode failures differ.
    """
    code = response.get("error_code") or get_error_code(response)
    message = get_error_message(response) or "Dashboard save rejected"
    if code == "config_not_found":
        raise_dashboard_edit_error(url_path, "not_found", message, False, action)
    if code == "error" and message.removeprefix("Command failed: ") == "Not supported":
        raise_dashboard_edit_error(
            url_path,
            "unsupported_mode",
            "Dashboard does not support storage-mode editing",
            False,
            action,
        )
