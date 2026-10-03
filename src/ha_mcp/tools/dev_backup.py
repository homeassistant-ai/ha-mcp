"""Developer-tool access to backup configuration and its human-only controls."""

import json
from typing import Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

_HUMAN_BACKUP_CONTROLS = frozenset({"enable_snapshot_actions", "backup_read_only"})


def dev_backup_config_fields() -> list[dict[str, Any]]:
    """Expose live values while marking human-managed controls read-only."""
    from ..settings_ui._handlers_backups import backup_config_fields

    fields = backup_config_fields()
    for row in fields:
        if row["field"] in _HUMAN_BACKUP_CONTROLS:
            row["editable"] = False
            row["locked_reason"] = "human_only"
    return fields


async def apply_dev_backup_config(
    server: Any, backup: dict[str, Any] | None
) -> dict[str, Any]:
    """Validate and apply agent-editable backup settings as one atomic request."""
    from ..settings_ui._handlers_backups import (
        _validate_backup_payload,
        apply_backup_config,
    )

    if not isinstance(backup, dict):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_MISSING_PARAMETER,
                "'backup' (an object of {field: value}) is required for "
                "action='set_backup_config'",
                suggestions=[
                    "Call ha_dev_manage_settings('get_backup_config') for field names"
                ],
            )
        )
    human_controls = _HUMAN_BACKUP_CONTROLS.intersection(backup)
    if human_controls:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "These backup controls are human-only and cannot be changed "
                "through developer tools: " + ", ".join(sorted(human_controls)),
                suggestions=[
                    "Ask the user to change the controls in the Backups tab, "
                    "app configuration, or environment variables."
                ],
            )
        )
    clean, err = _validate_backup_payload(backup)
    if err is not None:
        raise_tool_error(
            create_error_response(ErrorCode.VALIDATION_INVALID_PARAMETER, err)
        )
    response = await apply_backup_config(server, clean)
    body = json.loads(bytes(response.body))
    if response.status_code >= 400:
        raise_tool_error(
            create_error_response(
                ErrorCode.SERVICE_CALL_FAILED,
                _backup_error_message(body),
                context={"status": response.status_code, "response": body},
            )
        )
    return {"success": True, "data": body}


def _backup_error_message(body: Any) -> str:
    """Pull a human message out of a backup-config error response body."""
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(
            err.get("message") or err.get("code") or "backup config update failed"
        )
    if isinstance(err, str):
        return err
    return "backup config update failed"
