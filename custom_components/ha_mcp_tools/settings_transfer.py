"""Export or restore HA-MCP's portable settings from the administrator panel."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from pathlib import Path
from types import ModuleType
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.storage import Store

from . import (
    _ALLOWED_PATHS_STORAGE_KEY,
    _ALLOWED_PATHS_STORAGE_VERSION,
    _EXTRA_YAML_KEYS_STORAGE_KEY,
    _EXTRA_YAML_KEYS_STORAGE_VERSION,
    _apply_allowed_paths,
    _apply_extra_yaml_keys,
    _save_allowed_paths,
    _save_extra_yaml_keys,
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


def _load_wheel() -> ModuleType:
    """Load the paired wheel off-loop, allowing overrides that predate transfer."""
    try:
        from ha_mcp import settings_transfer
    except ImportError as err:
        raise InvalidBackup("server_unavailable") from err
    return settings_transfer


_TOOLS_STORES = {
    "allowed_dirs": (
        _ALLOWED_PATHS_STORAGE_KEY,
        _ALLOWED_PATHS_STORAGE_VERSION,
        "paths",
        _apply_allowed_paths,
        _save_allowed_paths,
    ),
    "extra_yaml_keys": (
        _EXTRA_YAML_KEYS_STORAGE_KEY,
        _EXTRA_YAML_KEYS_STORAGE_VERSION,
        "keys",
        _apply_extra_yaml_keys,
        _save_extra_yaml_keys,
    ),
}


async def _read_tools_setting(hass: HomeAssistant, name: str) -> list[str]:
    key, version, field, _, _ = _TOOLS_STORES[name]
    # Startup intentionally falls back to no grants on a read error. A backup
    # must distinguish a missing setting from a store it could not read.
    store: Store[dict[str, list[str]]] = Store(hass, version, key)
    data = await store.async_load()
    if data is None:
        return []
    if not isinstance(data, dict) or not isinstance(data.get(field), list):
        raise InvalidBackup("invalid_backup")
    return data[field]


async def _tools_settings(hass: HomeAssistant) -> dict[str, list[str]]:
    return {name: await _read_tools_setting(hass, name) for name in _TOOLS_STORES}


async def _write_tools_setting(
    hass: HomeAssistant,
    name: str,
    values: list[str],
    entry: ConfigEntry | None,
) -> None:
    _, _, _, apply, save = _TOOLS_STORES[name]
    if entry is not None and entry.state is ConfigEntryState.LOADED:
        _, rejected = await apply(hass, values)
        if rejected:
            raise InvalidBackup("invalid_backup")
    else:
        # Unloaded entries have no live permissions cache. Setup loads the
        # saved values, including on the first setup of a newly created entry.
        await save(hass, values)
    # HA Store logs certain write failures and returns normally. Read back
    # through a fresh Store before reporting that the transfer succeeded.
    if await _read_tools_setting(hass, name) != values:
        raise OSError("Could not persist File & YAML Tools settings")


async def async_export(hass: HomeAssistant) -> dict[str, Any]:
    entries = _entries(hass)
    wheel = await hass.async_add_executor_job(_load_wheel)
    result = {c.ENTRY_TYPE_SERVER: schema.portable_server(entries[c.ENTRY_TYPE_SERVER])}
    if c.ENTRY_TYPE_TOOLS in entries:
        result[c.ENTRY_TYPE_TOOLS] = await _tools_settings(hass)
    directory = Path(hass.config.path(c.SERVER_CONFIG_SUBDIR))
    try:
        settings = await hass.async_add_executor_job(
            wheel.export_server_settings, directory
        )
    except ValueError as err:
        raise InvalidBackup("invalid_server_settings") from err
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
    entry: ConfigEntry | None,
    tools: dict[str, Any],
    rollback: AsyncExitStack,
) -> None:
    old = await _tools_settings(hass)
    for name, previous in old.items():
        if previous != tools[name]:
            rollback.push_async_callback(
                _write_tools_setting, hass, name, previous, entry
            )
            await _write_tools_setting(hass, name, tools[name], entry)
    if entry is None:
        flow = await hass.config_entries.flow.async_init(
            c.DOMAIN,
            context={"source": SOURCE_IMPORT},
            data={c.CONF_ENTRY_TYPE: c.ENTRY_TYPE_TOOLS},
        )
        if flow["type"] != FlowResultType.CREATE_ENTRY:
            raise InvalidBackup("restore_failed")
        created = flow["result"]
        rollback.push_async_callback(hass.config_entries.async_remove, created.entry_id)
        if created.state is not ConfigEntryState.LOADED:
            raise InvalidBackup("restore_failed")


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
    wheel = await hass.async_add_executor_job(_load_wheel)
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
    directory = Path(hass.config.path(c.SERVER_CONFIG_SUBDIR))
    async with AsyncExitStack() as rollback:
        try:
            old_settings = await hass.async_add_executor_job(
                wheel.restore_server_settings, directory, settings
            )
        except wheel.DecisionPinRequired as err:
            raise InvalidBackup("decision_pin_required") from err
        except ValueError as err:
            raise InvalidBackup("invalid_backup") from err
        if old_settings is not None:
            rollback.push_async_callback(
                hass.async_add_executor_job,
                wheel.restore_server_settings,
                directory,
                old_settings,
            )
        if tools := checked["entries"].get(c.ENTRY_TYPE_TOOLS):
            await _restore_tools(hass, entries.get(c.ENTRY_TYPE_TOOLS), tools, rollback)
        restart = _restore_entry(
            hass,
            entries[c.ENTRY_TYPE_SERVER],
            server,
            old_settings is not None,
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
        code = err.code
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
