"""Bring the in-process ha-mcp server up and down for the config entry (#1527).

Orchestration between :mod:`embedded_server` (the server thread + token
provisioning) and :mod:`mcp_webhook` (the ingress webhook): the bring-up sequence,
repair issues on failure, connect-URL surfacing, and teardown. Kept out of
``__init__.py`` so the entry-point wiring stays thin and this logic is
independently testable.

Every failure here is contained: a failure files a repair issue and returns
rather than propagating out of the background bring-up task, so the rest of Home
Assistant keeps running even when the server can't be installed or started.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    DATA_MANAGER,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_BIND_HOST,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_NONE,
)
from .embedded_server import EmbeddedServerError, EmbeddedServerManager
from .mcp_webhook import async_register_webhook, async_unregister_webhook

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = "ha_mcp_server_connect"
_ISSUE_IDS = (ISSUE_PACKAGE_FAILED, ISSUE_START_FAILED)


async def async_bring_up_server(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Install, start, and expose the server. Runs as a background task.

    On failure files the matching repair issue and returns — Home Assistant stays
    up. On cancellation (the entry is being unloaded mid-bring-up) tears down any
    partial state and re-raises so the task ends cancelled. The secret webhook id
    and secret path must already exist in ``entry.data`` (the entry setup writes
    them before scheduling this task).
    """
    _clear_issues(hass)

    manager = EmbeddedServerManager(hass, entry)
    hass.data.setdefault(DOMAIN, {})[DATA_MANAGER] = manager

    try:
        await manager.async_start()

        auth_mode = str(entry.options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE))
        secret_path = str(entry.data[DATA_SECRET_PATH])
        await async_register_webhook(
            hass,
            entry,
            port=manager.port,
            secret_path=secret_path,
            auth_mode=auth_mode,
        )
        _surface_connect_urls(hass, entry, auth_mode)
    except asyncio.CancelledError:
        # Unloaded mid-bring-up: undo whatever partial state exists, then let the
        # cancellation propagate so the task ends cancelled.
        await async_teardown_server(hass)
        raise
    except EmbeddedServerError as err:
        _LOGGER.error("Home Assistant MCP Server failed to start: %s", err)
        await async_teardown_server(hass)
        _create_issue(hass, err.kind, str(err))
    except Exception as err:
        _LOGGER.exception("Home Assistant MCP Server: bring-up failed")
        await async_teardown_server(hass)
        _create_issue(hass, "start", str(err))


async def async_teardown_server(hass: HomeAssistant) -> None:
    """Unregister the webhook and stop the server thread (reload-safe, idempotent).

    Does NOT revoke the provisioned token — a reload must keep it. The ha_auth
    discovery views stay bound (aiohttp can't unregister them until HA restarts);
    they 404 while the entry is not live.
    """
    await async_unregister_webhook(hass)
    manager = hass.data.get(DOMAIN, {}).pop(DATA_MANAGER, None)
    if isinstance(manager, EmbeddedServerManager):
        await manager.async_stop()


async def async_revoke_credentials_on_remove(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Revoke the provisioned credentials when the config entry is removed."""
    await EmbeddedServerManager(hass, entry).async_revoke_credentials()
    _clear_issues(hass)


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
        "Home Assistant MCP Server is running. Connect URL(s):\n%s\n%s",
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
        title="Home Assistant MCP Server",
        notification_id=_NOTIFICATION_ID,
    )


def _create_issue(hass: HomeAssistant, kind: str, detail: str) -> None:
    """File the repair issue matching the failure ``kind`` (package / start)."""
    issue_id = ISSUE_PACKAGE_FAILED if kind == "package" else ISSUE_START_FAILED
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=issue_id,
        translation_placeholders={"detail": detail},
    )


def _clear_issues(hass: HomeAssistant) -> None:
    """Clear any previously-filed server-bring-up repair issues."""
    for issue_id in _ISSUE_IDS:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
