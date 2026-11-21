"""Settings export and import on the server's own settings page.

Home Assistant's HA-MCP sidebar panel answers these routes itself, before
its proxy forwards anything, so that it can also move the HA-MCP Server and
File & YAML Tools entries. Every other way of opening the page (standalone,
Docker, the app (add-on), the stdio sidecar) reaches these handlers: they move
the settings files, the File & YAML Tools permissions through the component's
services, and in the app the settings it keeps as Supervisor options.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .. import settings_transfer as transfer
from .._version import is_running_in_addon
from ..config import reset_global_settings
from ..tools.util_helpers import unwrap_service_response
from ..utils.config_write_lock import get_config_write_lock
from ..utils.data_paths import get_data_dir
from . import _supervisor
from ._handlers_fs import _fs_custom_paths_call

if TYPE_CHECKING:
    from ..server import HomeAssistantSmartMCPServer

logger = logging.getLogger(__name__)

# Backup field -> (read service, write service, list key in both).
_TOOLS_SERVICES = {
    "allowed_dirs": ("get_allowed_paths", "set_allowed_paths", "paths"),
    "extra_yaml_keys": ("get_extra_yaml_keys", "set_extra_yaml_keys", "keys"),
}
# Settings the running server reads only at start-up.
_RESTART_DOCUMENTS = {"tools", "features"}
# Sections whose app-managed fields the app (add-on) keeps as its options.
_APP_SECTIONS = ("features", "backup")


class TransferFailed(Exception):
    """A refused or failed transfer, with its panel code and HTTP status."""

    def __init__(self, code: str, status: int) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error": code},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def _read_body(request: Request) -> Any:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise TransferFailed("invalid_request", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > transfer.MAX_BYTES:
            raise TransferFailed("too_large", 413)
        body.extend(chunk)
    try:
        return json.loads(body)
    except (ValueError, RecursionError) as err:
        raise TransferFailed("invalid_backup", 400) from err


async def _tools_service(
    server: HomeAssistantSmartMCPServer | None, service: str, data: dict[str, Any]
) -> dict[str, Any]:
    result = await _fs_custom_paths_call(server, service, data)
    reply = unwrap_service_response(result) if isinstance(result, dict) else {}
    if not isinstance(reply, dict) or not reply.get("success", False):
        raise RuntimeError(f"ha_mcp_tools.{service} refused the request")
    return reply


async def _read_tools(
    server: HomeAssistantSmartMCPServer | None,
) -> dict[str, list[str]] | None:
    """The component's permission lists, or None without File & YAML Tools."""
    values = {}
    try:
        for name, (read, _, key) in _TOOLS_SERVICES.items():
            values[name] = list((await _tools_service(server, read, {}))[key])
    except Exception:
        # No File & YAML Tools entry (or no component) leaves nothing to move.
        logger.debug("File & YAML Tools permissions unavailable", exc_info=True)
        return None
    return values


async def export_bundle(server: HomeAssistantSmartMCPServer | None) -> dict[str, Any]:
    """This server's settings file: no connection, which only the HA entry has."""
    try:
        settings = await asyncio.to_thread(
            transfer.export_server_settings, get_data_dir()
        )
    except transfer.LocalSettingsUnusable as err:
        raise TransferFailed("invalid_server_settings", 400) from err
    if is_running_in_addon():
        live = await _supervisor._live_addon_options(server)
        for section in _APP_SECTIONS:
            app = {k: live[k] for k in transfer.app_option_fields(section) if k in live}
            settings[section] = {**(settings[section] or {}), **app} or None
        settings = transfer.validate_server_settings(settings, strict=False)
    tools = await _read_tools(server)
    return {
        "format": transfer.FORMAT,
        "version": transfer.VERSION,
        "contains_credentials": False,
        "entries": {} if tools is None else {"tools": tools},
        "server_settings": settings,
    }


async def _export(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> Response:
    try:
        if await _read_body(request) != {}:
            return _error("invalid_request", 400)
        bundle = await export_bundle(server)
    except TransferFailed as err:
        return _error(err.code, err.status)
    return Response(
        json.dumps(bundle, ensure_ascii=False).encode("utf-8"),
        media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="ha-mcp-settings.json"',
            "Cache-Control": "no-store",
        },
    )


def _without_app_options(settings: dict[str, Any]) -> dict[str, Any]:
    """``settings`` minus what the app (add-on) keeps in Supervisor instead."""
    if not is_running_in_addon():
        return settings
    trimmed = dict(settings)
    for section in _APP_SECTIONS:
        names = transfer.app_option_fields(section)
        trimmed[section] = {
            k: v for k, v in (settings[section] or {}).items() if k not in names
        }
    return trimmed


async def _restore_files(
    settings: dict[str, Any], limits: ImportLimits | None
) -> transfer.ServerSettingsChanges | None:
    restore = partial(transfer.restore_server_settings)
    if limits is not None:
        # The app's Supervisor options are checked where they are posted.
        restore = partial(
            restore,
            keep=limits.keep,
            allow=lambda old, new: limits.allow(
                _without_app_options(old), _without_app_options(new)
            ),
        )
    try:
        return await asyncio.to_thread(restore, get_data_dir(), settings)
    except transfer.ChangeRefused as err:
        raise TransferFailed("access_required", 403) from err
    except transfer.DecisionPinRequired as err:
        raise TransferFailed("decision_pin_required", 400) from err
    except transfer.LocalSettingsUnusable as err:
        raise TransferFailed("invalid_server_settings", 400) from err
    except (OSError, ValueError) as err:
        if transfer.INCOMPLETE_ROLLBACK_NOTE in getattr(err, "__notes__", ()):
            raise TransferFailed("rollback_incomplete", 500) from err
        if isinstance(err, OSError):
            raise TransferFailed("restore_failed", 500) from err
        raise TransferFailed("invalid_backup", 400) from err


