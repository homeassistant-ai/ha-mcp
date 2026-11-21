"""Settings files the developer settings tool writes, and whole-file transfer."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, NoReturn

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer
    from ..settings_ui._handlers_transfer import ImportLimits

# Marks a key for removal in merge_feature_overrides (reset action).
REMOVE = object()


async def merge_feature_overrides(changes: dict[str, Any]) -> None:
    """Merge changes atomically, refusing to overwrite unreadable or corrupt JSON."""
    from ..config import _FEATURE_FLAG_OVERRIDE_FILENAME
    from ..settings_ui._persistence import _atomic_write_json
    from ..utils.config_write_lock import get_config_write_lock, run_with_file_lock
    from ..utils.data_paths import get_data_dir

    def merge() -> None:
        path = get_data_dir() / _FEATURE_FLAG_OVERRIDE_FILENAME
        existing: dict[str, Any] = {}
        try:
            existing_raw = path.read_text()
        except FileNotFoundError:
            existing_raw = None
        except OSError as exc:
            raise_tool_error(
                create_error_response(
                    ErrorCode.INTERNAL_ERROR,
                    f"Could not read existing override file "
                    f"({type(exc).__name__}: {exc}); refusing to "
                    "overwrite to preserve prior settings.",
                    suggestions=["Check filesystem permissions and retry"],
                )
            )
        if existing_raw is not None:
            try:
                parsed = json.loads(existing_raw)
            except json.JSONDecodeError as exc:
                raise_tool_error(
                    create_error_response(
                        ErrorCode.CONFIG_INVALID,
                        f"Existing override file at {path} is not valid "
                        f"JSON ({exc}); refusing to overwrite to preserve "
                        "prior settings.",
                        suggestions=["Inspect or delete the file manually and retry"],
                    )
                )
            if isinstance(parsed, dict):
                existing = parsed
        for key, val in changes.items():
            if val is REMOVE:
                existing.pop(key, None)
            else:
                existing[key] = val
        _atomic_write_json(path, existing)

    # The worker owns the file lock through the replace, even if the
    # awaiting tool call is cancelled while the worker is still running.
    async with get_config_write_lock():
        await asyncio.to_thread(run_with_file_lock, merge)


async def export_settings_file(
    server: HomeAssistantSmartMCPServer | None,
) -> dict[str, Any]:
    """The settings page's export, without the HA entry's connection."""
    from ..settings_ui._handlers_transfer import TransferFailed, export_bundle

    try:
        return {"success": True, "settings_file": await export_bundle(server)}
    except TransferFailed as err:
        _raise_transfer_error(err.code)


async def import_settings_file(
    server: HomeAssistantSmartMCPServer | None, settings_file: dict[str, Any] | None
) -> dict[str, Any]:
    """Apply an export as the settings page's import does."""
    from ..settings_ui._handlers_transfer import TransferFailed, import_bundle
    from .tools_dev import _require_security_policy_access

    if settings_file is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "import_settings needs settings_file: an export_settings result's "
                "settings_file, or a file downloaded from a settings page.",
            )
        )
    try:
        return await import_bundle(server, settings_file, _agent_limits())
    except TransferFailed as err:
        if err.code == "access_required":
            _require_security_policy_access(_GUARDED_CHANGES)
        _raise_transfer_error(err.code)


_GUARDED_CHANGES = (
    "import tool security policies, entity visibility or File & YAML Tools permissions"
)
_SWITCH = "enable_tool_security_policies"


def _agent_limits() -> ImportLimits:
    """Hold an agent's import to the limits of the single-setting actions.

    The human-only settings keep this server's values, and changing what
    limits the agent needs dev_tools_security_policy_access. The import
    applies both against the settings it replaces, under its write lock.
    """
    from ..settings_ui._handlers_transfer import ImportLimits
    from .dev_backup import _HUMAN_BACKUP_CONTROLS
    from .tools_dev import _HUMAN_ONLY_SETTINGS, _security_policy_access_enabled

    def allow(old: dict[str, Any], new: dict[str, Any]) -> bool:
        changed = any(
            old.get(k) != new.get(k) for k in ("policy", "visibility", "permissions")
        )
        switch = (old.get("features") or {}).get(_SWITCH) != (
            new.get("features") or {}
        ).get(_SWITCH)
        return not (changed or switch) or _security_policy_access_enabled()

    return ImportLimits(
        keep={"features": _HUMAN_ONLY_SETTINGS, "backup": _HUMAN_BACKUP_CONTROLS},
        allow=allow,
    )


def _raise_transfer_error(code: str) -> NoReturn:
    rollback = code in {"rollback_incomplete", "restore_failed"}
    raise_tool_error(
        create_error_response(
            ErrorCode.SERVICE_CALL_FAILED if rollback else ErrorCode.CONFIG_INVALID,
            f"Settings transfer refused: {code}",
            suggestions=[
                "Check the Home Assistant / server log for the failing step"
                if rollback
                else "Use a file exported by HA-MCP's settings page or export_settings"
            ],
        )
    )
