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
import secrets
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration

from .const import (
    BIND_HOST_ALL,
    DATA_DCR_SIGNING_KEY,
    DATA_MANAGER,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_SECRET_PATH,
    DATA_WEBHOOK_ID,
    DEFAULT_BIND_HOST,
    DEFAULT_ENABLE_LLM_API,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    ISSUE_COMPONENT_OUTDATED,
    ISSUE_LEGACY_OAUTH_RESTART,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    OPT_BIND_HOST,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_STARTUP_NOTIFICATION,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    SERVER_UPDATES_DOCS_URL,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
)
from .embedded_server import EmbeddedServerError, EmbeddedServerManager
from .llm_api import async_register_llm_api, async_unregister_llm_api
from .mcp_webhook import async_register_webhook, async_unregister_webhook
from .oauth_legacy import legacy_credentials_active

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

        # The package is installed and importable now: verify the running
        # component satisfies the server's MIN_COMPONENT_VERSION and file/clear
        # the component-outdated repair issue. Advisory only — it never blocks
        # the (already started) server.
        await _async_check_component_compat(hass, entry)

        auth_mode = str(entry.options.get(OPT_WEBHOOK_AUTH, WEBHOOK_AUTH_NONE))
        secret_path = str(entry.data[DATA_SECRET_PATH])
        webhook_enabled = bool(entry.options.get(OPT_ENABLE_WEBHOOK, True))
        oauth_client_id = entry.data.get(DATA_OAUTH_CLIENT_ID)
        oauth_client_secret = entry.data.get(DATA_OAUTH_CLIENT_SECRET)
        # Always set up the loopback forwarding config — the sidebar settings
        # panel proxies through it (#1803); the option gates only the public
        # webhook endpoint. oauth_* args are ignored unless auth_mode is legacy.
        if not entry.data.get(DATA_DCR_SIGNING_KEY):
            # Upgrade path: entries created before 2.0.0 have no DCR key.
            # entry.data must stay JSON-serializable, so store hex.
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    DATA_DCR_SIGNING_KEY: secrets.token_bytes(32).hex(),
                },
            )
        oauth_restart_needed = await async_register_webhook(
            hass,
            entry,
            port=manager.port,
            secret_path=secret_path,
            auth_mode=auth_mode,
            register_endpoint=webhook_enabled,
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
            oauth_signing_key=entry.data.get(DATA_OAUTH_SIGNING_KEY),
            dcr_signing_key=entry.data.get(DATA_DCR_SIGNING_KEY),
        )
        _async_update_legacy_oauth_issue(hass, oauth_restart_needed)
        if not webhook_enabled:
            _LOGGER.info(
                "Webhook access disabled by option - the server is local-only "
                "(direct port + sidebar panel)"
            )
        # Whether the bound provider already serves the configured legacy
        # credentials (a rotation pending restart does not yet; see
        # legacy_credentials_active), so the log can say so.
        oauth_creds_active = True
        if webhook_enabled and auth_mode == WEBHOOK_AUTH_LEGACY:
            oauth_creds_active = legacy_credentials_active(
                hass,
                str(oauth_client_id or ""),
                str(oauth_client_secret or ""),
                str(entry.data.get(DATA_OAUTH_SIGNING_KEY) or ""),
            )
        _surface_connect_urls(
            hass,
            entry,
            auth_mode,
            webhook_enabled=webhook_enabled,
            oauth_creds_active=oauth_creds_active,
            oauth_restart_pending=oauth_restart_needed,
        )
        # Conversation-agent LLM API (#1745), gated on its option (default on).
        # Advisory: registration failures are contained inside (logged, feature
        # absent) — the running server must never be taken down by them.
        if bool(entry.options.get(OPT_ENABLE_LLM_API, DEFAULT_ENABLE_LLM_API)):
            await async_register_llm_api(
                hass, entry, port=manager.port, secret_path=secret_path
            )
        else:
            _LOGGER.info(
                "Conversation-agent LLM API disabled by option - the toolset "
                "will not be offered to Home Assistant conversation agents"
            )
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
    """Unregister the LLM API + webhook and stop the server thread (reload-safe,
    idempotent).

    Does NOT revoke the provisioned token — a reload must keep it. The ha_auth
    discovery views stay bound (aiohttp can't unregister them until HA restarts);
    they 404 while the entry is not live.
    """
    async_unregister_llm_api(hass)
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
    ir.async_delete_issue(hass, DOMAIN, ISSUE_COMPONENT_OUTDATED)
    # Clear the legacy-OAuth restart repair too: it is filed only from bring-up,
    # which never runs again for a removed entry, so a restart that was still
    # pending at removal would otherwise leave a dangling warning for a server
    # that no longer exists. (Re-enabling legacy on a fresh entry re-files it.)
    ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_OAUTH_RESTART)


