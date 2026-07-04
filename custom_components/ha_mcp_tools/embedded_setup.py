"""Orchestrate the in-process ha-mcp server for the config entry (issue #1527).

Thin glue between :mod:`embedded_server` (the server thread + token provisioning)
and :mod:`mcp_webhook` (the ingress webhook), plus the entry-level concerns:
secret generation, repair issues on failure, and surfacing the connect URLs. Kept
out of ``__init__.py`` so the large services module stays focused and this
opt-in path is independently testable.

Every failure here is contained: the integration's file/YAML services must keep
working even when the embedded server can't be installed or started, so a failure
files a repair issue and returns rather than propagating out of
``async_setup_entry``.
"""

from __future__ import annotations

import logging
import secrets
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DATA_EMBEDDED_MANAGER,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ISSUE_EMBEDDED_PACKAGE_FAILED,
    ISSUE_EMBEDDED_START_FAILED,
    OPT_BIND_HOST,
    OPT_EMBEDDED_ENABLED,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_NONE,
)
from .embedded_server import EmbeddedServerError, EmbeddedServerManager
from .mcp_webhook import async_register_webhook, async_unregister_webhook

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = "ha_mcp_tools_embedded_server"
_EMBEDDED_ISSUE_IDS = (ISSUE_EMBEDDED_PACKAGE_FAILED, ISSUE_EMBEDDED_START_FAILED)


def embedded_enabled(entry: ConfigEntry) -> bool:
    """Return True when the options enable the in-process server."""
    return bool(entry.options.get(OPT_EMBEDDED_ENABLED, False))


async def async_setup_embedded_server(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Bring up the embedded server + ingress webhook if enabled (never raises).

    When disabled, revokes any leftover provisioned token so a previously-enabled
    install cleans up. On failure, files a repair issue and returns — the
    integration's other services stay up.
    """
    if not embedded_enabled(entry):
        await _async_cleanup_disabled(hass, entry)
        return

    _clear_issues(hass)
    _ensure_secrets(hass, entry)

    manager = EmbeddedServerManager(hass, entry)
    hass.data.setdefault(DOMAIN, {})[DATA_EMBEDDED_MANAGER] = manager

    try:
        await manager.async_start()
    except EmbeddedServerError as err:
        _LOGGER.error("Embedded ha-mcp server failed to start: %s", err)
        # Best-effort teardown so no half-started thread lingers.
        await manager.async_stop()
        hass.data.get(DOMAIN, {}).pop(DATA_EMBEDDED_MANAGER, None)
        _create_issue(hass, str(err))
        return

    auth_mode = str(entry.options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE))
    secret_path = str(entry.data[DATA_SECRET_PATH])
    try:
        await async_register_webhook(
            hass,
            entry,
            port=manager.port,
            secret_path=secret_path,
            auth_mode=auth_mode,
        )
    except Exception as err:
        _LOGGER.exception("Embedded ha-mcp server: webhook registration failed")
        await manager.async_stop()
        hass.data.get(DOMAIN, {}).pop(DATA_EMBEDDED_MANAGER, None)
        _create_issue(hass, f"webhook registration failed: {err}")
        return

    _surface_connect_urls(hass, entry, auth_mode)


async def async_unload_embedded_server(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Unregister the webhook and stop the server thread (reload-safe).

    Does NOT revoke the provisioned token — a reload must keep it. Idempotent.
    """
    await async_unregister_webhook(hass)
    manager = hass.data.get(DOMAIN, {}).pop(DATA_EMBEDDED_MANAGER, None)
    if isinstance(manager, EmbeddedServerManager):
        await manager.async_stop()


async def async_remove_embedded_server(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke the provisioned credentials when the config entry is removed."""
    await EmbeddedServerManager(hass, entry).async_revoke_credentials()
    _clear_issues(hass)


async def _async_cleanup_disabled(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Revoke a leftover token after the server was disabled in options."""
    _clear_issues(hass)
    manager = EmbeddedServerManager(hass, entry)
    await manager.async_revoke_credentials()


def _ensure_secrets(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Generate + persist the stable webhook id and secret path on first enable.

    Both live in ``entry.data`` and stay stable across restarts so the connect
    URL never changes. Must run before the options update-listener is registered
    so this ``async_update_entry`` does not trigger a reload mid-setup.
    """
    data = dict(entry.data)
    changed = False
    if not data.get(DATA_WEBHOOK_ID):
        data[DATA_WEBHOOK_ID] = f"mcp_{secrets.token_hex(16)}"
        changed = True
    if not data.get(DATA_SECRET_PATH):
        data[DATA_SECRET_PATH] = f"/private_{secrets.token_urlsafe(16)}"
        changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=data)


def _surface_connect_urls(
    hass: HomeAssistant, entry: ConfigEntry, auth_mode: str
) -> None:
    """Log the connect URLs and (re)create a persistent notification with them."""
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    webhook_id = entry.data[DATA_WEBHOOK_ID]
    urls: list[str] = []

    # Nabu Casa remote URL (only when the cloud integration is set up + logged in).
    try:
        from homeassistant.components.cloud import (
            CloudNotAvailable,
            async_remote_ui_url,
        )

        try:
            cloud_base = async_remote_ui_url(hass)
            urls.append(f"{cloud_base}/api/webhook/{webhook_id}")
        except CloudNotAvailable:
            pass
    except ImportError:
        pass

    try:
        local_base = get_url(hass, allow_external=False, prefer_external=False)
        urls.append(f"{local_base}/api/webhook/{webhook_id}")
    except NoURLAvailableError:
        pass

    if not urls:
        urls.append(f"/api/webhook/{webhook_id}  (prefix with your Home Assistant URL)")

    port = int(entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
    bind_host = str(entry.options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
    auth_note = (
        "The webhook URL is the shared secret (no bearer required)."
        if auth_mode == WEBHOOK_AUTH_NONE
        else "Clients authenticate with your Home Assistant account (ha_auth)."
    )

    url_lines = "\n".join(f"- {url}" for url in urls)
    _LOGGER.info(
        "Embedded ha-mcp server is running. Connect URL(s):\n%s\n%s",
        url_lines,
        auth_note,
    )
    message = (
        "The Home Assistant MCP server is now running inside Home Assistant.\n\n"
        "Connect your MCP client to:\n"
        f"{url_lines}\n\n"
        f"{auth_note}\n"
    )
    if bind_host != DEFAULT_BIND_HOST:
        message += (
            f"\nDirect LAN access is also available at "
            f"http://<home-assistant-ip>:{port}{entry.data[DATA_SECRET_PATH]}\n"
        )
    persistent_notification.async_create(
        hass,
        message,
        title="Home Assistant MCP Server (in-process)",
        notification_id=_NOTIFICATION_ID,
    )


def _create_issue(hass: HomeAssistant, detail: str) -> None:
    """File a repair issue describing an embedded-server startup failure."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_EMBEDDED_START_FAILED,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_EMBEDDED_START_FAILED,
        translation_placeholders={"detail": detail},
    )


def _clear_issues(hass: HomeAssistant) -> None:
    """Clear any previously-filed embedded-server repair issues."""
    for issue_id in _EMBEDDED_ISSUE_IDS:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
