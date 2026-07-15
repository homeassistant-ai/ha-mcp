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
from urllib.parse import urlparse

from aiohttp import ClientError
from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.loader import async_get_integration

from .const import (
    BIND_HOST_ALL,
    CHANNEL_DEV,
    COMPONENT_MANIFEST_AT_TAG_URL,
    DATA_BRINGUP_TASK,
    DATA_MANAGER,
    DATA_OAUTH_CLIENT_ID,
    DATA_OAUTH_CLIENT_SECRET,
    DATA_OAUTH_SIGNING_KEY,
    DATA_PENDING_UPDATE_NOTIFY,
    DATA_SECRET_PATH,
    DATA_UPDATE_COORDINATOR,
    DATA_WEBHOOK_ID,
    DEFAULT_AUTO_UPDATE,
    DEFAULT_BIND_HOST,
    DEFAULT_ENABLE_LLM_API,
    DEFAULT_PIP_SPEC,
    DEFAULT_SERVER_PORT,
    DOMAIN,
    HACS_COMPONENT_URL,
    ISSUE_COMPONENT_OUTDATED,
    ISSUE_LEGACY_OAUTH_RESTART,
    ISSUE_PACKAGE_FAILED,
    ISSUE_START_FAILED,
    ISSUE_UPDATE_HELD,
    OPT_AUTO_UPDATE,
    OPT_BIND_HOST,
    OPT_ENABLE_LLM_API,
    OPT_ENABLE_SIDEBAR_PANEL,
    OPT_ENABLE_STARTUP_NOTIFICATION,
    OPT_ENABLE_WEBHOOK,
    OPT_EXTERNAL_URL,
    OPT_PIP_SPEC,
    OPT_SERVER_PORT,
    OPT_WEBHOOK_AUTH,
    WEBHOOK_AUTH_LEGACY,
    WEBHOOK_AUTH_NONE,
    channel_for_dist,
)
from .embedded_server import EmbeddedServerError, EmbeddedServerManager
from .llm_api import async_register_llm_api, async_unregister_llm_api
from .mcp_webhook import async_register_webhook, async_unregister_webhook
from .oauth_legacy import legacy_credentials_active

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import ServerVersionInfo

_LOGGER = logging.getLogger(__name__)

_NOTIFICATION_ID = "ha_mcp_tools_server_connect"
_UPDATE_NOTIFICATION_ID = "ha_mcp_tools_server_updated"
# ISSUE_UPDATE_HELD is cleared at bring-up start too: any reload that reaches
# bring-up either bypassed the hold deliberately (the update entity's Install
# button) or made it moot; if the hold still applies, the coordinator refresh
# that follows setup re-files it within moments.
_ISSUE_IDS = (ISSUE_PACKAGE_FAILED, ISSUE_START_FAILED, ISSUE_UPDATE_HELD)

# Per-request timeout for the component-manifest fetch behind the auto-update
# gate — mirrors the coordinator's PyPI fetch budget; a miss fails open.
_MANIFEST_FETCH_TIMEOUT_SECONDS = 30


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
        )
        _async_update_legacy_oauth_issue(hass, oauth_restart_needed)
        if not webhook_enabled:
            _LOGGER.info(
                "Webhook access disabled by option - the server is local-only "
                "(direct port + sidebar panel)"
            )
        # Only surface cleartext credentials once the bound provider actually
        # serves them: while a rotation is pending restart, an old-identity
        # token still validates and can read this log (see
        # legacy_credentials_active).
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
            oauth_client_id=oauth_client_id,
            oauth_client_secret=oauth_client_secret,
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
        await _async_finish_update_cycle(hass)
    except asyncio.CancelledError:
        # Unloaded mid-bring-up: undo whatever partial state exists, then let the
        # cancellation propagate so the task ends cancelled. The pending
        # update-notification marker (if any) deliberately survives — it
        # belongs to a bring-up that has not run yet, not to this one.
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
        # The install did not land: never fire the "updated" notification for
        # it — the repair issue above is the user-facing signal.
        _drop_pending_update_notify(hass)
    except Exception as err:
        _LOGGER.exception("HA-MCP in-process server: bring-up failed")
        with suppress(Exception):
            await async_teardown_server(hass)
        _create_issue(hass, "start", str(err))
        _drop_pending_update_notify(hass)


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