async def async_get_lan_hosts(hass: HomeAssistant) -> list[str]:
    """Every IPv4 address on an enabled network adapter, in adapter order (#1862).

    Lets the connect-URL surfaces list one entry per interface on a
    multi-interface / multi-VLAN host, instead of only the single address
    ``get_url`` resolves. IPv4 only: ``async_get_adapters`` returns a bare
    IPv6 ``address`` with a separate ``scope_id`` int, so a link-local adapter
    address is not a usable URL host without rejoining that zone id (and every
    IPv6 host additionally needs bracket-wrapping), so building those correctly
    is out of scope; the reported setups are IPv4. Best-effort: any failure
    yields an empty list so URL
    surfacing degrades to the single ``get_url`` host rather than taking down
    the caller (bring-up would otherwise file a repair issue for a display-only
    lookup).
    """
    try:
        from homeassistant.components import network

        adapters = await network.async_get_adapters(hass)
        # The whole extraction is inside the try (not just the fetch): a
        # malformed adapter entry must degrade to the single get_url host too,
        # never escape into async_bring_up_server's handler, which would tear
        # the running server down and file a start-failure for a display-only
        # lookup.
        return [
            ipv4["address"]
            for adapter in adapters
            if adapter["enabled"]
            for ipv4 in adapter["ipv4"]
        ]
    except Exception:  # display-only enumeration; must never fail the caller
        _LOGGER.warning(
            "Adapter enumeration failed; using the single resolved host",
            exc_info=True,
        )
        return []


def _swap_url_host(base: str, host: str) -> str:
    """Return ``base`` with its host replaced by ``host`` (scheme/port/path kept)."""
    parsed = urlparse(base)
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return parsed._replace(netloc=netloc).geturl()


def _dedup_hosts(primary: str | None, extra: list[str] | None) -> list[str]:
    """Ordered unique hosts, ``primary`` (the canonical get_url host) first."""
    ordered: list[str] = []
    for host in (primary, *(extra or [])):
        if host and host not in ordered:
            ordered.append(host)
    return ordered


def _resolve_local_url(hass: HomeAssistant) -> tuple[str | None, str | None]:
    """The get_url internal base URL and its host, or ``(None, None)``."""
    from homeassistant.helpers.network import NoURLAvailableError, get_url

    try:
        base = get_url(hass, allow_external=False, prefer_external=False)
    except NoURLAvailableError:
        return None, None  # No internal/local URL configured - hint form instead.
    return base, urlparse(base).hostname


def _local_webhook_urls(
    local_base: str, local_host: str | None, lan_hosts: list[str], webhook_id: str
) -> list[str]:
    """One local webhook URL per LAN host (canonical ``local_base`` verbatim)."""
    return [
        f"{local_base if host == local_host else _swap_url_host(local_base, host)}"
        f"/api/webhook/{webhook_id}"
        for host in lan_hosts
    ]