async def _write_tools(
    server: HomeAssistantSmartMCPServer | None,
    current: dict[str, list[str]],
    wanted: dict[str, list[str]],
    limits: ImportLimits | None,
) -> None:
    if limits and not limits.allow({"permissions": current}, {"permissions": wanted}):
        raise TransferFailed("access_required", 403)
    for name, values in wanted.items():
        _, write, key = _TOOLS_SERVICES[name]
        # The component applies what it accepts, so a refusal still wrote.
        if (await _tools_service(server, write, {key: values})).get("rejected"):
            raise TransferFailed("invalid_backup", 400)


async def _apply_app_options(
    server: HomeAssistantSmartMCPServer | None,
    settings: dict[str, Any],
    limits: ImportLimits | None,
) -> bool:
    """Post the app-managed settings to Supervisor; True when any changed."""
    if not is_running_in_addon() or server is None:
        return False
    live = await _supervisor._live_addon_options(server)
    # Only values the file states: an app export states every app option,
    # and another installation's file states only its overrides.
    changes = {
        name: values[name]
        for section in _APP_SECTIONS
        for values in [settings.get(section) or {}]
        for name in transfer.app_option_fields(section)
        if name in live
        and name in values
        and live[name] != values[name]
        and name not in (limits.keep.get(section, ()) if limits else ())
    }
    if not changes:
        return False
    if limits:
        old = {
            s: {k: live.get(k) for k in transfer.app_option_fields(s)}
            for s in _APP_SECTIONS
        }
        new = {
            s: {**old[s], **{k: v for k, v in changes.items() if k in old[s]}}
            for s in _APP_SECTIONS
        }
        if not limits.allow(old, new):
            raise TransferFailed("access_required", 403)
    ok, err = await _supervisor._supervisor_merge_and_post_options(
        server.settings.verify_ssl, changes
    )
    if not ok:
        logger.warning(
            "Settings import could not update the app options (%s)",
            err.kind if err else "unknown",
        )
        raise TransferFailed("restore_failed", 500)
    return True


async def _undo(
    server: HomeAssistantSmartMCPServer | None,
    changes: transfer.ServerSettingsChanges | None,
    tools: dict[str, list[str]] | None,
) -> bool:
    """Put back this import's writes; False when any undo failed."""
    complete = True
    for name, values in (tools or {}).items():
        _, write, key = _TOOLS_SERVICES[name]
        try:
            await _tools_service(server, write, {key: values})
        except Exception:
            logger.exception("Settings import could not undo %s", name)
            complete = False
    if changes is not None:
        try:
            await asyncio.to_thread(
                transfer.rollback_server_settings, get_data_dir(), changes
            )
        except transfer.ServerSettingsRollbackIncomplete:
            complete = False
    return complete


@dataclass(frozen=True)
class ImportLimits:
    """What an import may change, decided against the state it replaces.

    ``keep`` names, per section, settings that keep this server's values;
    ``allow`` sees the replaced and resulting ``policy``, ``visibility``,
    ``features``, ``backup`` and File & YAML Tools ``permissions`` and may
    refuse.
    """

    keep: Mapping[str, Collection[str]]
    allow: Callable[[dict[str, Any], dict[str, Any]], bool]


async def import_bundle(
    server: HomeAssistantSmartMCPServer | None,
    bundle: Any,
    limits: ImportLimits | None = None,
) -> dict[str, Any]:
    """Apply an export's settings and permissions; roll everything back on failure."""
    try:
        backup = transfer.read_backup(bundle)
    except transfer.IncompatibleVersion as err:
        raise TransferFailed("incompatible_version", 400) from err
    except ValueError as err:
        raise TransferFailed("invalid_backup", 400) from err
    skipped = ["connection"] if backup.has_connection else []
    async with get_config_write_lock():
        changes = tools = None
        try:
            changes = await _restore_files(backup.server_settings, limits)
            if backup.tools is not None:
                # Recorded before the first write so any failure can undo it.
                tools = await _read_tools(server)
                if tools is None:
                    skipped.append("file_permissions")
                else:
                    await _write_tools(server, tools, backup.tools, limits)
            app_changed = await _apply_app_options(
                server, backup.server_settings, limits
            )
        except Exception as err:
            if isinstance(err, TransferFailed):
                logger.info("Settings import refused (%s)", err.code)
                failure = err
            else:
                logger.exception("Settings import failed")
                failure = TransferFailed("restore_failed", 500)
            if not await _undo(server, changes, tools):
                failure = TransferFailed("rollback_incomplete", failure.status)
            raise failure from err
    reset_global_settings()
    changed = {d.key for d in changes.documents} if changes else set()
    return {
        "success": True,
        "restart_required": app_changed or bool(changed & _RESTART_DOCUMENTS),
        "restart": "server",
        "skipped": skipped,
    }


async def _import(
    server: HomeAssistantSmartMCPServer | None, request: Request
) -> JSONResponse:
    try:
        result = await import_bundle(server, await _read_body(request))
    except TransferFailed as err:
        return _error(err.code, err.status)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


def build_transfer_handlers(
    server: HomeAssistantSmartMCPServer | None,
) -> dict[str, Any]:
    """Construct the settings export and import route handlers."""

    async def export_settings(request: Request) -> Response:
        return await _export(server, request)

    async def import_settings(request: Request) -> JSONResponse:
        return await _import(server, request)

    return {"export_settings": export_settings, "import_settings": import_settings}