def build_connect_urls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    webhook_enabled: bool = True,
) -> list[str]:
    """Resolve the entry's connect URLs (webhook forms first, then direct).

    Shared by the admin-only surfaces that show real URLs: the Home Assistant
    log on start-up and the entry's Configure screen (the notification
    deliberately carries none - it is visible to every signed-in user). Each
    source is best-effort: a URL that cannot be resolved is omitted.
    """
    from homeassistant.helpers.network import NoURLAvailableError, get_url

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

    local_host: str | None = None
    try:
        local_base = get_url(hass, allow_external=False, prefer_external=False)
        local_host = urlparse(local_base).hostname
        if webhook_id:
            urls.append(f"{local_base}/api/webhook/{webhook_id}")
    except NoURLAvailableError:
        pass  # No internal/local URL configured - fall through to the hint form.

    if not urls and webhook_id:
        urls.append(f"/api/webhook/{webhook_id}  (prefix with your Home Assistant URL)")

    port = int(entry.options.get(OPT_SERVER_PORT, DEFAULT_SERVER_PORT))
    bind_host = str(entry.options.get(OPT_BIND_HOST, DEFAULT_BIND_HOST))
    secret_path = entry.data.get(DATA_SECRET_PATH)
    if bind_host == BIND_HOST_ALL and secret_path:
        # Direct-access URL: admin-gated surfaces only (log + Configure screen).
        # Guarded on the secret path so a missing one omits the line instead of
        # rendering a valid-looking URL without its credential segment.
        urls.append(
            f"http://{local_host or '<home-assistant-ip>'}:{port}{secret_path}"
            " (direct access)"
        )
    return urls


