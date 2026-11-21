"""Export or restore HA-MCP's portable settings from the administrator panel."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, nullcontext
from functools import partial
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from . import (
    _apply_allowed_paths_locked,
    _apply_extra_yaml_keys_locked,
)
from . import const as c
from . import settings_transfer_schema as schema
from .embedded_entry import _reload_relevant
from .settings_transfer_schema import RefusalCode, TransferRefused
from .tools_settings_store import (
    ALLOWED_PATHS_KEY,
    EXTRA_YAML_KEYS_KEY,
    ToolsStoreUnavailable,
    load_tools_setting,
    save_allowed_paths,
    save_extra_yaml_keys,
    store_writes,
)

_LOGGER = logging.getLogger(__name__)
# Must equal the wheel's INCOMPLETE_ROLLBACK_NOTE, which notes its own failed
# undo; copied because the wheel is imported lazily.
_INCOMPLETE_ROLLBACK = "Settings import rollback incomplete"
_WHEEL_MODULES = {"ha_mcp", "ha_mcp.settings_transfer"}


def _entries(hass: HomeAssistant) -> dict[str, ConfigEntry]:
    entries = {
        entry.data.get(c.CONF_ENTRY_TYPE, c.ENTRY_TYPE_TOOLS): entry
        for entry in hass.config_entries.async_entries(c.DOMAIN, include_ignore=False)
    }
    if c.ENTRY_TYPE_SERVER not in entries:
        raise TransferRefused(RefusalCode.SERVER_UNAVAILABLE)
    return entries


def _load_wheel() -> ModuleType:
    """Import lazily and off-loop: an older pinned wheel has no settings transfer."""
    try:
        from ha_mcp import settings_transfer
    except ImportError as err:
        if not (isinstance(err, ModuleNotFoundError) and err.name in _WHEEL_MODULES):
            _LOGGER.exception("The HA-MCP server package failed to import")
        raise TransferRefused(RefusalCode.SERVER_UNAVAILABLE) from err
    return settings_transfer


_TOOLS_STORES = {
    "allowed_dirs": (
        ALLOWED_PATHS_KEY,
        "paths",
        _apply_allowed_paths_locked,
        save_allowed_paths,
    ),
    "extra_yaml_keys": (
        EXTRA_YAML_KEYS_KEY,
        "keys",
        _apply_extra_yaml_keys_locked,
        save_extra_yaml_keys,
    ),
}


async def _read_tools_setting(hass: HomeAssistant, name: str) -> list[str]:
    key, field, _, _ = _TOOLS_STORES[name]
    # Startup intentionally falls back to no grants on a read error. A backup
    # must distinguish a missing setting from a store it could not read.
    return await load_tools_setting(hass, key, field) or []


async def _tools_settings(hass: HomeAssistant) -> dict[str, list[str]]:
    return {name: await _read_tools_setting(hass, name) for name in _TOOLS_STORES}


async def _write_tools_setting(
    hass: HomeAssistant,
    name: str,
    values: list[str],
    entry: ConfigEntry | None,
    *,
    strict: bool = True,
) -> None:
    _, _, apply, save = _TOOLS_STORES[name]
    if entry is not None and entry.state is ConfigEntryState.LOADED:
        _, rejected = await apply(hass, values)
        # An undo restores raw stored values; apply() drops what startup would.
        if rejected and strict:
            raise TransferRefused(RefusalCode.INVALID_BACKUP)
    else:
        # Unloaded entries have no live permissions cache. Setup loads the
        # saved values, including on the first setup of a newly created entry.
        await save(hass, values)


async def async_export(hass: HomeAssistant) -> dict[str, Any]:
    entries = _entries(hass)
    wheel = await hass.async_add_executor_job(_load_wheel)
    result = {c.ENTRY_TYPE_SERVER: schema.portable_server(entries[c.ENTRY_TYPE_SERVER])}
    if tools_entry := entries.get(c.ENTRY_TYPE_TOOLS):
        tools: dict[str, Any] = dict(await _tools_settings(hass))
        if tools_entry.disabled_by is not None:
            tools["disabled"] = True
        result[c.ENTRY_TYPE_TOOLS] = tools
    directory = Path(hass.config.path(c.SERVER_CONFIG_SUBDIR))
    try:
        settings = await hass.async_add_executor_job(
            wheel.export_server_settings, directory
        )
    except wheel.LocalSettingsUnusable as err:
        raise TransferRefused(RefusalCode.INVALID_SERVER_SETTINGS) from err
    bundle = {
        "format": schema.FORMAT,
        "version": schema.VERSION,
        "contains_credentials": True,
        "entries": result,
        "server_settings": settings,
    }
    return await hass.async_add_executor_job(
        partial(schema.validate, stored=True), bundle, Path(hass.config.config_dir)
    )


def _check_tools_entry(
    source: dict[str, Any] | None,
    target: ConfigEntry | None,
    *,
    allow_create: bool,
) -> None:
    """Keep the destination's privileged services consistent with the backup."""
    if source is None:
        if target is not None:
            raise TransferRefused(RefusalCode.EXTRA_TOOLS_ENTRY)
        return
    disabled = target is not None and target.disabled_by is not None
    if source.get("disabled", False) != disabled:
        # The enabled state must already match. HA's config flow sets up a new
        # entry immediately, so creating one for a disabled source would grant
        # file access before it could be disabled.
        raise TransferRefused(
            RefusalCode.TOOLS_MUST_BE_ENABLED
            if disabled
            else RefusalCode.TOOLS_MUST_BE_DISABLED
        )
    if target is None and not allow_create:
        raise TransferRefused(RefusalCode.RESTORE_FAILED)


