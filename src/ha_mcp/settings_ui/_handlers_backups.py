"""Backup snapshot + auto-backup-config route handlers for the settings UI.

Covers two related concerns behind one factory:

- **Snapshot ops** (list/view/diff/restore/delete/bulk) over the
  ``BackupManager`` snapshot store.
- **Auto-backup config** (get/save) — the enabled/throttle/retention
  knobs, persisted to the standalone override file or (in add-on mode)
  round-tripped through the Supervisor add-on options.

Handlers are module-level (own C901 budget); ``build_backups_handlers``
binds ``server`` into request-only wrappers.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._version import is_running_in_addon
from ..backup_manager import get_backup_manager
from ..config import (
    BACKUP_OVERRIDE_FIELDS,
    _reset_global_settings,
    get_backup_setting_origin,
    get_global_settings,
)
from ..errors import ErrorCode, create_error_response
from . import _persistence, _supervisor

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)


def _backup_mgr(server: HomeAssistantSmartMCPServer | None) -> Any:
    settings = get_global_settings()
    client = getattr(server, "client", None) or getattr(server, "_client", None)
    return get_backup_manager(client, settings) if client is not None else None


def _bad_request(
    message: str,
    *,
    code: ErrorCode = ErrorCode.VALIDATION_INVALID_PARAMETER,
    status: int = 400,
) -> JSONResponse:
    return JSONResponse(create_error_response(code, message), status_code=status)


def _not_found(name: str) -> JSONResponse:
    return JSONResponse(
        create_error_response(
            ErrorCode.RESOURCE_NOT_FOUND, f"Backup {name!r} not found"
        ),
        status_code=404,
    )


async def _list_backups(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    mgr = _backup_mgr(server)
    if mgr is None:
        return _bad_request("Backup manager unavailable")
    params = request.query_params
    try:
        limit = int(params.get("limit", "500"))
    except ValueError:
        return _bad_request("'limit' must be an integer")
    # Offload sync directory I/O to keep the request handler async-clean.
    entries = await asyncio.to_thread(
        mgr.list_snapshots,
        domain=params.get("domain") or None,
        entity_id=params.get("entity_id") or None,
        limit=max(1, min(10_000, limit)),
    )
    settings = get_global_settings()
    return JSONResponse(
        {
            "success": True,
            "backups": entries,
            "count": len(entries),
            "backup_dir": str(mgr.backup_dir),
            "enabled": mgr.enabled,
            "throttle_minutes": settings.auto_backup_throttle_minutes,
            "retain_per_entity": settings.auto_backup_retain_per_entity,
        }
    )


async def _view_backup(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    mgr = _backup_mgr(server)
    if mgr is None:
        return _bad_request("Backup manager unavailable")
    name = request.path_params.get("name", "")
    try:
        data = await asyncio.to_thread(mgr.read_snapshot, name)
    except FileNotFoundError:
        return _not_found(name)
    except ValueError as err:
        return _bad_request(str(err))
    return JSONResponse({"success": True, "data": data})


async def _diff_backup(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    import difflib

    import yaml  # type: ignore[import-untyped]

    mgr = _backup_mgr(server)
    if mgr is None:
        return _bad_request("Backup manager unavailable")
    name = request.path_params.get("name", "")
    try:
        snapshot = await asyncio.to_thread(mgr.read_snapshot, name)
    except FileNotFoundError:
        return _not_found(name)
    except ValueError as err:
        return _bad_request(str(err))
    handler = mgr.handler_for(snapshot["domain"])
    if handler is None:
        return _bad_request(
            f"No handler for domain {snapshot['domain']!r}; cannot diff",
            code=ErrorCode.RESOURCE_NOT_FOUND,
            status=404,
        )
    client = getattr(server, "client", None) or getattr(server, "_client", None)
    # Narrow to transport / HA-API / FS errors so programming bugs
    # propagate to the request handler instead of decorating the diff
    # output with a "_error" sentinel masquerading as entity state.
    from ..backup_manager import _CAPTURE_TRANSIENT_ERRORS

    try:
        current = await handler.fetch(client, snapshot["entity_id"])
    except _CAPTURE_TRANSIENT_ERRORS as err:
        current = {"_error": f"{type(err).__name__}: {err}"}
    backup_yaml = yaml.safe_dump(
        snapshot.get("config"), default_flow_style=False, sort_keys=True
    ).splitlines()
    current_yaml = yaml.safe_dump(
        current, default_flow_style=False, sort_keys=True
    ).splitlines()
    diff = list(
        difflib.unified_diff(
            backup_yaml,
            current_yaml,
            fromfile=f"backup:{name}",
            tofile=f"current:{snapshot['entity_id']}",
            lineterm="",
        )
    )
    return JSONResponse(
        {
            "success": True,
            "diff": "\n".join(diff),
            "backup_present": current is not None
            and not (isinstance(current, dict) and "_error" in current),
        }
    )


async def _restore_backup(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    mgr = _backup_mgr(server)
    if mgr is None:
        return _bad_request("Backup manager unavailable")
    name = request.path_params.get("name", "")
    try:
        result = await mgr.restore_snapshot(name)
    except FileNotFoundError:
        return _not_found(name)
    except (ValueError, LookupError) as err:
        return _bad_request(str(err))
    return JSONResponse({"success": True, "data": result})


async def _delete_backup(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    mgr = _backup_mgr(server)
    if mgr is None:
        return _bad_request("Backup manager unavailable")
    name = request.path_params.get("name", "")
    try:
        await asyncio.to_thread(mgr.delete_snapshot, name)
    except FileNotFoundError:
        return _not_found(name)
    except ValueError as err:
        return _bad_request(str(err))
    return JSONResponse({"success": True, "deleted": [name]})


async def _delete_backups_bulk(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    mgr = _backup_mgr(server)
    if mgr is None:
        return _bad_request("Backup manager unavailable")
    params = request.query_params
    older = params.get("older_than_days")
    try:
        older_int = int(older) if older is not None else None
    except ValueError:
        return _bad_request("'older_than_days' must be an integer")
    if (
        params.get("domain") is None
        and params.get("entity_id") is None
        and older_int is None
    ):
        return _bad_request(
            "Bulk delete requires at least one filter "
            "(domain, entity_id, older_than_days)"
        )
    try:
        bulk = await asyncio.to_thread(
            mgr.delete_bulk,
            domain=params.get("domain") or None,
            entity_id=params.get("entity_id") or None,
            older_than_days=older_int,
        )
    except ValueError as err:
        return _bad_request(str(err))
    return JSONResponse(
        {
            "success": True,
            "deleted": bulk["deleted"],
            "failed": bulk["failed"],
            "count": len(bulk["deleted"]),
            "failed_count": len(bulk["failed"]),
        }
    )


def backup_config_fields() -> list[dict[str, Any]]:
    """Auto-backup fields with per-field value/origin/editable.

    Shared by the HTTP ``_get_backup_config`` handler and the
    ``ha_dev_manage_settings`` developer tool so the two never drift.
    Origin/editable matrix (see ``config.get_backup_setting_origin``):
    ``addon`` editable (Supervisor), ``env`` read-only, ``file``/``default``
    editable (override file).
    """
    settings = get_global_settings()
    fields: list[dict[str, Any]] = []
    for field_name, env_name, _ftype in BACKUP_OVERRIDE_FIELDS:
        origin = get_backup_setting_origin(env_name)
        fields.append(
            {
                "field": field_name,
                "env_var": env_name,
                "value": getattr(settings, field_name),
                "origin": origin,
                "editable": origin in ("addon", "file", "default"),
            }
        )
    return fields


async def _get_backup_config(
    server: HomeAssistantSmartMCPServer | None, _: Request
) -> JSONResponse:
    """Return live auto-backup config + per-field origin + editable flag."""
    return JSONResponse(
        {
            "success": True,
            "is_addon": is_running_in_addon(),
            "fields": backup_config_fields(),
        }
    )


# Inclusive bounds for the integer auto-backup fields; a field absent here
# takes any parseable int. Keyed by the Settings field name.
_BACKUP_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "auto_backup_throttle_minutes": (0, 1440),
    "auto_backup_retain_per_entity": (1, 10_000),
    "auto_backup_calendar_lookahead_days": (1, 365),
    "snapshot_delete_min_age_days": (0, 365),
}


def _coerce_bool_field(field_name: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce a posted value to bool, accepting the JSON/HTML-form spellings."""
    if isinstance(raw, bool):
        return raw, None
    if isinstance(raw, int):
        return bool(raw), None
    if isinstance(raw, str):
        s = raw.strip().lower()
        if s in ("true", "1", "yes", "on"):
            return True, None
        if s in ("false", "0", "no", "off"):
            return False, None
        return None, f"Invalid boolean for {field_name}: {raw!r}"
    return None, f"Invalid value for {field_name}: {raw!r}"