def _surface_connect_urls(
    hass: HomeAssistant,
    entry: ConfigEntry,
    auth_mode: str,
    *,
    webhook_enabled: bool = True,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    oauth_creds_active: bool = True,
    oauth_restart_pending: bool = False,
) -> None:
    """Log the connect URLs and (re)create a persistent notification."""
    urls = build_connect_urls(hass, entry, webhook_enabled=webhook_enabled)
    if not webhook_enabled:
        auth_note = "Webhook access is disabled (local-only mode)."
    elif auth_mode == WEBHOOK_AUTH_NONE:
        auth_note = "The webhook URL is the shared secret (no bearer required)."
    elif auth_mode == WEBHOOK_AUTH_LEGACY:
        # Kept secret-free (unlike the log line below) — see the SECURITY note
        # on the persistent notification further down, which reuses this text.
        creds_where = (
            "the Home Assistant log or the entry's Configure screen"
            if oauth_creds_active
            else "the entry's Configure screen"
        )
        auth_note = (
            "OAuth (Beta) is ENABLED for this URL (legacy mode) - see "
            f"{creds_where} for the Client ID and Client Secret to paste "
            "into your MCP client."
        )
    else:
        auth_note = "Clients authenticate with your Home Assistant account (ha_auth)."

    url_lines = "\n".join(f"- {url}" for url in urls)
    log_message = (
        "HA-MCP in-process server is running. "
        f"Connect URL(s):\n{url_lines}\n{auth_note}"
    )
    if webhook_enabled and auth_mode == WEBHOOK_AUTH_LEGACY:
        if oauth_creds_active:
            # Admin-only log (mirrors the webhook-proxy add-on's own startup
            # log, start.py). Cleartext credentials — deliberately NOT in the
            # persistent notification below, which every signed-in user can
            # see.
            log_message += (
                f"\n  OAuth Client ID:     {oauth_client_id}"
                f"\n  OAuth Client Secret: {oauth_client_secret}"
            )
            if oauth_restart_pending:
                # First-enable mid-session late-binds the root views, so
                # /authorize is not live until the restart the repair asks
                # for. The credentials ARE the ones that will be served
                # (oauth_creds_active is True), but pasting them now gets a
                # connection that fails until the restart — same caveat the
                # rotation branch, the options hint, and the oauth_regenerate
                # help text carry.
                log_message += (
                    "\n  Legacy OAuth is not live until the restart Home "
                    "Assistant is asking for; these credentials work once "
                    "you restart."
                )
            log_message += (
                "\n  Paste both into your MCP client's OAuth connector setup "
                "(e.g. Google Gemini Spark: Advanced settings)."
            )
        else:
            # SECURITY (review finding on #1880): while a credential rotation
            # is pending the restart, the bound root views still serve the OLD
            # identity, so a token issued under it stays valid — and could
            # read this log through the server's own log tools. Logging the
            # NEW credentials here would hand them to exactly the party the
            # rotation is meant to evict, so they are withheld until the
            # restart makes them active (which also kills every old token).
            log_message += (
                "\n  The OAuth credentials were rotated and take effect after "
                "the restart Home Assistant is asking for; until then the "
                "previous credentials remain active. The new Client ID and "
                "Client Secret are on the entry's Configure screen."
            )
    _LOGGER.info(log_message)
    if not bool(entry.options.get(OPT_ENABLE_STARTUP_NOTIFICATION, True)):
        # Notification suppressed by option: clear any notification created
        # before the toggle was turned off, then skip creating a fresh one. The
        # connect URLs still reached the admin-only log above.
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
    # panel and the entry's Configure screen). The URLs above still go to the
    # log at INFO, which only admin-gated surfaces expose - the same posture
    # as the add-on printing its URL to the admin-only add-on log.
    message = (
        "The HA-MCP Server is now running inside Home Assistant.\n\n"
        f"{panel_line}"
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
            is_fixable=False,
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
# Automatic server-version updates (channel auto-update)
# ---------------------------------------------------------------------------


async def async_maybe_auto_update(
    hass: HomeAssistant, entry: ConfigEntry, info: ServerVersionInfo | None
) -> None:
    """Reload the entry when ``info`` shows a newer build AND auto-update is on.

    Called from the :class:`~.coordinator.ServerVersionCoordinator` listener
    registered by :mod:`embedded_entry` on every refresh (every
    ``UPDATE_CHECK_INTERVAL``, plus once shortly after setup). The coordinator
    itself always fetches (see its docstring) so the `update` platform entity
    stays populated regardless of this option; only the reload decided here is
    gated on it.

    Skips entirely when: auto-update is off, a pip-spec override is set,
    either version is unknown (``info`` may still be ``None`` — the
    coordinator's ``data`` type before its first successful refresh), or a
    bring-up is still in flight (below).

    A pending update is additionally gated on component compatibility
    (issues #1783/#1785): when the candidate release also shipped a newer
    custom component than the one running, the reload is HELD — loudly (a
    repair issue plus a warning log every check) and escapably (applying the
    HACS component update — which takes an HA restart, as the issue text
    says — unblocks the next check; the update entity's Install button never
    passes through here, so manual installs — like pip-spec overrides above —
    bypass the hold entirely). Every failure inside the gate fails OPEN so a
    GitHub hiccup can never wedge updates.

    Best-effort: an incomparable version string (AwesomeVersionException) is
    logged at debug and skipped; the next refresh retries. Genuine bugs
    propagate per the repo's no-silent-failure convention.
    """
    if not bool(entry.options.get(OPT_AUTO_UPDATE, DEFAULT_AUTO_UPDATE)):
        # Auto-update turned off: stay on the currently-installed version.
        return

    override = str(entry.options.get(OPT_PIP_SPEC) or "").strip()
    if override and override != DEFAULT_PIP_SPEC:
        return

    if info is None or info.installed is None or info.latest is None:
        # Nothing to compare (not installed yet, or the PyPI fetch failed /
        # was skipped) - the bring-up path installs the newest build itself.
        return

    bringup_task = hass.data.get(DOMAIN, {}).get(DATA_BRINGUP_TASK)
    if bringup_task is not None and not bringup_task.done():
        # The coordinator's first refresh runs shortly after setup, while the
        # background bring-up (embedded_entry.async_setup_server_entry) may
        # still be installing the package for the first time. Reloading here
        # would cancel that in-flight install (async_unload_server_entry
        # cancels the bring-up task on unload) and can loop: the reload's own
        # bring-up starts a fresh install that the NEXT refresh could again
        # interrupt.
        return

    try:
        newer = AwesomeVersion(info.latest) > AwesomeVersion(info.installed)
    except AwesomeVersionException as err:
        # Incomparable version strategies (e.g. a non-semver build string) — the
        # only expected failure here. Real bugs (TypeError, etc.) propagate.
        _LOGGER.debug("HA-MCP auto-update version compare failed: %s", err)
        return

    if not newer:
        # Up to date: a hold that was pending is resolved (the component
        # update landed and the unblocked reload installed the server).
        ir.async_delete_issue(hass, DOMAIN, ISSUE_UPDATE_HELD)
        return

    held = await _async_update_held_by_component(hass, info)
    if held is not None:
        shipped, running = held
        _LOGGER.warning(
            "HA-MCP server %s is available, but that release also updated the "
            "custom component (%s; running %s); holding the automatic server "
            "update until the component is updated via HACS. Press Install on "
            "the HA-MCP server update entity to install anyway.",
            info.latest,
            shipped,
            running,
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            ISSUE_UPDATE_HELD,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=ISSUE_UPDATE_HELD,
            translation_placeholders={
                "latest": str(info.latest),
                "shipped": shipped,
                "running": running,
            },
            learn_more_url=HACS_COMPONENT_URL,
        )
        return
    ir.async_delete_issue(hass, DOMAIN, ISSUE_UPDATE_HELD)

    channel = channel_for_dist(info.dist)
    _LOGGER.info(
        "HA-MCP server update available on the %s channel (%s -> %s); "
        "reloading the entry to install it.",
        channel,
        info.installed,
        info.latest,
    )
    # The "updated" notification must wait for the reloaded entry's bring-up to
    # actually install and start the new build — async_reload returns when
    # entry SETUP finishes, while the pip install still runs in the background
    # and can fail (review finding). Leave a marker for bring-up to pop:
    # notification on success (_async_finish_update_cycle), silent drop on
    # failure (the package/start repair issues cover that path).
    hass.data.setdefault(DOMAIN, {})[DATA_PENDING_UPDATE_NOTIFY] = {
        "old": info.installed
    }
    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except Exception:
        # A raising reload leaves no repair issue behind (those are filed by
        # bring-up, which never ran), so this ERROR log is the only signal —
        # it must not be swallowed or left at debug (review finding). The next
        # coordinator refresh retries the whole cycle.
        _drop_pending_update_notify(hass)
        _LOGGER.exception(
            "HA-MCP auto-update reload failed (%s -> %s on the %s channel)",
            info.installed,
            info.latest,
            channel,
        )


def _drop_pending_update_notify(hass: HomeAssistant) -> None:
    """Drop the deferred update-notification marker without notifying."""
    hass.data.get(DOMAIN, {}).pop(DATA_PENDING_UPDATE_NOTIFY, None)


async def _async_update_held_by_component(
    hass: HomeAssistant, info: ServerVersionInfo
) -> tuple[str, str] | None:
    """Return ``(shipped, running)`` when the pending update must be held.

    The #1783/#1785 breakage: a server release whose repo state also bumped the
    custom component auto-installed under the OLD component before HACS had
    even surfaced the component update. The component version in the manifest
    at the candidate release's git tag is what shipped with that server build —
    newer than the running component means the release changed the component
    too, so the automatic server install waits for the component.

    Fails OPEN (returns None → install proceeds, the pre-gate behavior) on
    every expected failure: manifest unreachable, component version unreadable,
    incomparable versions. Blocking updates indefinitely on a transient would
    be worse than the crash this guards against — and the crash itself is now
    also survivable server-side (the tools registry skips a failing module).
    """
    shipped = await _async_fetch_shipped_component_version(hass, str(info.latest))
    if shipped is None:
        return None

    try:
        integration = await async_get_integration(hass, DOMAIN)
        running = str(integration.version)
    except Exception:
        # Same wide loader surface as _async_check_component_compat: advisory
        # gate, logged visibly rather than swallowed silently.
        _LOGGER.warning(
            "Could not read the HA-MCP component version for the auto-update "
            "gate; proceeding with the update",
            exc_info=True,
        )
        return None

    try:
        if AwesomeVersion(running) < AwesomeVersion(shipped):
            return shipped, running
    except AwesomeVersionException as err:
        # Incomparable version strategies only; real bugs propagate.
        _LOGGER.debug("HA-MCP auto-update gate version compare failed: %s", err)
    return None


async def _async_fetch_shipped_component_version(
    hass: HomeAssistant, server_version: str
) -> str | None:
    """Return the component version shipped at server release ``vX.Y.Z``.

    Reads the component manifest as committed at the release's git tag (raw
    GitHub URL). Stable tags exist before the PyPI publish; a dev tag only
    appears after its binary builds finish, so a fresh dev version can 404
    here for some minutes — see COMPONENT_MANIFEST_AT_TAG_URL. Returns None
    on any failure; the caller treats that as "nothing to hold on"
    (fail-open).
    """
    url = COMPONENT_MANIFEST_AT_TAG_URL.format(version=server_version)
    try:
        session = async_get_clientsession(hass)
        async with asyncio.timeout(_MANIFEST_FETCH_TIMEOUT_SECONDS):
            async with session.get(url) as resp:
                resp.raise_for_status()
                # content_type=None: raw.githubusercontent.com serves
                # text/plain, which aiohttp's default json() rejects.
                payload = await resp.json(content_type=None)
        return str(payload["version"])
    except (ClientError, TimeoutError, KeyError, TypeError, ValueError) as err:
        _LOGGER.debug(
            "HA-MCP shipped-component manifest fetch failed for %s: %s", url, err
        )
        return None


async def _async_finish_update_cycle(hass: HomeAssistant) -> None:
    """Refresh the version entity and fire the deferred update notification.

    Runs at the end of a fully successful bring-up. Both halves belong exactly
    here (review findings): the freshly installed version is only knowable once
    the install landed — without a refresh the `update` entity keeps showing a
    stale "update available" for up to UPDATE_CHECK_INTERVAL after a successful
    install — and the notification deferred by async_maybe_auto_update must
    only fire for an install that actually happened. Advisory: a failure here
    must never fail the (already running) server, so it is logged visibly and
    swallowed. No reload loop: the refresh's listener re-enters
    async_maybe_auto_update, which no-ops on the still-running bring-up task.
    """
    domain_data = hass.data.get(DOMAIN, {})
    coordinator = domain_data.get(DATA_UPDATE_COORDINATOR)
    try:
        if coordinator is not None:
            await coordinator.async_refresh()
    except Exception:
        _LOGGER.warning("HA-MCP: post-install version refresh failed", exc_info=True)
    marker = domain_data.pop(DATA_PENDING_UPDATE_NOTIFY, None)
    if marker is None or coordinator is None or coordinator.data is None:
        return
    installed = coordinator.data.installed
    old = marker.get("old")
    if installed is None or installed == old:
        # The reload ran but the installed version did not actually move (the
        # install can legitimately resolve to the same build) — an "updated
        # to" notification would be false.
        return
    _create_update_notification(
        hass, channel_for_dist(coordinator.data.dist), old, installed
    )


def _create_update_notification(
    hass: HomeAssistant, channel: str, old_version: str, new_version: str
) -> None:
    """Notify that an automatic server update installed and the server is up.

    Only called from :func:`_async_finish_update_cycle` after a successful
    bring-up, so the versions are the confirmed before/after pair, never a
    prediction. SECURITY: same posture as ``_surface_connect_urls`` -
    persistent notifications are visible to every authenticated Home Assistant
    user, so this carries no secrets or connect URLs, only version numbers and
    a public GitHub link.
    """
    release_url = (
        "https://github.com/homeassistant-ai/ha-mcp/commits/master"
        if channel == CHANNEL_DEV
        else f"https://github.com/homeassistant-ai/ha-mcp/releases/tag/v{new_version}"
    )
    message = (
        f"The HA-MCP server was automatically updated from {old_version} to "
        f"{new_version} on the {channel} channel.\n\n"
        f"[Release notes]({release_url})"
    )
    persistent_notification.async_create(
        hass,
        message,
        title="HA-MCP Server updated",
        notification_id=_UPDATE_NOTIFICATION_ID,
    )


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
    (``MIN_COMPONENT_VERSION``). The server package updates independently of
    the HACS component (this manager pip-installs new server builds), so the
    running component can lag what the server expects. When it does, surface a
    WARNING repair issue pointing at the HACS component update; clear it once
    the component is new enough.

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
            learn_more_url=HACS_COMPONENT_URL,
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_COMPONENT_OUTDATED)