def build_connect_urls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    webhook_enabled: bool = True,
    extra_hosts: list[str] | None = None,
) -> list[str]:
    """Resolve the entry's connect URLs (webhook forms first, then direct).

    Backs the entry's Configure screen, the one administrator-only surface
    that shows real URLs (the start-up log and notification deliberately
    carry none - see ``_surface_connect_urls``). Each source is best-effort:
    a URL that cannot be resolved is omitted.

    ``extra_hosts`` (from :func:`async_get_lan_hosts`) adds one webhook and one
    direct-access URL per additional LAN address, so a multi-interface /
    multi-VLAN host surfaces every reachable interface rather than only the
    single host ``get_url`` picks (#1862). The ``get_url`` host stays canonical
    and first; any repeat of it in ``extra_hosts`` is deduped away.
    """
    webhook_id = entry.data.get(DATA_WEBHOOK_ID)
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

    local_base, local_host = _resolve_local_url(hass)

    # One entry per enabled LAN address so a multi-interface / multi-VLAN host
    # surfaces every reachable interface rather than get_url's single pick
    # (#1862). The get_url host stays canonical and first.
    lan_hosts = _dedup_hosts(local_host, extra_hosts)

    if local_base and webhook_id:
        urls.extend(_local_webhook_urls(local_base, local_host, lan_hosts, webhook_id))

    if not urls and webhook_id:
        urls.append(f"/api/webhook/{webhook_id}  (prefix with your Home Assistant URL)")

    port = int(entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
    bind_host = str(entry.options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
    secret_path = entry.data.get(DATA_SECRET_PATH)
    if bind_host == BIND_HOST_ALL and secret_path:
        # Direct-access URL: admin-gated surfaces only (log + Configure screen).
        # Guarded on the secret path so a missing one omits the line instead of
        # rendering a valid-looking URL without its credential segment.
        urls.extend(
            f"http://{host}:{port}{secret_path} (direct access)"
            for host in lan_hosts or ["<home-assistant-ip>"]
        )
    return urls


def _surface_connect_urls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_mode: str,
    *,
    webhook_enabled: bool = True,
    oauth_creds_active: bool = True,
    oauth_restart_pending: bool = False,
) -> None:
    """Log where to find the connect details and (re)create a notification.

    Neither surface carries a connect URL or a credential. In the secret-URL
    (``none``) mode the URL IS an admin-equivalent credential, and the log is
    readable by more than the administrator who set the entry up: the
    server's own log tools serve it to any connected client, and users paste
    it into bug reports. The entry's Configure screen is the one
    administrator-only surface that shows them (``build_connect_urls``).
    """
    if not webhook_enabled:
        auth_note = "Webhook access is disabled (local-only mode)."
    elif auth_mode == WEBHOOK_AUTH_NONE:
        auth_note = "The webhook URL is the shared secret (no bearer required)."
    elif auth_mode == WEBHOOK_AUTH_LEGACY:
        auth_note = (
            "OAuth (Beta) is ENABLED for this URL (legacy mode) - see the "
            "entry's Configure screen for the Client ID and Client Secret to "
            "paste into your MCP client."
        )
    else:
        auth_note = "Clients authenticate with your Home Assistant account (ha_auth)."

    log_message = (
        "HA-MCP in-process server is running. The connect URL(s) are on the "
        "entry's Configure screen (Settings - Devices & Services - HA-MCP "
        f"Custom Component - HA-MCP Server - Configure). {auth_note}"
    )
    if webhook_enabled and auth_mode == WEBHOOK_AUTH_LEGACY:
        if not oauth_creds_active:
            # A rotation is pending the restart: the bound root views still
            # serve the OLD identity until then (see legacy_credentials_active).
            log_message += (
                " The OAuth credentials were rotated and take effect after "
                "the restart Home Assistant is asking for; until then the "
                "previous credentials remain active."
            )
        elif oauth_restart_pending:
            # First-enable mid-session late-binds the root views, so
            # /authorize is not live until the restart the repair asks for.
            log_message += (
                " Legacy OAuth is not live until the restart Home Assistant "
                "is asking for; the credentials work once you restart."
            )
    _LOGGER.info(log_message)
    if not bool(entry.options.get(OPT_ENABLE_STARTUP_NOTIFICATION, True)):
        # Notification suppressed by option: clear any notification created
        # before the toggle was turned off, then skip creating a fresh one.
        persistent_notification.async_dismiss(hass, _NOTIFICATION_ID)
        return
    # The sidebar-panel line is included only while the panel is registered:
    # with the panel option off the /ha-mcp route does not exist and the link
    # would 404.
    panel_line = (
        "Manage it from the [HA-MCP settings panel](/ha-mcp) in the sidebar.\n\n"
        if bool(entry.options.get(OPT_ENABLE_SIDEBAR_PANEL, True))
        else ""
    )
    # SECURITY (review finding): persistent notifications are visible to EVERY
    # authenticated Home Assistant user - core's persistent_notification/get
    # and /subscribe carry no admin gate. In the default posture the connect
    # URL IS an admin-equivalent credential, so the notification deliberately
    # carries NO secrets: it points at the admin-only surfaces (the sidebar
    # panel and the entry's Configure screen).
    message = (
        "The HA-MCP Server is now running inside Home Assistant.\n\n"
        f"{panel_line}"
        "The connect URL is shown on the entry's Configure screen "
        "(Settings - Devices & Services - HA-MCP Custom Component - "
        "HA-MCP Server - Configure), which only administrators can open, "
        "because the URL is the credential.\n\n"
        f"{auth_note}\n\n"
        "To disable this notification, uncheck the startup notification box "
        "on that same configuration screen.\n"
    )
    persistent_notification.async_create(
        hass,
        message,
        title="HA-MCP Server",
        notification_id=_NOTIFICATION_ID,
    )


def _async_update_legacy_oauth_issue(hass: HomeAssistant, restart_needed: bool) -> None:
    """File/clear the legacy-OAuth restart repair per ``async_register_webhook``'s
    return value.

    Raised on BOTH transitions (see that function's docstring): enabling
    legacy mode (the root views just bound, or bound with different
    credentials than before) and disabling it (the views are still bound from
    a prior legacy registration). aiohttp can neither bind nor release an HTTP
    view without a full Home Assistant restart either way.
    """
    if restart_needed:
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_LEGACY_OAUTH_RESTART,
            is_fixable=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_LEGACY_OAUTH_RESTART,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_LEGACY_OAUTH_RESTART)


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


