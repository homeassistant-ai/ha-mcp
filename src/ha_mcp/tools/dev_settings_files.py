"""Settings files the developer settings tool writes, and whole-file transfer."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, NoReturn

from ..errors import ErrorCode, create_error_response
from .helpers import raise_tool_error

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

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

    if settings_file is None:
        raise_tool_error(
            create_error_response(
                ErrorCode.VALIDATION_INVALID_PARAMETER,
                "import_settings needs settings_file: an export_settings result's "
                "settings_file, or a file downloaded from a settings page.",
            )
        )
    try:
        guarded = await _guard_agent_import(server, settings_file)
        return await import_bundle(server, guarded)
    except TransferFailed as err:
        _raise_transfer_error(err.code)


def _kept(
    incoming: dict[str, Any] | None, current: dict[str, Any] | None, names: Any
) -> dict[str, Any] | None:
    """``incoming`` with ``names`` taken from ``current`` (or left unset)."""
    merged = {k: v for k, v in (incoming or {}).items() if k not in names}
    merged.update({k: v for k, v in (current or {}).items() if k in names})
    return merged or None


async def _guard_agent_import(
    server: HomeAssistantSmartMCPServer | None, settings_file: dict[str, Any]
) -> dict[str, Any]:
    """Hold an agent's import to the limits of the single-setting actions.

    The human-only settings keep this server's values, and anything that
    loosens what limits the agent needs dev_tools_security_policy_access.
    """
    from .. import settings_transfer as transfer
    from ..settings_ui._handlers_transfer import export_bundle
    from .dev_backup import _HUMAN_BACKUP_CONTROLS
    from .tools_dev import _HUMAN_ONLY_SETTINGS, _require_security_policy_access

    try:
        incoming = transfer.validate_server_settings(settings_file["server_settings"])
    except KeyError, TypeError, ValueError:
        return settings_file  # import_bundle refuses it with its own code
    current = await export_bundle(server)
    settings = current["server_settings"]
    incoming["features"] = _kept(
        incoming["features"], settings["features"], _HUMAN_ONLY_SETTINGS
    )
    incoming["backup"] = _kept(
        incoming["backup"], settings["backup"], _HUMAN_BACKUP_CONTROLS
    )
    switch = "enable_tool_security_policies"
    entries = settings_file.get("entries")
    tools = entries.get("tools") if isinstance(entries, dict) else None
    if (
        incoming["policy"] != settings["policy"]
        or incoming["visibility"] != settings["visibility"]
        or (incoming["features"] or {}).get(switch)
        != (settings["features"] or {}).get(switch)
        or (
            isinstance(tools, dict)
            and not tools.get("disabled")
            and {k: tools.get(k) for k in ("allowed_dirs", "extra_yaml_keys")}
            != current["entries"].get("tools")
        )
    ):
        _require_security_policy_access(
            "import tool security policies, entity visibility or File & YAML "
            "Tools permissions"
        )
    return {**settings_file, "server_settings": incoming}


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