def _tools_setup_lock(entry: ConfigEntry | None) -> asyncio.Lock | nullcontext[None]:
    """Prevent setup and unload while importing into an existing entry."""
    if entry is None:
        return nullcontext()
    if entry.setup_lock.locked() or entry.state in (
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
    ):
        raise TransferRefused(RefusalCode.TOOLS_ENTRY_BUSY)
    if entry.disabled_by is None and entry.state is not ConfigEntryState.LOADED:
        raise TransferRefused(RefusalCode.TOOLS_ENTRY_UNAVAILABLE)
    return entry.setup_lock


def _add_undo(
    stack: AsyncExitStack,
    label: str,
    undo: Callable[..., Awaitable[Any]],
    *args: Any,
) -> None:
    """Report an undo failure while preserving the error that started rollback."""

    async def unwind(
        _exc_type: type[BaseException] | None,
        original: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        try:
            await undo(*args)
        except Exception as err:
            if original is None:
                raise
            # Only the fixed step label and error type are safe to log.
            _LOGGER.error(
                "Settings import could not undo %s (%s)", label, type(err).__name__
            )
            original.add_note(_INCOMPLETE_ROLLBACK)

    stack.push_async_exit(unwind)


async def _rollback_tools_setting(
    hass: HomeAssistant,
    name: str,
    revision: int,
    before: list[str],
    imported: list[str],
    entry: ConfigEntry | None,
) -> None:
    writes = store_writes(hass, _TOOLS_STORES[name][0])
    async with writes.lock:
        current = (
            await _read_tools_setting(hass, name)
            if writes.revision == revision
            else None
        )
        if current not in (imported, before):
            _LOGGER.warning("Kept %s because it changed after settings import", name)
            return
        # A save can land even when its verification read fails, leaving the
        # live cache at the old value; rewriting before re-syncs both.
        await _write_tools_setting(hass, name, before, entry, strict=False)
        writes.bump()


async def _restore_tools(
    hass: HomeAssistant,
    entry: ConfigEntry | None,
    tools: dict[str, Any],
    rollback: AsyncExitStack,
) -> None:
    for name, (key, _, _, _) in _TOOLS_STORES.items():
        writes = store_writes(hass, key)
        async with writes.lock:
            previous = await _read_tools_setting(hass, name)
            if previous == tools[name]:
                continue
            _add_undo(
                rollback,
                name,
                _rollback_tools_setting,
                hass,
                name,
                writes.bump(),
                previous,
                tools[name],
                entry,
            )
            await _write_tools_setting(hass, name, tools[name], entry)
    if entry is None:
        flow = await hass.config_entries.flow.async_init(
            c.DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={c.CONF_ENTRY_TYPE: c.ENTRY_TYPE_TOOLS},
        )
        if flow["type"] != FlowResultType.CREATE_ENTRY:
            _LOGGER.warning(
                "Settings import could not add the File & YAML Tools entry (%s)",
                flow.get("reason", flow["type"]),
            )
            raise TransferRefused(RefusalCode.RESTORE_FAILED)
        created = flow["result"]
        _add_undo(
            rollback,
            "created Tools entry",
            hass.config_entries.async_remove,
            created.entry_id,
        )
        if created.state is not ConfigEntryState.LOADED:
            raise TransferRefused(RefusalCode.RESTORE_FAILED)


def _restore_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    server: dict[str, Any],
    files_changed: bool,
) -> bool:
    """Commit the config entry in one update, scheduling at most one worker reload."""
    data, options = schema.restored_entry(entry, server)
    options_reload = _reload_relevant(entry.options) != _reload_relevant(options)
    needs_reload = files_changed or data != entry.data
    # Legacy binds its credentials to HA HTTP routes until HA restarts. Other
    # modes may also need new routes if the webhook was previously disabled.
    was_legacy = entry.options.get(c.OPT_WEBHOOK_AUTH) == c.WEBHOOK_AUTH_LEGACY
    legacy = options.get(c.OPT_WEBHOOK_AUTH) == c.WEBHOOK_AUTH_LEGACY
    restart_required = (
        was_legacy != legacy
        or (
            legacy
            and any(
                data.get(k) != entry.data.get(k)
                for k in (
                    c.DATA_OAUTH_CLIENT_ID,
                    c.DATA_OAUTH_CLIENT_SECRET,
                    c.DATA_OAUTH_SIGNING_KEY,
                )
            )
        )
        or (
            options.get(c.OPT_ENABLE_WEBHOOK, True)
            and not entry.options.get(c.OPT_ENABLE_WEBHOOK, True)
        )
    )
    hass.config_entries.async_update_entry(entry, data=data, options=options)
    if needs_reload and not options_reload and entry.state is ConfigEntryState.LOADED:
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return restart_required


