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
from contextlib import suppress
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import (
    BIND_HOST_ALL,
    DATA_MANAGER,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_BIND_HOST,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_NONE,
)
from .embedded_server import EmbeddedServerError, EmbeddedServerManager
from .mcp_webhook import async_register_webhook, async_unregister_webhook

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = "ha_mcp_tools_server_connect"
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
        webhook_enabled = bool(entry.options.get(OPT_ENABLE_WEBHOOK, True))
        if webhook_enabled:
            await async_register_webhook(
                hass,
                entry,
                port=manager.port,
                secret_path=secret_path,
                auth_mode=auth_mode,
            )
        else:
            _LOGGER.info(
                "Webhook access disabled by option - the server is local-only "
                "(direct port + sidebar panel)"
            )
        _surface_connect_urls(hass, entry, auth_mode, webhook_enabled=webhook_enabled)
    except asyncio.CancelledError:
        # Unloaded mid-bring-up: undo whatever partial state exists, then let the
        # cancellation propagate so the task ends cancelled.
        await async_teardown_server(hass)
        raise
    except EmbeddedServerError as err:
        _LOGGER.error("HA-MCP in-process server failed to start: %s", err)
        # suppress: filing the repair issue must be UNCONDITIONAL (review
        # finding) - a raising teardown would otherwise leave the entry
        # looking healthy with the failure visible only in the log.
        with suppress(Exception):
            await async_teardown_server(hass)
        _create_issue(hass, err.kind, str(err))
    except Exception as err:
        _LOGGER.exception("HA-MCP in-process server: bring-up failed")
        with suppress(Exception):
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
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_mode: str,
    *,
    webhook_enabled: bool = True,
) -> None:
    """Log the connect URLs and (re)create a persistent notification with them."""
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    webhook_id = entry.data[DATA_WEBHOOK_ID]
    urls: list[str] = []
    external = str(entry.options.get(OPT_EXTERNAL_URL) or "").rstrip("/")
    if not webhook_enabled:
        # Local-only mode: no webhook exists, so no webhook URLs to surface.
        external = ""
        webhook_id = None
    if external:
        # Owner-requested parity with the webhook-proxy app: a configured
        # external URL leads the list (any reverse proxy, not just Nabu Casa).
        urls.append(f"{external}/api/webhook/{webhook_id}")

    # Nabu Casa remote URL (only when the cloud integration is set up + logged in).
    try:
        from homeassistant.components.cloud import (
            CloudNotAvailable,
            async_remote_ui_url,
        )

        try:
            if webhook_id:
                cloud_base = async_remote_ui_url(hass)
                urls.append(f"{cloud_base}/api/webhook/{webhook_id}")
        except CloudNotAvailable:
            pass  # Cloud not logged in / remote UI off - no remote URL to show.
    except ImportError:
        pass  # Cloud integration not installed (e.g. HA Core) - local URL only.

    try:
        if webhook_id:
            local_base = get_url(hass, allow_external=False, prefer_external=False)
            urls.append(f"{local_base}/api/webhook/{webhook_id}")
    except NoURLAvailableError:
        pass  # No internal/local URL configured - fall through to the hint form.

    if not urls and webhook_id:
        urls.append(f"/api/webhook/{webhook_id}  (prefix with your Home Assistant URL)")

    port = int(entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
    bind_host = str(entry.options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
    auth_note = (
        "Webhook access is disabled (local-only mode)."
        if not webhook_enabled
        else "The webhook URL is the shared secret (no bearer required)."
        if auth_mode == WEBHOOK_AUTH_NONE
        else "Clients authenticate with your Home Assistant account (ha_auth)."
    )

    if bind_host == BIND_HOST_ALL:
        # Direct-access URL goes to the LOG only (admin-gated), never the
        # notification - see the security note below.
        urls.append(
            f"http://<home-assistant-ip>:{port}{entry.data[DATA_SECRET_PATH]}"
            " (direct access)"
        )
    url_lines = "\n".join(f"- {url}" for url in urls)
    _LOGGER.info(
        "HA-MCP in-process server is running. Connect URL(s):\n%s\n%s",
        url_lines,
        auth_note,
    )
    # SECURITY (review finding): persistent notifications are visible to EVERY
    # authenticated Home Assistant user - core's persistent_notification/get
    # and /subscribe carry no admin gate. In the default posture the connect
    # URL IS an admin-equivalent credential, so the notification deliberately
    # carries NO secrets: it points at the admin-only surfaces (the sidebar
    # panel and the entry's Configure screen). The URLs above still go to the
    # log at INFO, which only admin-gated surfaces expose - the same posture
    # as the add-on printing its URL to the admin-only add-on log.
    message = (
        "The HA-MCP Server is now running inside Home Assistant.\n\n"
        "Manage it from the [HA-MCP settings panel](/ha-mcp) in the sidebar.\n\n"
        "The connect URL is shown on the entry's Configure screen "
        "(Settings - Devices & Services - HA-MCP Custom Component - "
        "HA-MCP Server - Configure) and in the Home Assistant log - both "
        "administrator-only, because the URL is the credential.\n\n"
        f"{auth_note}\n"
    )
    persistent_notification.async_create(
        hass,
        message,
        title="HA-MCP Server",
        notification_id=_NOTIFICATION_ID,
    )


_ISSUE_BY_KIND = {
    "package": ISSUE_PACKAGE_FAILED,
    "start": ISSUE_START_FAILED,
}


def _create_issue(hass: HomeAssistant, kind: str, detail: str) -> None:
    """File the repair issue matching the failure ``kind`` (package / start).

    Exhaustive lookup on purpose: an unknown kind is a coding error and must
    raise here rather than silently filing the wrong user-facing repair issue.
    """
    issue_id = _ISSUE_BY_KIND[kind]
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
