"""Argument validation and access checks for explicit backup tool actions."""

from typing import TYPE_CHECKING, Any

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

if TYPE_CHECKING:
    from ..config import Settings


_VALID_COMBOS: set[tuple[str, str]] = {
    ("snapshot", "create"),
    ("snapshot", "list"),
    ("snapshot", "restore"),
    ("snapshot", "delete"),
    ("edits", "create"),
    ("edits", "list"),
    ("edits", "view"),
    ("edits", "diff"),
    ("edits", "restore"),
    ("edits", "delete"),
}


def gate_backup_combo(scope: str, action: str) -> None:
    """Reject unsupported scope/action pairs before dispatch."""
    if (scope, action) in _VALID_COMBOS:
        return
    raise_tool_error(
        create_error_response(
            ErrorCode.VALIDATION_INVALID_PARAMETER,
            f"Invalid combination: scope={scope!r}, action={action!r}",
            context={"scope": scope, "action": action},
            suggestions=[
                "Valid combinations: "
                + ", ".join(sorted(f"({s},{a})" for s, a in _VALID_COMBOS)),
                "scope='snapshot' is for full HA tarball backups (heavy, restart on restore)",
                "scope='edits' is for per-entity auto-backups produced by write tools (lightweight)",
            ],
        )
    )


def require_backup_param(param_name: str, value: Any, scope: str, action: str) -> Any:
    """Validate a required parameter for the selected backup operation."""
    if value is None or (isinstance(value, str) and not value.strip()):
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                f"{param_name!r} is required for scope={scope!r}, action={action!r}",
                context={"scope": scope, "action": action, "missing_param": param_name},
            )
        )
    return value


def require_backup_access(settings: "Settings", scope: str, action: str) -> None:
    """Gate explicit calls, including proxy dispatch, before any HA I/O.

    Automatic capture does not pass through this guard. The global/request
    read-only restriction takes precedence over the backup-specific controls.
    """
    from ..read_only import READ_ONLY_EXEMPT_TOOLS, require_write_access

    read_operation = (
        READ_ONLY_EXEMPT_TOOLS["ha_manage_backup"].blocked_write(
            {"scope": scope, "action": action}
        )
        is None
    )
    if not read_operation:
        require_write_access("ha_manage_backup")
    if scope == "snapshot" and not settings.enable_snapshot_actions:
        raise_tool_error(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Full Home Assistant snapshot actions are disabled "
                "(enable_snapshot_actions=false), including listing. "
                "Per-edit backups remain available subject to backup_read_only.",
                context={
                    "scope": scope,
                    "action": action,
                    "enable_snapshot_actions": False,
                },
                suggestions=[
                    "Use scope='edits' to inspect per-edit backups.",
                    (
                        "A human can enable snapshot actions in the Backups tab, app "
                        "configuration, or ENABLE_SNAPSHOT_ACTIONS environment variable. "
                        "Developer tools cannot change this control."
                    ),
                ],
            )
        )
    if settings.backup_read_only is True and not read_operation:
        raise_tool_error(
            create_error_response(
                ErrorCode.READ_ONLY_MODE,
                "Backup Read Only is enabled (backup_read_only=true). Explicit "
                "backup creation, restore, and deletion are blocked in both scopes. "
                "Automatic pre-edit capture still follows enable_auto_backup.",
                context={"scope": scope, "action": action, "backup_read_only": True},
                suggestions=[
                    (
                        "Use edits.list, edits.view, edits.diff, or snapshot.list "
                        "when snapshot actions are enabled."
                    ),
                    (
                        "A human can change Backup Read Only in the Backups tab, app "
                        "configuration, or BACKUP_READ_ONLY environment variable. "
                        "Developer tools cannot change this control."
                    ),
                ],
            )
        )