async def async_import(hass: HomeAssistant, bundle: Any) -> bool:
    """Validate every section, retain target-local state and roll back on failure."""
    wheel = await hass.async_add_executor_job(_load_wheel)
    checked = await hass.async_add_executor_job(
        schema.validate, bundle, Path(hass.config.config_dir)
    )
    entries = _entries(hass)
    source_tools = checked["entries"].get(c.ENTRY_TYPE_TOOLS)
    target_tools = entries.get(c.ENTRY_TYPE_TOOLS)
    _check_tools_entry(source_tools, target_tools, allow_create=True)
    settings = checked["server_settings"]
    server = checked["entries"][c.ENTRY_TYPE_SERVER]
    hook = (
        server["options"].get(c.OPT_WEBHOOK_ID_OVERRIDE)
        or server["data"][c.DATA_WEBHOOK_ID]
    )
    current_hook = hass.data.get(c.DOMAIN, {}).get(c.DATA_WEBHOOK, {}).get("webhook_id")
    if hook != current_hook and hook in hass.data.get(webhook.DOMAIN, {}):
        raise TransferRefused(RefusalCode.WEBHOOK_IN_USE)
    directory = Path(hass.config.path(c.SERVER_CONFIG_SUBDIR))
    # Keep setup/unload from crossing the choice between live and stored
    # settings. A newly created entry loads the persisted values in its flow.
    async with (
        _tools_setup_lock(target_tools),
        AsyncExitStack() as rollback,
    ):
        try:
            server_changes = await hass.async_add_executor_job(
                wheel.restore_server_settings, directory, settings
            )
        except wheel.DecisionPinRequired as err:
            raise TransferRefused(RefusalCode.DECISION_PIN_REQUIRED) from err
        except wheel.LocalSettingsUnusable as err:
            raise TransferRefused(RefusalCode.INVALID_SERVER_SETTINGS) from err
        except ValueError as err:
            if _INCOMPLETE_ROLLBACK in getattr(err, "__notes__", ()):
                raise
            raise TransferRefused(RefusalCode.INVALID_BACKUP) from err
        if server_changes is not None:
            _add_undo(
                rollback,
                "server settings",
                hass.async_add_executor_job,
                wheel.rollback_server_settings,
                directory,
                server_changes,
            )
        if source_tools is not None:
            await _restore_tools(
                hass, entries.get(c.ENTRY_TYPE_TOOLS), source_tools, rollback
            )
        # Entry enable/remove flows can run while the disk writes are awaited.
        _check_tools_entry(
            source_tools, _entries(hass).get(c.ENTRY_TYPE_TOOLS), allow_create=False
        )
        restart = _restore_entry(
            hass,
            entries[c.ENTRY_TYPE_SERVER],
            server,
            server_changes is not None,
        )
        rollback.pop_all()
    return restart or bool(server_changes and server_changes.restart_required)