# ---------------------------------------------------------------------------
# Component / server version-compatibility repair issue
# ---------------------------------------------------------------------------


def _read_min_component_version() -> str | None:
    """Return the server's declared ``MIN_COMPONENT_VERSION``, or None (blocking).

    Imported here (in an executor thread) so the heavy ``ha_mcp`` import stays
    off the event loop and out of this module's top level. Guards older/newer
    server layouts that do not expose the constant by returning None (skip).
    """
    try:
        from ha_mcp.tools.tools_filesystem import MIN_COMPONENT_VERSION
    except (ImportError, AttributeError):
        return None
    return str(MIN_COMPONENT_VERSION)


async def _async_check_component_compat(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """File/clear the component-outdated repair issue for the running server.

    The ha-mcp server declares the minimum custom-component version it needs
    (``MIN_COMPONENT_VERSION``). Each component release pins the server it was
    built with, so only a pip-spec override can install a server that expects
    a newer component. When it does, surface a WARNING repair issue pointing
    at the HACS component update; clear it once the component is new enough.

    Advisory only — it must never block or fail server startup, so an
    unexpected error is logged (visible, not silent) and swallowed rather than
    propagated to the bring-up's failure handling.
    """
    required = await hass.async_add_executor_job(_read_min_component_version)
    if required is None:
        # Server predates MIN_COMPONENT_VERSION, or a newer layout moved it —
        # nothing to enforce.
        return

    try:
        integration = await async_get_integration(hass, DOMAIN)
        if integration.version is None:
            # None rather than an exception; "None" would then reach
            # AwesomeVersion and compare as an ordinary string.
            raise ValueError("the manifest carries no version")
        own = str(integration.version)
    except Exception:
        # The loader legitimately raises a wide, varied surface
        # (IntegrationNotFound, manifest errors); advisory check, logged
        # visibly with the traceback rather than swallowed silently.
        _LOGGER.warning(
            "Could not read the HA-MCP component version for the compatibility check",
            exc_info=True,
        )
        return

    try:
        outdated = AwesomeVersion(own) < AwesomeVersion(required)
    except AwesomeVersionException as err:
        # Incomparable version strategies only; real bugs propagate.
        _LOGGER.debug("HA-MCP component-compat version compare failed: %s", err)
        return

    if outdated:
        _LOGGER.warning(
            "The installed ha-mcp server requires HA-MCP Custom Component %s or "
            "newer, but %s is running; update the component via HACS.",
            required,
            own,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_COMPONENT_OUTDATED,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_COMPONENT_OUTDATED,
            translation_placeholders={"required": required, "installed": own},
            learn_more_url=SERVER_UPDATES_DOCS_URL,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_COMPONENT_OUTDATED)
