"""Export or restore HA-MCP's portable settings from the administrator panel."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from . import (
    _apply_allowed_paths,
    _apply_extra_yaml_keys,
    _load_allowed_paths,
    _load_extra_yaml_keys,
)
from . import const as c
from . import settings_transfer_schema as schema
from .embedded_entry import _reload_relevant
from .settings_transfer_schema import InvalidBackup

_LOGGER = logging.getLogger(__name__)


def _entries(hass: HomeAssistant) -> dict[str, ConfigEntry]:
    entries = {
        entry.data.get(c.CONF_ENTRY_TYPE, c.ENTRY_TYPE_TOOLS): entry
        for entry in hass.config_entries.async_entries(c.DOMAIN)
    }
    if c.ENTRY_TYPE_SERVER not in entries:
        raise InvalidBackup("server_unavailable")
    return entries


async def _wheel(hass: HomeAssistant) -> ModuleType:
    # A developer package override may predate transfer support. Load the
    # installed wheel at use time, never during component registration.
    try:
        return await hass.async_add_executor_job(
            import_module, "ha_mcp.settings_transfer"
        )
    except ImportError as err:
        raise InvalidBackup("server_unavailable") from err


async def _tools_settings(hass: HomeAssistant) -> dict[str, list[str]]:
    return {
        "allowed_dirs": await _load_allowed_paths(hass),
        "extra_yaml_keys": await _load_extra_yaml_keys(hass),
    }


async def async_export(hass: HomeAssistant) -> dict[str, Any]:
    entries = _entries(hass)
    wheel = await _wheel(hass)
    result = {c.ENTRY_TYPE_SERVER: schema.portable_server(entries[c.ENTRY_TYPE_SERVER])}
    if c.ENTRY_TYPE_TOOLS in entries:
        result[c.ENTRY_TYPE_TOOLS] = await _tools_settings(hass)
    directory = Path(hass.config.path(c.SERVER_CONFIG_SUBDIR))
    settings = await hass.async_add_executor_job(
        wheel.export_server_settings, directory
    )
    bundle = {
        "format": schema.FORMAT,
        "version": schema.VERSION,
        "contains_credentials": True,
        "entries": result,
        "server_settings": settings,
    }
    return await hass.async_add_executor_job(
        schema.validate, bundle, Path(hass.config.config_dir)
    )


async def _restore_tools(
    hass: HomeAssistant,
    existing: bool,
    tools: dict[str, Any],
    rollback: AsyncExitStack,
) -> None:
    old = await _tools_settings(hass)
    if old["allowed_dirs"] != tools["allowed_dirs"]:
        _, rejected = await _apply_allowed_paths(hass, tools["allowed_dirs"])
        rollback.push_async_callback(_apply_allowed_paths, hass, old["allowed_dirs"])
        if rejected:
            raise InvalidBackup("invalid_backup")
    if old["extra_yaml_keys"] != tools["extra_yaml_keys"]:
        await _apply_extra_yaml_keys(hass, tools["extra_yaml_keys"])
        rollback.push_async_callback(
            _apply_extra_yaml_keys, hass, old["extra_yaml_keys"]
        )
    if not existing:
        flow = await hass.config_entries.flow.async_init(
            c.DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={c.CONF_ENTRY_TYPE: c.ENTRY_TYPE_TOOLS},
        )
        if flow["type"] != FlowResultType.CREATE_ENTRY:
            raise InvalidBackup("restore_failed")
        rollback.push_async_callback(
            hass.config_entries.async_remove, flow["result"].entry_id
        )


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
    entries = _entries(hass)
    wheel = await _wheel(hass)
    checked = await hass.async_add_executor_job(
        schema.validate, bundle, Path(hass.config.config_dir)
    )
    settings = checked["server_settings"]
    server = checked["entries"][c.ENTRY_TYPE_SERVER]
    hook = (
        server["options"].get(c.OPT_WEBHOOK_ID_OVERRIDE)
        or server["data"][c.DATA_WEBHOOK_ID]
    )
    current_hook = hass.data.get(c.DOMAIN, {}).get(c.DATA_WEBHOOK, {}).get("webhook_id")
    if hook != current_hook and hook in hass.data.get(webhook.DOMAIN, {}):
        raise InvalidBackup("webhook_in_use")
    try:
        await hass.async_add_executor_job(wheel.validate_server_settings, settings)
    except ValueError as err:
        raise InvalidBackup("invalid_backup") from err
    directory = Path(hass.config.path(c.SERVER_CONFIG_SUBDIR))
    old_settings = await hass.async_add_executor_job(
        wheel.export_server_settings, directory
    )
    files_changed = settings != old_settings
    async with AsyncExitStack() as rollback:
        if files_changed:
            try:
                await hass.async_add_executor_job(
                    wheel.restore_server_settings, directory, settings
                )
            except wheel.DecisionPinRequired as err:
                raise InvalidBackup("decision_pin_required") from err
            rollback.push_async_callback(
                hass.async_add_executor_job,
                wheel.restore_server_settings,
                directory,
                old_settings,
            )
        if tools := checked["entries"].get(c.ENTRY_TYPE_TOOLS):
            await _restore_tools(hass, c.ENTRY_TYPE_TOOLS in entries, tools, rollback)
        restart = _restore_entry(
            hass,
            entries[c.ENTRY_TYPE_SERVER],
            server,
            files_changed,
        )
        rollback.pop_all()
    return restart


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


async def _transfer(hass: HomeAssistant, body: Any, exporting: bool) -> web.Response:
    lock = hass.data.setdefault("ha_mcp_tools_settings_transfer_lock", asyncio.Lock())
    async with lock:
        if exporting:
            if body != {}:
                return _error("invalid_request", 400)
            bundle = await async_export(hass)
            payload = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
            if len(payload) > schema.MAX_BYTES:
                raise _TooLarge
            return web.Response(
                body=payload,
                content_type="application/json",
                headers={
                    "Content-Disposition": 'attachment; filename="ha-mcp-settings.json"',
                    "Cache-Control": "no-store",
                },
            )
        restart = await async_import(hass, body)
        return web.json_response(
            {"success": True, "restart_required": restart},
            headers={"Cache-Control": "no-store"},
        )


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
        return _error("origin_denied", 403)
    if request.content_type != "application/json":
        return _error("invalid_request", 415)
    try:
        body = await _read_json(request)
        # A disconnect during an executor write must let the transaction finish.
        return await asyncio.shield(
            hass.async_create_task(_transfer(hass, body, exporting))
        )
    except InvalidBackup as err:
        code = str(err)
        return _error(
            code, 409 if code in {"server_unavailable", "webhook_in_use"} else 400
        )
    except _TooLarge:
        return _error("too_large", 413)
    except json.JSONDecodeError, UnicodeError, RecursionError:
        return _error("invalid_backup", 400)
    except Exception as err:  # noqa: BLE001 -- never emit uploaded credentials
        _LOGGER.error("HA-MCP settings transfer failed (%s)", type(err).__name__)
        return _error("restore_failed", 500)