class _TooLarge(ValueError):
    """The upload or serialized download exceeds the supported backup size."""


async def _read_json(request: web.Request) -> Any:
    body = bytearray()
    async for chunk in request.content.iter_chunked(64 * 1024):
        if len(body) + len(chunk) > schema.MAX_BYTES:
            raise _TooLarge
        body.extend(chunk)
    return json.loads(body)


def _error(code: str, status: int) -> web.Response:
    return web.json_response(
        {"success": False, "error": code},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _same_origin(request: web.Request) -> bool:
    # Fetch Metadata is generated by the browser and cannot be set by page JS.
    # It also covers HA Cloud and reverse proxies that rewrite HA's origin.
    site = request.headers.get("Sec-Fetch-Site")
    if site is not None:
        return site == "same-origin"
    return request.headers.get("Origin") == str(request.url.origin())


async def _export_response(hass: HomeAssistant, body: Any) -> web.Response:
    if body != {}:
        return _error("invalid_request", 400)
    bundle = await async_export(hass)
    payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
    if len(payload) > schema.MAX_BYTES:
        return _error("too_large", 413)
    return web.Response(
        body=payload,
        content_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="ha-mcp-settings.json"',
            "Cache-Control": "no-store",
        },
    )


async def _import_response(hass: HomeAssistant, body: Any) -> web.Response:
    restart = await async_import(hass, body)
    if restart:
        _LOGGER.warning("Restart Home Assistant to apply the imported HA-MCP settings")
    return web.json_response(
        {"success": True, "restart_required": restart},
        headers={"Cache-Control": "no-store"},
    )


async def _transfer(hass: HomeAssistant, body: Any, exporting: bool) -> web.Response:
    """Run one transfer and report every outcome, including to the log.

    A disconnected browser cancels the request handler, and the shield then
    discards this task's result, so nothing may be left for the handler to log.
    """
    operation = "export" if exporting else "import"
    lock = hass.data.setdefault("ha_mcp_tools_settings_transfer_lock", asyncio.Lock())
    async with lock:
        try:
            if exporting:
                return await _export_response(hass, body)
            return await _import_response(hass, body)
        except Exception as err:
            incomplete = _INCOMPLETE_ROLLBACK in getattr(err, "__notes__", ())
            if not incomplete and (expected := _expected_failure(operation, err)):
                return expected
            _LOGGER.error(
                "Settings %s failed%s",
                operation,
                " with incomplete rollback" if incomplete else "",
                exc_info=_withheld(err),
            )
            return _error(
                "rollback_incomplete" if incomplete else "restore_failed", 500
            )


def _expected_failure(operation: str, err: Exception) -> web.Response | None:
    if isinstance(err, ToolsStoreUnavailable):
        _LOGGER.warning("Settings %s failed (%s)", operation, err.code)
        return _error(err.code, 503)
    if isinstance(err, TransferRefused):
        _LOGGER.info("Settings %s refused (%s)", operation, err.code)
        return _error(err.code, err.code.http_status)
    return None


class _MessageWithheld(Exception):
    """Stands in for an error whose message may echo uploaded values."""


def _withheld(err: Exception) -> _MessageWithheld:
    """Keep the error's type and frames for the log, never its message."""
    return _MessageWithheld(type(err).__name__).with_traceback(err.__traceback__)


async def async_handle(
    hass: HomeAssistant,
    request: web.Request,
    *,
    exporting: bool,
) -> web.Response:
    """Handle only the two reserved sidebar routes after live admin authentication."""
    if request.method != "POST":
        return _error("invalid_request", 405)
    if not _same_origin(request):
        _LOGGER.warning("Refused a settings transfer request from another origin")
        return _error("origin_denied", 403)
    if request.content_type != "application/json":
        return _error("invalid_request", 415)
    try:
        body = await _read_json(request)
    except _TooLarge:
        return _error("too_large", 413)
    except json.JSONDecodeError, UnicodeError, RecursionError:
        return _error("invalid_backup", 400)
    # A disconnect during an executor write must let the transaction finish.
    return await asyncio.shield(
        hass.async_create_task(_transfer(hass, body, exporting))
    )