def _coerce_int_field(field_name: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce a posted value to a bounds-checked int (bools are rejected)."""
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return None, f"Invalid integer for {field_name}: {raw!r}"
    try:
        value = int(raw)
    except (ValueError, TypeError):
        return None, f"Invalid integer for {field_name}: {raw!r}"
    bounds = _BACKUP_INT_BOUNDS.get(field_name)
    if bounds is not None and not (bounds[0] <= value <= bounds[1]):
        return None, f"{field_name} must be {bounds[0]}..{bounds[1]}"
    return value, None


def _coerce_str_field(field_name: str, raw: Any) -> tuple[Any, str | None]:
    """Coerce a posted value to a null-byte-free str."""
    if not isinstance(raw, str):
        return None, f"Invalid string for {field_name}: {raw!r}"
    if "\x00" in raw:
        return None, f"{field_name} must not contain null bytes"
    return raw, None


def _validate_backup_payload(payload: Any) -> tuple[dict[str, Any], str | None]:
    """Coerce and bounds-check the POST body. Returns (clean, error_msg)."""
    if not isinstance(payload, dict):
        return {}, "Body must be a JSON object"
    clean: dict[str, Any] = {}
    for field_name, _env_name, ftype in BACKUP_OVERRIDE_FIELDS:
        if field_name not in payload:
            continue
        raw = payload[field_name]
        if ftype is bool:
            value, err = _coerce_bool_field(field_name, raw)
        elif ftype is int:
            value, err = _coerce_int_field(field_name, raw)
        elif ftype is str:
            value, err = _coerce_str_field(field_name, raw)
        else:
            continue
        if err is not None:
            return {}, err
        clean[field_name] = value
    if not clean:
        return {}, "No editable auto-backup fields in body"
    return clean, None


def _filter_env_pinned_backup_fields(clean: dict[str, Any]) -> list[dict[str, str]]:
    """Drop env-pinned fields from ``clean`` in place, returning the rejects.

    Standalone (file) mode only: an env var (process or ``.env``) wins over
    the override file, so a UI edit to such a field is refused rather than
    silently no-op'd at the next Settings read.
    """
    rejected: list[dict[str, str]] = []
    for field_name in list(clean.keys()):
        env_name = next(en for fn, en, _ in BACKUP_OVERRIDE_FIELDS if fn == field_name)
        if os.environ.get(env_name) is not None:
            rejected.append({"field": field_name, "env_var": env_name})
            del clean[field_name]
    return rejected


async def _save_backup_config_addon(
    server: HomeAssistantSmartMCPServer | None, clean: dict[str, Any]
) -> JSONResponse:
    """Add-on-mode branch of ``_save_backup_config``: merge ``clean`` into the
    Supervisor add-on options (full-replacement) and return.

    ``start.py`` re-derives env vars from ``config.yaml`` on the next add-on
    boot, but the actual restart is fired by the user's global Restart Add-on
    button — not here — so ``restart_required=True`` and no auto-restart.
    """
    if server is None:
        # Addon mode without a live server means we're in the stdio sidecar
        # — but addon detection should already be False there. Defensive
        # guard for type-checker + future refactors.
        return _bad_request(
            "Backup settings POST requires a live MCP server",
            code=ErrorCode.INTERNAL_ERROR,
            status=500,
        )
    # Merge ``clean`` into the *full* current options before posting.
    # Supervisor validates against the addon schema and rejects any body
    # missing a required key (notably ``backup_hint`` on the production /
    # dev manifests). A previous version shipped just the auto-backup
    # fields, producing ``addon_configuration_invalid_error: Missing option
    # 'backup_hint' in root`` from supervisor and a confusing 400 in the UI.
    ok, sup_err = await _supervisor._supervisor_merge_and_post_options(
        server.settings.verify_ssl, clean
    )
    if not ok:
        assert sup_err is not None
        logger.warning(
            "Supervisor backup-config update failed (%s): %s",
            sup_err.kind,
            sup_err.message,
        )
        code = (
            ErrorCode.CONNECTION_FAILED
            if sup_err.kind == "transport"
            else ErrorCode.CONFIG_VALIDATION_FAILED
        )
        return JSONResponse(
            create_error_response(code, sup_err.message),
            status_code=sup_err.status_code,
        )
    # Unified restart flow — see _save_feature_flags for the rationale. Don't
    # auto-restart from a save handler; the global Restart Add-on button is
    # the single restart path.
    return JSONResponse(
        {"success": True, "applied": clean, "mode": "addon", "restart_required": True}
    )


def _apply_backup_config_file(clean: dict[str, Any]) -> JSONResponse:
    """Standalone/file-mode branch of ``apply_backup_config``.

    Refuse any field pinned by an env var (process or ``.env``) — 409 with
    the offending names so the UI can refresh and show the read-only banner.
    Editable fields merge into ``<data_dir>/backup_settings.json`` and a
    Settings cache reset publishes them immediately, hence
    ``restart_required=False``.
    """
    rejected = _filter_env_pinned_backup_fields(clean)
    if rejected:
        return JSONResponse(
            {
                "success": False,
                "error": {
                    "code": "env_var_pinned",
                    "message": (
                        "Some fields are set via environment variable and "
                        "cannot be changed from the web UI. Unset the "
                        "env var(s) and reload."
                    ),
                    "rejected": rejected,
                },
            },
            status_code=409,
        )
    if not clean:
        return _bad_request("No editable auto-backup fields after env-var filter")
    current = _persistence._load_backup_settings_override()
    current.update(clean)
    if not _persistence._save_backup_settings_override(current):
        return JSONResponse(
            create_error_response(
                ErrorCode.CONFIG_VALIDATION_FAILED,
                "Failed to persist override file",
            ),
            status_code=500,
        )
    # Drop the cached Settings so the next read sees the merged value.
    # File-mode auto-backup settings take effect immediately on the next
    # ``get_global_settings()`` read — no restart needed, hence
    # ``restart_required=False``.
    _reset_global_settings()
    return JSONResponse(
        {"success": True, "applied": clean, "mode": "file", "restart_required": False}
    )


async def apply_backup_config(
    server: HomeAssistantSmartMCPServer | None, clean: dict[str, Any]
) -> JSONResponse:
    """Route already-validated auto-backup fields to their persistence path.

    Addon mode POSTs to Supervisor (``_save_backup_config_addon``);
    standalone/file mode merges the override file (``_apply_backup_config_file``).
    Shared core of the HTTP save handler (``_save_backup_config``) and the
    ``ha_dev_manage_settings`` developer tool so both take identical
    addon-vs-file routing and env-pin rejection — ``clean`` must already be
    validated by ``_validate_backup_payload``.
    """
    if is_running_in_addon():
        # ``is_running_in_addon()`` checks SUPERVISOR_TOKEN — the
        # ``_save_backup_config_addon`` helper also catches the missing-token
        # ``RuntimeError`` from ``make_supervisor_httpx_client`` as
        # defense-in-depth.
        return await _save_backup_config_addon(server, clean)
    # Same write-guard treatment as tool_config/tool_policy: the RMW is
    # synchronous (safe in-process), but the guard's file lock keeps a
    # writer in another process from interleaving with it.
    from ..utils.config_write_lock import config_write_guard

    async with config_write_guard():
        return _apply_backup_config_file(clean)


async def _save_backup_config(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    """Persist auto-backup config edits and publish to the live process.

    Parses and validates the request body, then delegates the addon-vs-file
    routing to ``apply_backup_config`` (shared with the developer tool).
    """
    try:
        payload = await request.json()
    except (ValueError, json.JSONDecodeError):
        return _bad_request("Invalid JSON body")
    clean, err = _validate_backup_payload(payload)
    if err is not None:
        return _bad_request(err)
    return await apply_backup_config(server, clean)


def build_backups_handlers(
    server: HomeAssistantSmartMCPServer | None,
) -> dict[str, Any]:
    """Construct the backup snapshot + auto-backup-config route handlers."""

    async def list_backups(request: Request) -> JSONResponse:
        return await _list_backups(server, request)

    async def view_backup(request: Request) -> JSONResponse:
        return await _view_backup(server, request)

    async def diff_backup(request: Request) -> JSONResponse:
        return await _diff_backup(server, request)

    async def restore_backup(request: Request) -> JSONResponse:
        return await _restore_backup(server, request)

    async def delete_backup(request: Request) -> JSONResponse:
        return await _delete_backup(server, request)

    async def delete_backups_bulk(request: Request) -> JSONResponse:
        return await _delete_backups_bulk(server, request)

    async def get_backup_config(request: Request) -> JSONResponse:
        return await _get_backup_config(server, request)

    async def save_backup_config(request: Request) -> JSONResponse:
        return await _save_backup_config(server, request)

    return {
        "list_backups": list_backups,
        "view_backup": view_backup,
        "diff_backup": diff_backup,
        "restore_backup": restore_backup,
        "delete_backup": delete_backup,
        "delete_backups_bulk": delete_backups_bulk,
        "get_backup_config": get_backup_config,
        "save_backup_config": save_backup_config,
    }
